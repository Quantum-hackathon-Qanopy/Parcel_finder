"""Near-live weekly monitoring of the project against its frozen comparison parcels.

    python src/monitor.py                  # fetch alerts, update the log, rebuild the dashboard
    python src/monitor.py --offline        # reuse the last download (no Earth Engine)
    python src/monitor.py --since 2025-01-01 --qae-every 8

Steps (pipeline steps 7 and 8):
  1. load the frozen snapshot (snapshot.py) and refuse to run if its hash no longer matches
  2. download weekly forest-disturbance alerts (RADD radar alerts, confirmed class) for the
     project's grid cells and for each comparison parcel from Earth Engine
  3. turn them into cumulative loss rates and the avoided loss
         (mean parcel loss rate - project loss rate) x project forest area
  4. give the uncertainty two ways: a classical bootstrap over the parcels, and, at
     checkpoints, a simulated quantum amplitude estimate next to a Monte Carlo estimate with
     the same number of evaluations (see amplitude_estimation.py)
  5. convert to tons of CO2e with the carbon density implied by the project's own projection
     and compare with the claim, pro-rated over the monitoring window
  6. write outputs/monitoring_log.csv, append a run record to outputs/monitoring_runs.csv and
     rebuild outputs/monitoring_dashboard.html

To run it every Monday at 07:00 on Windows, in a terminal from the repository root:

    schtasks /create /sc weekly /d MON /st 07:00 /tn ParcelMonitor /tr "python %CD%\\src\\monitor.py --no-open"

Limits worth knowing: RADD flags disturbance only in primary humid forest and from 2019;
the most recent weeks are provisional because alerts get confirmed with a delay; the
quantum part is a noiseless simulation, not a hardware run.
"""

import argparse
import csv
import json
import webbrowser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

import snapshot as snap
from config import CELL_HA, CELL_KM, OUTPUT_DIR
from geo import init_earth_engine, square_bounds
from select_parcels import SEED

RADD = 'projects/radar-wur/raddalert/v1'      # RADD forest disturbance alerts
GEOGRAPHY = 'sa'                              # South America tile set
MONITOR_START = date(2020, 1, 1)              # losses are counted from this date (RADD alerts here start in 2020)
SCALE_M = 10                                  # RADD's native pixel size
PROVISIONAL_WEEKS = 8                         # newest weeks can still be revised
BOOTSTRAP = 2000

# Project facts supplied by the team (check against the project documents).
PROJECT_START = date(2017, 12, 14)            # ALGAR start date
PROJECT_END = date(2047, 12, 14)              # 30-year project life
CLAIMED_TCO2E_PER_YEAR = 1_004_626.0          # projected average avoided emissions per year

# The project's own 30-year projection. The carbon density is derived from it, so it is the
# value implied by the project's estimate, not an independent measurement.
PROJECT_AVOIDED_HA = 82_994.27                # projected avoided deforestation over the project life
PROJECT_AVOIDED_TCO2E = 30_138_794.0          # projected avoided emissions over the project life
CARBON_TCO2E_PER_HA = PROJECT_AVOIDED_TCO2E / PROJECT_AVOIDED_HA     # 363.1 tCO2e/ha = 99.0 tC/ha

RAW_JSON = OUTPUT_DIR / 'monitoring_raw.json'
LOG_CSV = OUTPUT_DIR / 'monitoring_log.csv'
RUNS_CSV = OUTPUT_DIR / 'monitoring_runs.csv'
DASHBOARD_HTML = OUTPUT_DIR / 'monitoring_dashboard.html'


# --------------------------------------------------------------------- data download
def week_start(week):
    """Monday of a week index (weeks counted from 2000-01-01, shifted to start on Monday)."""
    return date(2000, 1, 1) + timedelta(days=7 * int(week) + 2)


