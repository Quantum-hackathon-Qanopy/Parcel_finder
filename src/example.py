"""Find comparison parcels for a project (step 1 of 3; see README).

1. Load the project boundary from a KML file.
2. Find well-spaced parcels that together resemble every part of the project, using
   only information from before the project start year (see parcel_finder.py).
3. Print a short quality report and save the results as CSV/JSON files.
   Then run select_parcels.py and selection_map.py (the map).
"""

import csv
import json

from config import (BUFFER_M, CANDIDATES_CSV, CELL_SIZE_M, MIN_FOREST_SHARE, PROFILE_JSON,
                    PROJECT_CELLS_CSV, PROJECT_START_YEAR, SEARCH_RADIUS_SETTING)
from geo import init_earth_engine, load_boundary
from parcel_finder import FindParcels

# ---------------------------------------------------------------- settings
# Project settings (boundary file, start year, cell size, buffer) are in config.py.
N_CANDIDATES = 200                   # how many parcels to keep
MIN_SPACING_KM = 6.0                 # minimum distance between parcel centres (0 = off)
N_NEAREST = 3                        # project cells each candidate is compared with
RUN_RING_TEST = True                 # print loss rates in rings around the project
SEARCH_RADII_M = (50_000, 100_000, 150_000)   # tried in turn unless the project sets its own radius
MIN_CANDIDATES = 100                 # widen the search if fewer comparison parcels are found


# ------------------------------------------------------------------- steps
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


def search(rings):
    """Find comparison parcels, widening the search radius until enough are found. A radius set
    for the project in data/projects.json is used as it is."""
    trend_years = min(5, PROJECT_START_YEAR - 2002)            # the data starts in 2001
    if trend_years < 1:
        raise SystemExit(f'A project starting in {PROJECT_START_YEAR} has too little earlier data.')
    radii = [SEARCH_RADIUS_SETTING] if SEARCH_RADIUS_SETTING else list(SEARCH_RADII_M)
    best = None
    for radius in radii:
        finder = FindParcels(
            rings, start_year=PROJECT_START_YEAR, buffer_m=BUFFER_M, search_radius_m=radius,
            cell_size_m=CELL_SIZE_M, min_forest_share=MIN_FOREST_SHARE, trend_years=trend_years,
            min_spacing_km=MIN_SPACING_KM, n_nearest=N_NEAREST)
        print(f'Searching for parcels within {radius / 1000:g} km (this can take several minutes)...')
        try:
            finder.fit(N_CANDIDATES)
        except RuntimeError as err:                           # for example too many eligible cells
            if best is None:
                raise
            print(f'  Stopped widening: {err}')
            break
        finder.radius_m = radius
        best = finder
        if len(finder.candidates) >= MIN_CANDIDATES or radius == radii[-1]:
            break
        print(f'  Only {len(finder.candidates)} comparison parcels within {radius / 1000:g} km; '
              'widening the search.')
    return best


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
                   'search_radius_m': finder.radius_m,
                   'project_cell_means': finder.project_means(),
                   'n_project_cells': len(finder.project_cells),
                   'feature_spread': finder.spreads()}, fh, indent=2)


# -------------------------------------------------------------------- main
def main():
    init_earth_engine()
    finder = search(load_boundary())
    if not finder.candidates:
        raise SystemExit('No eligible parcels found. Check the settings (buffer, spacing, forest share).')

    print_report(finder)
    if RUN_RING_TEST:
        print_ring_test(finder)

    save_results(finder)
    print(f'\nSaved {CANDIDATES_CSV}, {PROJECT_CELLS_CSV} and {PROFILE_JSON}')


if __name__ == '__main__':
    main()