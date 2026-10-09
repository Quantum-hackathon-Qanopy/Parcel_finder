"""Settings and file locations shared by several scripts. Edit the projects here.

Every script works on one project at a time. Choose it with --project, for example

    python src/run_pipeline.py --project agroflorestal

The default is algar. Whatever a project generates goes to outputs/<project>/. Settings that
belong to one step stay at the top of that step's script:
    example.py            candidate search (number of candidates, spacing, ring test)
    select_parcels.py     the selection (K, pool size, weights, QAOA size)
    monitor.py            the monitoring window
"""

import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # repository root
DATA_DIR = ROOT / 'data'                                # input files
OUTPUT_ROOT = ROOT / 'outputs'                          # everything the scripts generate
OUTPUT_ROOT.mkdir(exist_ok=True)

# --------------------------------------------------------------------------- projects
# kml: boundary file in data/ (None until one is provided).
# claim_per_year: the project's projected average avoided emissions, tCO2e per year.
# projected_ha / projected_tco2e: the project's own projection of avoided deforestation and
#     avoided emissions over its life; the carbon density is derived from them. Or set
#     tco2e_per_ha directly (then also set density_source, e.g. 'assumption'). With neither, only
#     hectares are reported (no tCO2e, no NFT status).
PROJECTS = {
    'algar': {
        'name': 'ALGAR Pacajá',
        'kml': 'ALGAR_limite_fazenda_pacaja_v2_MS_17012019.shp.kml',
        'start': date(2017, 12, 14), 'end': date(2047, 12, 14),
        'claim_per_year': 1_004_626.0,
        'projected_ha': 82_994.27, 'projected_tco2e': 30_138_794.0, 'tco2e_per_ha': None,
        'registered_ha': None,
    },
    'agroflorestal': {
        'name': 'Projeto Agroflorestal (Verra 5295, Pará, Brazil)',
        'kml': 'PROJETO_AGROFLORESTAL ID 5295.kml',
        'start': date(2021, 10, 10), 'end': date(2061, 10, 9),
        # Verra overview: estimated average annual reductions 94,143 tCO2e (the average achieved in
        # the first monitoring period, 2021-2024). The description also gives a projected
        # 79,648.32 tCO2e per year for the first crediting period; change this value to use that.
        'claim_per_year': 94_143.0,
        'projected_ha': None, 'projected_tco2e': None,
        # ASSUMPTION, not from this project: about 150 tC per ha (above and below ground), a typical
        # value for Amazon forest, times 44/12. Replace by a density derived from the project
        # description (projected avoided hectares and tCO2e) and set 'density_source' to None.
        'tco2e_per_ha': 150.0 * 44 / 12, 'density_source': 'assumption',
        'registered_ha': 23_093,
    },
}
DEFAULT_PROJECT = 'algar'


def _choose_project():
    """The project named by --project (removed from sys.argv so other parsers never see it),
    else the PARCEL_PROJECT environment variable, else the default."""
    slug, rest, args = None, [], sys.argv[1:]
    while args:
        arg = args.pop(0)
        if arg == '--project' and args:
            slug = args.pop(0)
        elif arg.startswith('--project='):
            slug = arg.split('=', 1)[1]
        else:
            rest.append(arg)
    sys.argv[1:] = rest
    slug = slug or os.environ.get('PARCEL_PROJECT') or DEFAULT_PROJECT
    if slug not in PROJECTS:
        raise SystemExit(f"Unknown project '{slug}'. Known projects: {', '.join(PROJECTS)}")
    os.environ['PARCEL_PROJECT'] = slug             # scripts started from here inherit it
    return slug


PROJECT_SLUG = _choose_project()
PROJECT = PROJECTS[PROJECT_SLUG]
OUTPUT_DIR = OUTPUT_ROOT / PROJECT_SLUG
OUTPUT_DIR.mkdir(exist_ok=True)

PROJECT_NAME = PROJECT['name']
KML_FILE = DATA_DIR / PROJECT['kml'] if PROJECT['kml'] else None
PROJECT_START = PROJECT['start']
PROJECT_END = PROJECT['end']
PROJECT_START_YEAR = PROJECT_START.year      # features use data from before this year
CLAIMED_TCO2E_PER_YEAR = PROJECT['claim_per_year']
PROJECT_AVOIDED_HA = PROJECT['projected_ha']
PROJECT_AVOIDED_TCO2E = PROJECT['projected_tco2e']
CARBON_TCO2E_PER_HA = PROJECT['tco2e_per_ha'] or (
    PROJECT_AVOIDED_TCO2E / PROJECT_AVOIDED_HA if PROJECT_AVOIDED_HA and PROJECT_AVOIDED_TCO2E else None)
# where the density comes from: 'projection' (derived from the project's own projection),
# 'assumption' (a general value that is not from the project), or None (no density)
CARBON_SOURCE = None if CARBON_TCO2E_PER_HA is None else (
    PROJECT.get('density_source') or 'projection')


def require_boundary():
    """The project's boundary file, or exit with a clear message if there is none yet."""
    if KML_FILE is None:
        raise SystemExit(f"Project '{PROJECT_SLUG}' has no boundary file yet. Put a KML in data/ "
                         "and set its name under 'kml' in src/config.py.")
    if not KML_FILE.exists():
        raise SystemExit(f'Boundary file not found: {KML_FILE}')
    return KML_FILE


# ------------------------------------------------------------------------------ secrets
def _env_name(name):
    return name.strip().lower().replace(' ', '_')


def _read_env_file():
    """Read .env. Lines look like NAME=value or NAME: value; a name may contain spaces."""
    values = {}
    path = ROOT / '.env'                                   # git ignores this file
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            separators = [i for i in (line.find('='), line.find(':')) if i > 0]
            if separators:
                cut = min(separators)
                values[_env_name(line[:cut])] = line[cut + 1:].strip().strip('"').strip("'")
    return values


_ENV_FILE = _read_env_file()


def get_env(*names, default=None):
    """First value found among the environment variables and the .env file (names are not
    case sensitive; spaces count as underscores). Secrets are only ever read through this,
    never printed."""
    for name in names:
        value = os.environ.get(name) or _ENV_FILE.get(_env_name(name))
        if value:
            return value
    return default


# ------------------------------------------------------------------ analysis settings
EE_PROJECT = 'ultrahack-project'     # your Google Cloud project with Earth Engine enabled
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