def fetch_weekly_losses(squares, since, batch=60):
    """Confirmed RADD alert area (ha) per square and week. squares: {key: (lon, lat)}."""
    ee = init_earth_engine()

    images = (ee.ImageCollection(RADD).filter(ee.Filter.eq('layer', 'alerts'))
              .filter(ee.Filter.eq('geography', GEOGRAPHY)).sort('system:time_end'))
    mosaic = images.mosaic()                                  # newest version on top
    day = mosaic.select('Date')                               # YYDOY
    since_code = (since.year - 2000) * 1000 + since.timetuple().tm_yday
    mask = mosaic.select('Alert').gte(3).And(day.gte(since_code))
    year = day.divide(1000).floor()
    doy = day.mod(1000)
    days = year.multiply(365).add(year.add(3).divide(4).floor()).add(doy).subtract(1)
    week = days.subtract(2).divide(7).floor()                 # Monday-based week index
    data = (ee.Image.pixelArea().divide(1e4).rename('ha').addBands(week.rename('week'))
            .updateMask(mask))
    reducer = ee.Reducer.sum().group(groupField=1, groupName='week')

    keys, losses = list(squares), {}
    for start in range(0, len(keys), batch):
        part = keys[start:start + batch]
        features = [ee.Feature(ee.Geometry.Rectangle(list(square_bounds(*squares[k], CELL_KM)), None, False),
                               {'key': k}) for k in part]
        out = data.reduceRegions(ee.FeatureCollection(features), reducer, SCALE_M,
                                 tileScale=4).getInfo()
        for feature in out['features']:
            props = feature['properties']
            losses[props['key']] = {int(g['week']): float(g['sum'])
                                    for g in props.get('groups', [])}
        print(f'  downloaded {min(start + batch, len(keys))} of {len(keys)} squares')
    return losses


# ---------------------------------------------------------------------------- analysis
def build_series(losses, parcels, project_cells, since):
    """Weekly cumulative loss for the project and each parcel, from `since` to the latest alert."""
    all_weeks = [w for per in losses.values() for w in per]
    first = (since - date(2000, 1, 1)).days // 7
    last = max(all_weeks) if all_weeks else first
    weeks = list(range(first, last + 1))
    index = {w: i for i, w in enumerate(weeks)}

    cache = {}                                                # key -> cumulative hectares by week

    def cumulative(key):
        series = np.zeros(len(weeks))
        for w, ha in losses.get(key, {}).items():
            if w in index:
                series[index[w]] += ha
        cache[key] = np.cumsum(series)
        return cache[key]

    parcel_forest = np.array([p['forest_start'] * CELL_HA for p in parcels])
    parcel_loss = np.array([cumulative(f"parcel:{p['id']}") for p in parcels])    # parcels x weeks
    project_loss = sum(cumulative(f"project:{c['id']}") for c in project_cells)
    project_forest = float(sum(c['forest_start'] for c in project_cells) * CELL_HA)
    return {'weeks': weeks, 'parcel_rate': (parcel_loss / parcel_forest[:, None]).T,   # weeks x parcels
            'project_loss_ha': project_loss, 'project_forest_ha': project_forest, 'cum': cache,
            'project_rate': project_loss / project_forest}


def to_tco2(hectares):
    return hectares * CARBON_TCO2E_PER_HA


def estimators(rates, project_rate, project_forest, rng, trials):
    """Avoided-loss intervals from the three estimators at one checkpoint."""
    from amplitude_estimation import (SCHEDULE, build_a, evaluations, good_probabilities,
                                      quantum_estimates)
    top = float(rates.max())
    if top <= 0:
        return None
    values = rates / top
    a = float(values.mean())
    budget = evaluations(len(SCHEDULE))
    to_ha = lambda rate: (rate - project_rate) * project_forest
    probs, _, _ = good_probabilities(build_a(values), SCHEDULE)
    qae = quantum_estimates(probs, len(SCHEDULE), 0, 0, 0.0, rng, trials) * top
    mc = rng.binomial(budget, a, size=trials) / budget * top
    interval = lambda x: [float(to_ha(np.percentile(x, 2.5))), float(to_ha(np.percentile(x, 97.5)))]
    return {'evaluations': int(budget), 'trials': trials,
            'mc': {'median': float(to_ha(np.median(mc))), 'interval': interval(mc)},
            'qae': {'median': float(to_ha(np.median(qae))), 'interval': interval(qae)}}


