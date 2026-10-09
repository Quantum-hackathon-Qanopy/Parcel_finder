"""Settings and file locations shared by several scripts.

Projects are plain data in data/projects.json, so a new project needs no code. Add one with

    python src/add_project.py NAME --name "..." --kml file.kml --start 2023-05-03 \
        --end 2063-05-02 --claim 690039 --carbon-tc-per-ha 150 --run

Every script works on one project at a time. Choose it with --project, for example

    python src/run_pipeline.py --project agroflorestal

The default is algar. Whatever a project generates goes to outputs/<project>/. Settings that
belong to one step stay at the top of that step's script:
    example.py            candidate search (number of candidates, spacing, ring test)
    select_parcels.py     the selection (K, pool size, weights, QAOA size)
    monitor.py            the monitoring window
"""

import json
import os
import sys
from datetime import date
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):                 # names in other alphabets must not crash a Windows console
    try:
        _stream.reconfigure(errors='replace')
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent          # repository root
DATA_DIR = ROOT / 'data'                                # input files
OUTPUT_ROOT = ROOT / 'outputs'                          # everything the scripts generate
OUTPUT_ROOT.mkdir(exist_ok=True)
DASHBOARD_HTML = OUTPUT_ROOT / 'monitoring_dashboard.html'   # the one dashboard page for all projects

# --------------------------------------------------------------------------- projects
# data/projects.json, one entry per project (add_project.py writes it). Fields:
#   name, kml (boundary file in data/), start and end (YYYY-MM-DD), claim_per_year (the project's
#   projected average avoided emissions, tCO2e per year), registered_ha (optional);
#   the carbon density, one of: carbon_tc_per_ha (an assumption, tons of carbon per hectare, times
#   44/12 to give tCO2e) or projected_ha with projected_tco2e (the project's own projection, from
#   which the density is derived). With neither, only hectares are reported (no tCO2e, no NFT);
#   optional: kml_layer (which folder of a multi-layer KML is the project area),
#   radd_geography ('sa', 'africa', 'asia' or 'ca'; detected when missing),
#   search_radius_m (widened automatically when missing), min_forest_share (0.5 when missing).
PROJECTS_FILE = DATA_DIR / 'projects.json'


def _project(entry):
    """One registry entry in the form the scripts use."""
    density, source = entry.get('tco2e_per_ha'), entry.get('density_source')
    if entry.get('carbon_tc_per_ha'):
        density, source = entry['carbon_tc_per_ha'] * 44.0 / 12.0, 'assumption'
    elif density is None and entry.get('projected_ha') and entry.get('projected_tco2e'):
        density, source = entry['projected_tco2e'] / entry['projected_ha'], 'projection'
    return {'name': entry['name'], 'kml': entry.get('kml'),
            'start': date.fromisoformat(entry['start']), 'end': date.fromisoformat(entry['end']),
            'claim_per_year': float(entry['claim_per_year']),
            'registered_ha': entry.get('registered_ha'),
            'projected_ha': entry.get('projected_ha'), 'projected_tco2e': entry.get('projected_tco2e'),
            'tco2e_per_ha': density, 'density_source': source if density else None,
            'kml_layer': entry.get('kml_layer'),
            'radd_geography': entry.get('radd_geography'),
            'search_radius_m': entry.get('search_radius_m'),
            'min_forest_share': entry.get('min_forest_share')}


def read_registry():
    """The raw registry (as written in the file)."""
    return json.loads(PROJECTS_FILE.read_text(encoding='utf-8')) if PROJECTS_FILE.exists() else {}


def write_project(slug, entry):
    """Add or replace one project in data/projects.json."""
    registry = read_registry()
    registry[slug] = entry
    PROJECTS_FILE.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


PROJECTS = {slug: _project(entry) for slug, entry in read_registry().items()}
DEFAULT_PROJECT = 'algar' if 'algar' in PROJECTS else next(iter(PROJECTS), None)


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
        raise SystemExit(f"Unknown project '{slug}'. Known projects: {', '.join(PROJECTS)}. "
                         'Add one with python src/add_project.py.')
    os.environ['PARCEL_PROJECT'] = slug             # scripts started from here inherit it
    return slug


PROJECT_SLUG = _choose_project()
PROJECT = PROJECTS[PROJECT_SLUG]
OUTPUT_DIR = OUTPUT_ROOT / PROJECT_SLUG
OUTPUT_DIR.mkdir(exist_ok=True)

PROJECT_NAME = PROJECT['name']
RADD_GEOGRAPHY = PROJECT['radd_geography']        # None until detected (monitor.py or add_project.py)
KML_FILE = DATA_DIR / PROJECT['kml'] if PROJECT['kml'] else None
KML_LAYER = PROJECT['kml_layer']                  # None: every layer with polygons is used
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
        raise SystemExit(f"Project '{PROJECT_SLUG}' has no boundary file yet. Add it with "
                         'python src/add_project.py.')
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
MIN_FOREST_SHARE = PROJECT['min_forest_share'] or 0.5       # cells with less forest than this are dropped
SEARCH_RADIUS_SETTING = PROJECT['search_radius_m']          # None: example.py widens the search as needed


def _searched_radius():
    """The radius the last candidate search ended up using (recorded in the profile file)."""
    try:
        return json.loads((OUTPUT_DIR / 'project_profile.json').read_text(encoding='utf-8')).get('search_radius_m')
    except (OSError, ValueError):
        return None


SEARCH_RADIUS_M = SEARCH_RADIUS_SETTING or _searched_radius() or 50_000   # metres

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
PROTECTED_GEOJSON = OUTPUT_DIR / 'protected_areas.geojson'   # protected areas near the project (cached)
