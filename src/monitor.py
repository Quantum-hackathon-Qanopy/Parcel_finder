"""Near-live weekly monitoring of the project against its frozen comparison parcels.

    python src/monitor.py                  # fetch alerts, update the log, rebuild the dashboard
    python src/monitor.py --offline        # reuse the last download (no Earth Engine)
    python src/monitor.py --since 2025-01-01 --qae-every 8

Steps (pipeline steps 7 and 8):
  1. load the frozen snapshot (snapshot.py) and refuse to run if its hash no longer matches
  2. download weekly forest-disturbance alerts (RADD radar alerts, confirmed class) from Earth
     Engine for each comparison parcel and for the project itself, measured inside its exact
     boundary (the project's grid cells are only used for the map)
  3. turn them into cumulative loss rates and the avoided loss
         (mean parcel loss rate - project loss rate) x project forest area
  4. give the uncertainty two ways: a classical bootstrap over the parcels, and, at
     checkpoints, a simulated quantum amplitude estimate next to a Monte Carlo estimate with
     the same number of evaluations (see amplitude_estimation.py)
  5. convert to tons of CO2e with the carbon density implied by the project's own projection
     and compare with the claim, pro-rated over the monitoring window
  6. work out the NFT status (nft_status.py) and, with --publish-nft, publish it (publish_nft.py)
  7. write outputs/monitoring_log.csv, append a run record to outputs/monitoring_runs.csv and
     rebuild outputs/monitoring_dashboard.html

To run it every Monday at 07:00 on Windows, in a terminal from the repository root:

    schtasks /create /sc weekly /d MON /st 07:00 /tn ParcelMonitor /tr "python %CD%\\src\\run_pipeline.py --publish"

(run_pipeline.py runs this script and then publishes the NFT status; drop --publish to
only monitor and do a dry run of the publish)

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

import nft_status
import snapshot as snap
from config import (BUFFER_KM, CARBON_SOURCE, CARBON_TCO2E_PER_HA, CELL_HA, CELL_KM,
                    CLAIMED_TCO2E_PER_YEAR, DASHBOARD_HTML, OUTPUT_DIR, OUTPUT_ROOT, PROJECT, PROJECTS,
                    PROJECT_AVOIDED_HA, PROJECT_AVOIDED_TCO2E, PROJECT_END, PROJECT_NAME,
                    PROJECT_SLUG, PROJECT_START, RADD_GEOGRAPHY, SEARCH_KM, read_registry,
                    write_project)
from geo import (RADD_ASSET as RADD, detect_radd_geography, grown_boundary, init_earth_engine,
                 load_boundary, load_protected, square_bounds)
from parcel_finder import HANSEN, boundary_geometry
from select_parcels import SEED

MONITOR_START = max(date(2020, 1, 1), PROJECT_START)   # RADD alerts start in 2020; nothing before the project starts counts
SCALE_M = 10                                  # RADD's native pixel size
PROVISIONAL_WEEKS = 8                         # newest weeks can still be revised
BOOTSTRAP = 2000
FOREST_THRESHOLD = 30                         # canopy cover (%) that counts as forest, as in FindParcels

# The project's facts (dates, claim, projection, carbon density) are in config.py. The carbon
# density is derived from the project's own projection, so it is the value implied by the
# project's estimate, not an independent measurement. Without one, only hectares are reported.

RAW_JSON = OUTPUT_DIR / 'monitoring_raw.json'
LOG_CSV = OUTPUT_DIR / 'monitoring_log.csv'
RUNS_CSV = OUTPUT_DIR / 'monitoring_runs.csv'
PAYLOAD_JSON = OUTPUT_DIR / 'dashboard_payload.json'      # this project's part of the dashboard


# --------------------------------------------------------------------- data download
def week_start(week):
    """Monday of a week index (weeks counted from 2000-01-01, shifted to start on Monday)."""
    return date(2000, 1, 1) + timedelta(days=7 * int(week) + 2)


def boundary_forest_ha(ee, geometry):
    """Forest inside the project boundary at the project start, in hectares: Hansen canopy of at
    least FOREST_THRESHOLD %, after removing loss before the start year (as FindParcels does)."""
    hansen = ee.Image(HANSEN)
    loss_year = hansen.select('lossyear')
    lost_before = loss_year.gt(0).And(loss_year.lte(PROJECT_START.year - 2001))
    forest = hansen.select('treecover2000').where(lost_before, 0).gte(FOREST_THRESHOLD)
    area = ee.Image.pixelArea().divide(1e4).updateMask(forest)
    return float(area.reduceRegion(ee.Reducer.sum(), geometry, 30, maxPixels=1e11,
                                   tileScale=8).getInfo()['area'])


def fetch_weekly_losses(squares, since, geography, batch=60):
    """Confirmed RADD alert area (ha) per week for each square, and for the project's exact
    boundary under the key 'project:boundary'. squares: {key: (lon, lat)}. Also returns the forest
    area inside the boundary. Returns (losses, forest hectares)."""
    ee = init_earth_engine()

    images = (ee.ImageCollection(RADD).filter(ee.Filter.eq('layer', 'alerts'))
              .filter(ee.Filter.eq('geography', geography)).sort('system:time_end'))
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

    geometry = boundary_geometry(load_boundary())             # the project's true shape
    out = data.reduceRegions(ee.FeatureCollection([ee.Feature(geometry, {'key': 'project:boundary'})]),
                             reducer, SCALE_M, tileScale=8).getInfo()
    props = out['features'][0]['properties']
    losses['project:boundary'] = {int(g['week']): float(g['sum']) for g in props.get('groups', [])}
    forest_ha = boundary_forest_ha(ee, geometry)
    print(f'  project boundary: {forest_ha:,.0f} ha of forest at the start')
    return losses, forest_ha


# ---------------------------------------------------------------------------- analysis
def build_series(losses, parcels, project_cells, since, project_forest_ha):
    """Weekly cumulative loss for the project and each parcel, from `since` to the latest alert.
    The project's loss is measured inside its exact boundary against `project_forest_ha`; the
    project cells are only kept for the map."""
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
    for c in project_cells:
        cumulative(f"project:{c['id']}")
    project_loss = cumulative('project:boundary')
    project_forest = float(project_forest_ha)
    return {'weeks': weeks, 'parcel_rate': (parcel_loss / parcel_forest[:, None]).T,   # weeks x parcels
            'project_loss_ha': project_loss, 'project_forest_ha': project_forest, 'cum': cache,
            'project_rate': project_loss / project_forest}


def to_tco2(hectares):
    """tCO2e for a number of hectares, or None when the project has no carbon density."""
    return None if CARBON_TCO2E_PER_HA is None else float(hectares * CARBON_TCO2E_PER_HA)


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
                  'avoided_tco2': to_tco2(avoided),
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
                         round(last['avoided_ci_hi'], 3),
                         '' if last['avoided_tco2'] is None else round(last['avoided_tco2'], 1)])


def spatial(snapshot, series):
    """Cumulative loss (ha) by week for every square, for the dashboard map."""
    def entry(key, item):
        return {'lon': round(item['lon'], 5), 'lat': round(item['lat'], 5),
                'forest_ha': round(item['forest_start'] * CELL_HA, 1),
                'cum': [round(float(x), 2) for x in series['cum'][key]]}
    return {'cell_km': CELL_KM,
            'parcels': [dict(entry(f"parcel:{p['id']}", p), id=p['id']) for p in snapshot['parcels']],
            'cells': [entry(f"project:{c['id']}", c) for c in snapshot['project_cells']]}


def project_facts():
    return {'slug': PROJECT_SLUG, 'name': PROJECT_NAME,
            'start': PROJECT_START.isoformat(), 'end': PROJECT_END.isoformat(),
            'registered_ha': PROJECT['registered_ha'],
            'claim_per_year': CLAIMED_TCO2E_PER_YEAR,
            'projected_ha': PROJECT_AVOIDED_HA, 'projected_tco2e': PROJECT_AVOIDED_TCO2E,
            'tco2e_per_ha': CARBON_TCO2E_PER_HA, 'density_source': CARBON_SOURCE}


def write_latest(snapshot, records, since):
    """The newest week and the project facts, for the NFT publisher (publish_nft.py)."""
    latest = {'record': {k: v for k, v in records[-1].items() if k != 'estimators'},
              'since': since.isoformat(), 'snapshot_sha256': snapshot['sha256'],
              'project': project_facts()}
    nft_status.LATEST_JSON.write_text(json.dumps(latest, indent=1))
    return latest


def write_dashboard(snapshot, records, series, retrieved, since, latest):
    payload = {
        'records': records, 'retrieved': retrieved, 'since': since.isoformat(),
        'snapshot': {'sha256': snapshot['sha256'], 'created_at': snapshot['created_at'],
                     'parcels': len(snapshot['parcels']),
                     'project_cells': len(snapshot['project_cells'])},
        'project_forest_ha': series['project_forest_ha'],
        'spatial': spatial(snapshot, series),
        'boundary': boundary_payload(), 'zones': zones_payload(), 'protected': protected_payload(),
        'project': project_facts(),
        'nft': nft_block(latest),
        'provisional_weeks': PROVISIONAL_WEEKS}
    PAYLOAD_JSON.write_text(json.dumps(payload), encoding='utf-8')
    render_dashboard()


def boundary_payload():
    """The project outline for the dashboard map: rings of [lat, lon], lightly simplified
    (about 10 m) so large boundary files stay small."""
    from shapely.geometry import Polygon
    rings = []
    for ring in load_boundary():
        outline = Polygon(ring).simplify(0.0001, preserve_topology=True)
        if not outline.is_empty and outline.geom_type == 'Polygon':
            rings.append([[round(y, 5), round(x, 5)] for x, y in outline.exterior.coords])
    return rings


def zones_payload():
    """The leakage buffer and the outer search limit for the dashboard map: rings of [lat, lon],
    simplified to about 50 m. Candidates lie between the two; nothing is chosen inside the buffer."""
    from shapely.geometry import Polygon

    def rings(km):
        out = []
        for ring in grown_boundary(km):
            outline = Polygon(ring).simplify(0.0005, preserve_topology=True)
            if not outline.is_empty and outline.geom_type == 'Polygon':
                out.append([[round(y, 5), round(x, 5)] for x, y in outline.exterior.coords])
        return out
    return {'buffer_km': BUFFER_KM, 'search_km': SEARCH_KM,
            'buffer': rings(BUFFER_KM), 'search': rings(SEARCH_KM)}


def protected_payload():
    """Protected areas near the project for the dashboard map, as simplified GeoJSON. They are
    downloaded and cached on first use. None means they could not be fetched (no Earth Engine
    login); an empty collection means there are none in the search area."""
    from shapely.geometry import mapping, shape
    data = load_protected()
    if data is None:
        return None
    features = []
    for feature in data['features']:
        geometry = shape(feature['geometry']).simplify(0.0005, preserve_topology=True)
        if not geometry.is_empty:
            features.append({'type': 'Feature', 'properties': {'name': feature['properties'].get('name')},
                             'geometry': mapping(geometry)})
    return {'type': 'FeatureCollection', 'features': features}


def nft_block(latest):
    return {'status': nft_status.build_status(latest), 'published': nft_status.load_state(),
            'contract': nft_status.CONTRACT_ADDRESS, 'explorer': nft_status.EXPLORER_URL}


def render_dashboard():
    """Write the one dashboard page for every project that has monitoring results."""
    payloads = []
    for path in sorted(OUTPUT_ROOT.glob('*/dashboard_payload.json')):
        payloads.append(json.loads(path.read_text(encoding='utf-8')))
    for payload in payloads:                                  # artwork of the five statuses, for the NFT panel
        if payload['nft']['status']:
            payload['nft']['status']['images'] = nft_status.level_images()
    order = list(PROJECTS)                                    # tabs follow the order in config.py
    payloads.sort(key=lambda p: order.index(p['project']['slug']) if p['project']['slug'] in order else len(order))
    html = (Path(__file__).resolve().parent / 'monitor_dashboard.html').read_text(encoding='utf-8')
    DASHBOARD_HTML.write_text(html.replace('__DATA__', json.dumps({'projects': payloads})),
                              encoding='utf-8')


def refresh_dashboard():
    """Redraw the dashboard with the current NFT state, without redoing the monitoring."""
    latest = nft_status.load_latest()
    latest['project'] = project_facts()                       # pick up changed project facts too
    nft_status.LATEST_JSON.write_text(json.dumps(latest, indent=1))
    payload = json.loads(PAYLOAD_JSON.read_text(encoding='utf-8'))
    payload['project'] = project_facts()
    payload['boundary'] = boundary_payload()
    payload['zones'] = zones_payload()
    payload['protected'] = protected_payload()
    payload['nft'] = nft_block(latest)
    PAYLOAD_JSON.write_text(json.dumps(payload), encoding='utf-8')
    render_dashboard()


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
    parser.add_argument('--publish-nft', action='store_true',
                        help='after monitoring, publish the status to the NFT (see publish_nft.py)')
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
        if 'project:boundary' not in raw['losses']:
            raise SystemExit('The saved download predates measuring inside the exact boundary; run '
                             'without --offline once.')
        losses = {k: {int(w): v for w, v in per.items()} for k, per in raw['losses'].items()}
        forest_ha = raw['boundary_forest_ha']
        retrieved = raw['retrieved']
        print(f'Using the download from {retrieved}')
    else:
        print(f'Downloading RADD alerts since {args.since} for {len(squares)} squares...')
        geography = RADD_GEOGRAPHY
        if geography is None:                                     # first run for this project
            geography, counts = detect_radd_geography(load_boundary())
            if geography is None:
                raise SystemExit(f"RADD has no alerts at '{PROJECT_SLUG}' (alert pixels per tile set: "
                                 f'{counts}). It covers humid tropical forest only, so this project '
                                 'cannot be monitored with it.')
            print(f"  RADD tile set detected: '{geography}'")
            entry = read_registry()[PROJECT_SLUG]
            entry['radd_geography'] = geography
            write_project(PROJECT_SLUG, entry)
        losses, forest_ha = fetch_weekly_losses(squares, args.since, geography)
        retrieved = datetime.now(timezone.utc).isoformat(timespec='seconds')
        RAW_JSON.write_text(json.dumps({'since': args.since.isoformat(), 'retrieved': retrieved,
                                        'boundary_forest_ha': forest_ha, 'losses': losses}))

    if not any(sum(per.values()) > 0 for per in losses.values()):
        print(f"\nWARNING: no alerts at all were found for '{PROJECT_SLUG}', neither in the project nor in "
              f"any comparison parcel. RADD may not cover this region, "
              'so zero loss here would mean no data, not no deforestation.\n')
    series = build_series(losses, snapshot['parcels'], snapshot['project_cells'], args.since, forest_ha)
    rng = np.random.default_rng(SEED)
    print(f"Analysing {len(series['weeks'])} weeks (QAE checkpoint every {args.qae_every})...")
    records = analyse(series, args.qae_every, args.trials, rng, args.since)

    write_log(records)
    append_run(snapshot, records, retrieved)
    latest = write_latest(snapshot, records, args.since)
    write_dashboard(snapshot, records, series, retrieved, args.since, latest)
    last = records[-1]
    print(f"\nLatest week {last['week_start']}: project loss {last['project_rate']:.3%}, "
          f"parcels {last['parcel_rate_mean']:.3%}")
    print(f"Avoided loss {last['avoided_ha']:,.0f} ha "
          f"(95% interval {last['avoided_ci_lo']:,.0f} to {last['avoided_ci_hi']:,.0f})")
    status = nft_status.build_status(latest)
    if status is None:
        print(f"No carbon density for '{PROJECT_SLUG}' (set tco2e_per_ha or the projected avoided "
              'hectares and tCO2e in config.py): no tCO2e figure and no NFT status.')
    else:
        print(f"= {last['avoided_tco2']:,.0f} tCO2e at {CARBON_TCO2E_PER_HA:.0f} tCO2e/ha")
        print(f"NFT status: {status['label']} ({status['ratio']:.0%} of the claim to date)")
    print(f'Saved {LOG_CSV}, {RUNS_CSV} and {DASHBOARD_HTML}')
    if args.publish_nft:
        import publish_nft
        publish_nft.publish(dry_run=False)
    if not args.no_open:
        webbrowser.open(DASHBOARD_HTML.resolve().as_uri())


if __name__ == '__main__':
    main()