def analyse(series, qae_every, trials, rng, since):
    """One record per week: loss rates, avoided loss with a bootstrap interval, CO2."""
    rates, n_weeks = series['parcel_rate'], len(series['weeks'])
    n_parcels = rates.shape[1]
    picks = rng.integers(0, n_parcels, size=(BOOTSTRAP, n_parcels))
    records = []
    for i, week in enumerate(series['weeks']):
        row = rates[i]
        boot = row[picks].mean(axis=1)                         # bootstrap means of the parcels
        to_ha = lambda rate: (rate - series['project_rate'][i]) * series['project_forest_ha']
        avoided = float(to_ha(row.mean()))
        window_years = ((week_start(week) + timedelta(days=7)) - since).days / 365.25
        record = {'week_start': week_start(week).isoformat(),
                  'provisional': bool(i >= n_weeks - PROVISIONAL_WEEKS),
                  'project_loss_ha': float(series['project_loss_ha'][i]),
                  'project_rate': float(series['project_rate'][i]),
                  'parcel_rate_mean': float(row.mean()),
                  'parcel_rate_p05': float(np.percentile(row, 5)),
                  'parcel_rate_p95': float(np.percentile(row, 95)),
                  'avoided_ha': avoided,
                  'avoided_ci_lo': float(to_ha(np.percentile(boot, 2.5))),
                  'avoided_ci_hi': float(to_ha(np.percentile(boot, 97.5))),
                  'avoided_tco2': float(to_tco2(avoided)),
                  'claim_tco2': CLAIMED_TCO2E_PER_YEAR * window_years, 'estimators': None}
        if qae_every and (i % qae_every == 0 or i == n_weeks - 1):
            record['estimators'] = estimators(row, series['project_rate'][i],
                                              series['project_forest_ha'], rng, trials)
        records.append(record)
    return records


# ----------------------------------------------------------------------------- outputs
def write_log(records):
    columns = ['week_start', 'provisional', 'project_loss_ha', 'project_rate',
               'parcel_rate_mean', 'parcel_rate_p05', 'parcel_rate_p95', 'avoided_ha',
               'avoided_ci_lo', 'avoided_ci_hi', 'avoided_tco2', 'claim_tco2']
    with open(LOG_CSV, 'w', newline='') as fh:
        writer = csv.writer(fh)
        writer.writerow(columns + ['mc_lo', 'mc_hi', 'qae_lo', 'qae_hi'])
        for r in records:
            e = r['estimators']
            extra = ([e['mc']['interval'][0], e['mc']['interval'][1],
                      e['qae']['interval'][0], e['qae']['interval'][1]] if e else ['', '', '', ''])
            writer.writerow([r[c] for c in columns] + extra)


def append_run(snapshot, records, retrieved):
    new = not RUNS_CSV.exists()
    last = records[-1]
    with open(RUNS_CSV, 'a', newline='') as fh:
        writer = csv.writer(fh)
        if new:
            writer.writerow(['run_utc', 'snapshot_sha256', 'data_retrieved_utc', 'latest_week',
                             'weeks', 'avoided_ha', 'ci_lo', 'ci_hi', 'avoided_tco2'])
        writer.writerow([datetime.now(timezone.utc).isoformat(timespec='seconds'),
                         snapshot['sha256'], retrieved, last['week_start'], len(records),
                         round(last['avoided_ha'], 3), round(last['avoided_ci_lo'], 3),
                         round(last['avoided_ci_hi'], 3), round(last['avoided_tco2'], 1)])


def spatial(snapshot, series):
    """Cumulative loss (ha) by week for every square, for the dashboard map."""
    def entry(key, item):
        return {'lon': round(item['lon'], 5), 'lat': round(item['lat'], 5),
                'forest_ha': round(item['forest_start'] * CELL_HA, 1),
                'cum': [round(float(x), 2) for x in series['cum'][key]]}
    return {'cell_km': CELL_KM,
            'parcels': [dict(entry(f"parcel:{p['id']}", p), id=p['id']) for p in snapshot['parcels']],
            'cells': [entry(f"project:{c['id']}", c) for c in snapshot['project_cells']]}


