"""Find comparison parcels for a project and show them on a map.

1. Load the project boundary from a KML file.
2. Find well-spaced parcels that together resemble every part of the project, using
   only information from before the project start year (see parcel_finder.py).
3. Print a short quality report and save the results as CSV/JSON files.
4. Build an interactive map and open it in the browser.
"""

import csv
import json
import webbrowser
from pathlib import Path

import ee
import geopandas as gpd

from map_view import build_map
from parcel_finder import FindParcels

# ---------------------------------------------------------------- settings
ROOT = Path(__file__).resolve().parent.parent   # repository root
DATA_DIR = ROOT / 'data'
OUTPUT_DIR = ROOT / 'outputs'
OUTPUT_DIR.mkdir(exist_ok=True)
EE_PROJECT = 'ultrahack-project'     # your Google Cloud project with Earth Engine enabled
KML_FILE = DATA_DIR / 'ALGAR_limite_fazenda_pacaja_v2_MS_17012019.shp.kml'
PROJECT_START_YEAR = 2015            # PLACEHOLDER: replace with the real project start year
N_CANDIDATES = 200                   # how many parcels to keep
CELL_SIZE_M = 2000                   # parcel side length in metres (2000 m = 400 ha)
BUFFER_M = 10000                     # leakage buffer around the project, in metres
MIN_SPACING_KM = 6.0                 # minimum distance between parcel centres (0 = off)
N_NEAREST = 3                        # project cells each candidate is compared with
RUN_RING_TEST = True                 # print loss rates in rings around the project

CANDIDATES_CSV = OUTPUT_DIR / 'candidates.csv'
PROJECT_CELLS_CSV = OUTPUT_DIR / 'project_cells.csv'
PROFILE_JSON = OUTPUT_DIR / 'project_profile.json'
MAP_HTML = OUTPUT_DIR / 'demo_map.html'


# ------------------------------------------------------------------- steps
def init_earth_engine():
    """Start Earth Engine; log in first if this machine has no saved login."""
    try:
        ee.Initialize(project=EE_PROJECT)
    except Exception:
        ee.Authenticate()
        ee.Initialize(project=EE_PROJECT)


def load_boundary(kml_file):
    """Outer boundary of the first polygon in the KML, as (lon, lat) pairs."""
    ring = gpd.read_file(kml_file, driver='KML').geometry.iloc[0].exterior.coords
    return [(c[0], c[1]) for c in ring]


def print_report(finder):
    """Summary of the result: how close is the chosen set to the project?"""
    cells = finder.candidates
    sims = [c['properties']['similarity'] for c in cells]
    print(f'Eligible cells: {finder.n_eligible}')
    print(f'Parcels kept: {len(cells)} of {N_CANDIDATES} wanted '
          f'({finder.n_coverage_picks} picked for coverage, the rest by similarity)')
    print(f'Similarity of kept parcels: best {max(sims):.3f}, worst {min(sims):.3f}')
    if len(cells) < N_CANDIDATES:
        print('Fewer parcels than wanted: lower MIN_SPACING_KM or widen the search range.')

    # Average of each feature: project vs chosen parcels, gap in units of spread.
    project, spread = finder.project_means(), finder.spreads()
    print(f"\n{'feature':<14}{'project':>12}{'parcels':>12}{'gap (sd)':>10}")
    for band in finder.band_names:
        mean = sum(c['properties'][band] for c in cells) / len(cells)
        print(f'{band:<14}{project[band]:>12.2f}{mean:>12.2f}'
              f'{(mean - project[band]) / spread[band]:>10.2f}')

    project_disturbed = (finder.project_cells[:, 0] < 0.95).mean()
    parcel_disturbed = sum(c['properties']['forest_start'] < 0.95 for c in cells) / len(cells)
    print(f'Disturbed cells (forest share below 95%): project {project_disturbed:.0%}, '
          f'parcels {parcel_disturbed:.0%}')
    print(f'Project cells with a close analogue among the parcels: {finder.coverage():.0%}')


def print_ring_test(finder):
    """Forest loss per year in rings around the project, before vs after the start."""
    print('\nRing test (forest loss, % of forest per year; protected areas left out):')
    print(f"{'ring (km)':<12}{'before':>9}{'after':>9}{'after/before':>14}")
    for r in finder.ring_loss_report():
        print(f"{r['inner_km']:g}-{r['outer_km']:g}".ljust(12)
              + f"{r['before']:>9.2f}{r['after']:>9.2f}{r['ratio']:>14.2f}")


def save_results(finder):
    """Write the parcels, the project's own cells and a small profile file."""
    bands = finder.band_names
    with open(CANDIDATES_CSV, 'w', newline='') as fh:
        writer = csv.writer(fh)
        writer.writerow(['id', 'lon', 'lat', 'similarity'] + bands)
        for i, cell in enumerate(finder.candidates):
            p = cell['properties']
            writer.writerow([i, p['lon'], p['lat'], p['similarity']] + [p[b] for b in bands])

    with open(PROJECT_CELLS_CSV, 'w', newline='') as fh:
        writer = csv.writer(fh)
        writer.writerow(['id', 'lon', 'lat'] + bands)
        for i, (row, (lon, lat)) in enumerate(zip(finder.project_cells, finder.project_lonlat)):
            writer.writerow([i, lon, lat] + row.tolist())

    with open(PROFILE_JSON, 'w') as fh:
        json.dump({'start_year': PROJECT_START_YEAR, 'bands': bands,
                   'project_cell_means': finder.project_means(),
                   'n_project_cells': len(finder.project_cells),
                   'feature_spread': finder.spreads()}, fh, indent=2)


# -------------------------------------------------------------------- main
def main():
    init_earth_engine()
    finder = FindParcels(
        load_boundary(KML_FILE), start_year=PROJECT_START_YEAR, buffer_m=BUFFER_M,
        cell_size_m=CELL_SIZE_M, min_spacing_km=MIN_SPACING_KM, n_nearest=N_NEAREST)

    print('Searching for parcels (this can take several minutes)...')
    finder.fit(N_CANDIDATES)
    if not finder.candidates:
        raise SystemExit('No eligible parcels found. Check the settings (buffer, spacing, forest share).')

    print_report(finder)
    if RUN_RING_TEST:
        print_ring_test(finder)

    save_results(finder)
    print(f'\nSaved {CANDIDATES_CSV}, {PROJECT_CELLS_CSV} and {PROFILE_JSON}')

    build_map(finder).to_html(MAP_HTML)
    print(f'Saved {MAP_HTML}')
    webbrowser.open(Path(MAP_HTML).resolve().as_uri())


if __name__ == '__main__':
    main()