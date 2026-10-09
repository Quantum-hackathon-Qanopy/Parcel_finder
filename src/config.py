"""Settings and file locations shared by several scripts. Edit the project settings here.

Settings that belong to one step stay at the top of that step's script:
    example.py            candidate search (number of candidates, spacing, ring test)
    select_parcels.py     the selection (K, pool size, weights, QAOA size)
    monitor.py            the monitoring window, project dates and claim
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # repository root
DATA_DIR = ROOT / 'data'                                # input files
OUTPUT_DIR = ROOT / 'outputs'                           # everything the scripts generate
OUTPUT_DIR.mkdir(exist_ok=True)

# ------------------------------------------------------------------ project settings
EE_PROJECT = 'ultrahack-project'     # your Google Cloud project with Earth Engine enabled
KML_FILE = DATA_DIR / 'ALGAR_limite_fazenda_pacaja_v2_MS_17012019.shp.kml'
PROJECT_START_YEAR = 2017            # ALGAR started 14 Dec 2017; features use data up to 2016
CELL_SIZE_M = 2000                   # parcel side length in metres (2000 m = 400 ha)
BUFFER_M = 10000                     # leakage buffer around the project, in metres
SEARCH_RADIUS_M = 50000              # candidates are searched up to this distance, in metres

CELL_KM = CELL_SIZE_M / 1000
CELL_HA = CELL_KM ** 2 * 100
BUFFER_KM = BUFFER_M / 1000
SEARCH_KM = SEARCH_RADIUS_M / 1000

# ------------------------------------------------------------------------ output files
CANDIDATES_CSV = OUTPUT_DIR / 'candidates.csv'               # written by example.py
PROJECT_CELLS_CSV = OUTPUT_DIR / 'project_cells.csv'
PROFILE_JSON = OUTPUT_DIR / 'project_profile.json'
SELECTED_CSV = OUTPUT_DIR / 'selected_parcels.csv'           # written by select_parcels.py
MATCHES_CSV = OUTPUT_DIR / 'selected_matches.csv'
QAOA_JSON = OUTPUT_DIR / 'qaoa_result.json'
SNAPSHOT_JSON = OUTPUT_DIR / 'snapshot.json'                 # written by snapshot.py