def write_dashboard(snapshot, records, series, retrieved, since):
    payload = {
        'records': records, 'retrieved': retrieved, 'since': since.isoformat(),
        'snapshot': {'sha256': snapshot['sha256'], 'created_at': snapshot['created_at'],
                     'parcels': len(snapshot['parcels']),
                     'project_cells': len(snapshot['project_cells'])},
        'project_forest_ha': series['project_forest_ha'],
        'spatial': spatial(snapshot, series),
        'project': {'start': PROJECT_START.isoformat(), 'end': PROJECT_END.isoformat(),
                    'claim_per_year': CLAIMED_TCO2E_PER_YEAR,
                    'projected_ha': PROJECT_AVOIDED_HA, 'projected_tco2e': PROJECT_AVOIDED_TCO2E,
                    'tco2e_per_ha': CARBON_TCO2E_PER_HA},
        'provisional_weeks': PROVISIONAL_WEEKS}
    html = (Path(__file__).resolve().parent / 'monitor_dashboard.html').read_text(encoding='utf-8')
    DASHBOARD_HTML.write_text(html.replace('__DATA__', json.dumps(payload)), encoding='utf-8')


# --------------------------------------------------------------------------------- main
def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--offline', action='store_true', help='reuse the last download')
    parser.add_argument('--since', type=date.fromisoformat, default=MONITOR_START,
                        help='start of the monitoring window (YYYY-MM-DD)')
    parser.add_argument('--qae-every', type=int, default=4,
                        help='run the QAE comparison every N weeks (0 = never)')
    parser.add_argument('--trials', type=int, default=200, help='repeats for the QAE/Monte Carlo spread')
    parser.add_argument('--no-open', action='store_true', help='do not open the dashboard')
    args = parser.parse_args()

    snapshot = snap.load()                                    # exits if the hash is wrong
    print(f"Snapshot {snapshot['sha256'][:12]} verified: {len(snapshot['parcels'])} parcels, "
          f"{len(snapshot['project_cells'])} project cells")
    squares = {f"parcel:{p['id']}": (p['lon'], p['lat']) for p in snapshot['parcels']}
    squares.update({f"project:{c['id']}": (c['lon'], c['lat']) for c in snapshot['project_cells']})

    if args.offline and RAW_JSON.exists():
        raw = json.loads(RAW_JSON.read_text())
        if raw['since'] != args.since.isoformat():
            raise SystemExit(f"Saved download starts {raw['since']}; run without --offline "
                             f"for --since {args.since}.")
        losses = {k: {int(w): v for w, v in per.items()} for k, per in raw['losses'].items()}
        retrieved = raw['retrieved']
        print(f'Using the download from {retrieved}')
    else:
        print(f'Downloading RADD alerts since {args.since} for {len(squares)} squares...')
        losses = fetch_weekly_losses(squares, args.since)
        retrieved = datetime.now(timezone.utc).isoformat(timespec='seconds')
        RAW_JSON.write_text(json.dumps({'since': args.since.isoformat(), 'retrieved': retrieved,
                                        'losses': losses}))

    series = build_series(losses, snapshot['parcels'], snapshot['project_cells'], args.since)
    rng = np.random.default_rng(SEED)
    print(f"Analysing {len(series['weeks'])} weeks (QAE checkpoint every {args.qae_every})...")
    records = analyse(series, args.qae_every, args.trials, rng, args.since)

    write_log(records)
    append_run(snapshot, records, retrieved)
    write_dashboard(snapshot, records, series, retrieved, args.since)
    last = records[-1]
    print(f"\nLatest week {last['week_start']}: project loss {last['project_rate']:.3%}, "
          f"parcels {last['parcel_rate_mean']:.3%}")
    print(f"Avoided loss {last['avoided_ha']:,.0f} ha "
          f"(95% interval {last['avoided_ci_lo']:,.0f} to {last['avoided_ci_hi']:,.0f}) "
          f"= {last['avoided_tco2']:,.0f} tCO2e at {CARBON_TCO2E_PER_HA:.0f} tCO2e/ha")
    print(f'Saved {LOG_CSV}, {RUNS_CSV} and {DASHBOARD_HTML}')
    if not args.no_open:
        webbrowser.open(DASHBOARD_HTML.resolve().as_uri())


if __name__ == '__main__':
    main()
