"""Add a project to the pipeline and run it, with no code to edit.

Interactive (just answer the questions):

    python src/add_project.py

It asks for the boundary file and the project's facts (you can paste the Verra overview text and
it picks the numbers out), checks the project, and then runs the whole pipeline and, if you say
so, publishes the status to the NFT.

Or give everything as arguments:

    python src/add_project.py lomphat --name "Lomphat REDD+" --kml LWS_boundary.kml \\
        --start 2023-05-03 --end 2063-05-02 --claim 690039 --registered-ha 134730 \\
        --carbon-tc-per-ha 150 --run --publish

The project is stored in data/projects.json after checking what would otherwise fail later: the
boundary file (area against the registered area, parts, how well the 2 km grid covers it), that
RADD alerts exist at the place (and which regional tile set), and how much of it counts as forest.
Problems that would make the results meaningless stop it, with the reason; --force adds the
project anyway. With --run it then runs the whole pipeline (candidate search, selection, snapshot,
monitoring); with --publish as well, the last step writes the status to the NFT.

Needed: a name, the boundary, start and end dates and the claim (the project's projected average
avoided emissions per year, tCO2e). For tCO2e figures and an NFT status also give a carbon
density: either --carbon-tc-per-ha (an assumption, tons of carbon per hectare) or --projected-ha
together with --projected-tco2e (the project's own projection over its life). Without one only
hectares are shown.
"""

import argparse
import re
import shutil
import subprocess
import sys
import unicodedata
from argparse import Namespace
from datetime import date, datetime
from pathlib import Path

from config import DATA_DIR, PROJECTS, ROOT, read_registry, write_project
from geo import area_ha, boundary_report, canopy_share, detect_radd_geography, kml_layers, load_boundary

MAX_AREA_GAP = 0.15            # warn when the boundary area differs from the registered one by more
MAX_AREA_RATIO = 3.0          # a boundary this many times larger or smaller than the registered area is refused
MAX_AREA_HA = 1_000_000        # the Earth Engine work cannot handle much more than this
MIN_PROJECT_CELLS = 10         # fewer cells mostly inside the project cannot represent it
MIN_CANOPY_SHARE = 0.05        # below this the project has almost no forest at the 30% threshold
WARN_CANOPY_SHARE = 0.20
DATE_FORMATS = ('%Y-%m-%d', '%d %b %Y', '%d %B %Y', '%d/%m/%Y', '%d.%m.%Y')


# ------------------------------------------------------------------------------ small helpers
def find_kml(name):
    """The boundary file: in data/ or at the given path (quotes from drag and drop are ignored)."""
    name = name.strip().strip('"').strip("'")
    for path in (DATA_DIR / name, Path(name)):
        if path.is_file():
            return path
    raise SystemExit(f'Boundary file not found: {name} (looked in data/ and at the given path).')


def parse_date(text):
    text = text.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError('use a date like 2023-05-03 or 03 May 2023')


def parse_number(text):
    return float(re.sub(r'[,\s]', '', text))


def slugify(text):
    plain = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')   # Pará -> Para
    return re.sub(r'[^a-z0-9]+', '-', plain.lower()).strip('-') or 'project'


def unused_slug(text):
    slug = slugify(text)
    if slug in PROJECTS:
        raise ValueError(f"'{slug}' is taken; choose another")
    return slug


def one_of(*options):
    def check(text):
        if text not in options:
            raise ValueError('type ' + ', '.join(options[:-1]) + ' or ' + options[-1])
        return text
    return check


# -------------------------------------------------------------------- the Verra overview text
OVERVIEW_FIELDS = {
    'claim': ('Estimated Average Annual Emission Reductions and Carbon Dioxide Removals',),
    'registered_ha': ('Acres/Hectares',),
    'start': ('Estimated Project Start Date', 'Crediting Period Duration Start Date'),
    'end': ('Crediting Period Duration End Date',),
    'country': ('Country',), 'region': ('State/Province',), 'city': ('City',),
}


def parse_overview(text):
    """Pull the facts out of a pasted Verra project overview (a label line, then a value line)."""
    lines = [line.strip() for line in text.splitlines()]
    found = {}
    for key, labels in OVERVIEW_FIELDS.items():
        for label in labels:
            for i, line in enumerate(lines[:-1]):
                if line.lower() == label.lower():
                    value = next((v for v in lines[i + 1:] if v), '')
                    if value and value != '-':
                        found[key] = value
                        break
            if key in found:
                break
    project_id = re.search(r'/project/(\d+)', text)
    if project_id:
        found['verra_id'] = project_id.group(1)
    converted = {}
    for key, value in found.items():
        try:
            converted[key] = (parse_date(value) if key in ('start', 'end')
                              else parse_number(value) if key in ('claim', 'registered_ha') else value)
        except ValueError:
            pass
    return converted


# ------------------------------------------------------------------------ the question prompts
def ask(prompt, default=None, parse=str, optional=False):
    """Ask until the answer is valid. Enter accepts the default (or skips an optional question)."""
    suffix = f' [{default}]' if default not in (None, '') else (' (optional, Enter to skip)' if optional else '')
    while True:
        try:
            answer = input(f'{prompt}{suffix}: ').strip()
        except EOFError:
            raise SystemExit('\nNo more input; stopping.')
        if not answer:
            if default not in (None, ''):
                return default
            if optional:
                return None
            print('  An answer is needed.')
            continue
        try:
            return parse(answer)
        except (ValueError, SystemExit) as err:
            print(f'  {err}')


def confirm(prompt, default=False):
    answer = ask(prompt + (' (Y/n)' if default else ' (y/N)'), default='y' if default else 'n',
                 parse=lambda t: t.lower()[:1])
    return answer == 'y'


def read_overview():
    """Read a pasted block of text. It ends at END, after the last Verra line, or at two blank lines."""
    print('  Paste the Verra project overview here, then press Enter. Type END on its own line if it')
    print('  does not finish by itself. Or just press Enter to type the values by hand.')
    lines, blanks, after_buffer = [], 0, False
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip().lower() == 'end':
            break
        if not line.strip():
            blanks += 1
            if blanks >= 2 or not lines:
                break
            continue
        blanks = 0
        lines.append(line)
        if after_buffer:                                  # the value line after the last label
            break
        after_buffer = line.strip().lower().startswith('total buffer contributions')
    return '\n'.join(lines)


def choose_kml():
    used = {entry.get('kml') for entry in read_registry().values()}
    free = sorted((p for p in DATA_DIR.glob('*.kml') if p.name not in used),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if free:
        print('Boundary files in data/ that no project uses yet:')
        for number, path in enumerate(free, 1):
            print(f'  {number}  {path.name}')
    answer = ask('Boundary KML file (a number from the list, a file name in data/, or a path)',
                 default='1' if free else None)
    if answer.isdigit() and 1 <= int(answer) <= len(free):
        return free[int(answer) - 1]
    return find_kml(answer)


def interactive():
    """Ask for everything and return the same settings the command line would give."""
    print('Add a project. Press Ctrl+C at any time to stop.\n')
    kml = choose_kml()
    print()
    facts = {}
    pasted = read_overview()
    if pasted:
        facts = parse_overview(pasted)
        names = {'claim': 'annual claim (tCO2e)', 'registered_ha': 'registered area (ha)', 'start': 'start date',
                 'end': 'end date', 'country': 'country', 'region': 'region', 'verra_id': 'Verra id'}
        print('  Found: ' + (', '.join(f'{names[k]} = {v}' for k, v in facts.items() if k in names) or 'nothing usable'))
    print()
    where = ', '.join(v for v in (facts.get('country'), facts.get('region')) if v)
    default_name = (f"Verra {facts['verra_id']}" + (f' ({where})' if where else '')) if 'verra_id' in facts \
        else kml.stem.replace('_', ' ')
    name = ask('Project name', default_name)
    slug = ask('Short name (used for folders and --project)', slugify(name), parse=unused_slug)
    start = ask('Project start date', facts.get('start'), parse_date)
    end = ask('End of the crediting period', facts.get('end'), parse_date)
    claim = ask("Projected average avoided emissions per year, tCO2e (the project's claim)",
                facts.get('claim'), parse_number)
    registered = ask('Registered project area in hectares', facts.get('registered_ha'), parse_number, optional=True)

    print('\nCarbon density (needed for tCO2e figures and the NFT status):')
    print("  1  The project's own projection: I know its projected avoided hectares and tCO2e")
    print('  2  An assumption: I give tons of carbon per hectare (for example 150)')
    print('  3  None for now: show hectares only, no NFT status')
    choice = ask('Choose 1, 2 or 3', '2', parse=one_of('1', '2', '3'))
    tc = projected_ha = projected_tco2e = None
    if choice == '1':
        projected_ha = ask("Projected avoided deforestation over the project's life, hectares", parse=parse_number)
        projected_tco2e = ask('Projected avoided emissions over its life, tCO2e', parse=parse_number)
    elif choice == '2':
        tc = ask('Assumed carbon density, tons of carbon per hectare', 150, parse_number)
    publish = False
    if choice != '3':
        print('\nThe NFT is written to a public test network. A publish is a permanent transaction.')
        publish = confirm('Publish the result to the NFT when the run finishes?')
    return Namespace(slug=slug, name=name, kml=str(kml), start=start, end=end, claim=claim,
                     registered_ha=registered, carbon_tc_per_ha=tc, projected_ha=projected_ha,
                     projected_tco2e=projected_tco2e, region=None, search_radius_km=None,
                     min_forest_share=None, no_ee=False, force=False, run=True, publish=publish,
                     interactive=True)


# ------------------------------------------------------------------------------ the real work
def process(a):
    slug = a.slug.lower()
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', slug):
        raise SystemExit('The short name may only use letters, digits, - and _.')
    if slug in PROJECTS and not a.force:
        raise SystemExit(f"Project '{slug}' already exists. Use --force to replace it.")
    if (a.projected_ha is None) != (a.projected_tco2e is None):
        raise SystemExit('Give --projected-ha and --projected-tco2e together.')
    problems = []

    kml = find_kml(a.kml)
    print(f'\nChecking the boundary {kml.name} ...')
    layer = getattr(a, 'kml_layer', None)
    layers = kml_layers(kml)
    if not layers:
        raise SystemExit(f'No polygons found in {kml.name}. The boundary has to be a polygon.')
    if len(layers) > 1 and layer is None:
        sizes = {name: area_ha(geometry) for name, geometry in layers.items()}
        print(f'  The file has {len(layers)} layers with polygons: '
              + '; '.join(f'{name} ({size:,.0f} ha)' for name, size in sizes.items()))
        if a.registered_ha:
            best = min(sizes, key=lambda name: abs(sizes[name] / a.registered_ha - 1))
            if abs(sizes[best] / a.registered_ha - 1) <= MAX_AREA_GAP:
                layer = best
                print(f"  Using the layer '{layer}', whose area matches the registered area.")
        if layer is None:
            print('  Using all of them together (use --kml-layer NAME to pick one).')
    report = boundary_report(kml, layer=layer)
    print(f"  {report['area_ha']:,.0f} ha in {report['polygons']} polygon(s), {report['holes']} holes, "
          f"{report['vertices']:,} vertices")
    print(f"  2 km cells mostly (90%) inside: {report['cells_mostly_inside']}, covering "
          f"{report['cover_by_those_cells']:.0%} of the project")
    if a.registered_ha:
        ratio = report['area_ha'] / a.registered_ha
        if ratio > MAX_AREA_RATIO or ratio < 1 / MAX_AREA_RATIO:
            problems.append(f"the boundary is {report['area_ha']:,.0f} ha but the registered area is "
                            f"{a.registered_ha:,.0f} ha ({ratio:,.1f} times). It is probably a reference "
                            'region or project zone, not the project area. Use the project-area file')
        elif abs(ratio - 1) > MAX_AREA_GAP:
            print(f"  WARNING: the boundary is {report['area_ha']:,.0f} ha but the registered area is "
                  f"{a.registered_ha:,.0f} ha. Is this the right file?")
    if report['area_ha'] > MAX_AREA_HA:
        problems.append(f"the boundary is {report['area_ha']:,.0f} ha, above the {MAX_AREA_HA:,} ha the "
                        'pipeline can handle')
    if report['cells_mostly_inside'] < MIN_PROJECT_CELLS:
        problems.append(f"only {report['cells_mostly_inside']} grid cells lie mostly inside the boundary "
                        f"(need {MIN_PROJECT_CELLS}); a boundary of thin fragments cannot be matched")

    region = a.region
    if not a.no_ee:
        rings = load_boundary(kml, layer)
        print('Checking alert coverage and forest cover with Earth Engine ...')
        if region is None:
            region, counts = detect_radd_geography(rings)
            print(f'  RADD alert pixels per tile set: {counts}')
            if region is None:
                problems.append('RADD has no alerts at this place (it covers humid tropical forest), so '
                                'there is no weekly loss data to monitor with')
            else:
                print(f"  RADD tile set: '{region}'")
        share = canopy_share(rings)
        print(f'  area with tree cover of at least 30%: {share:.0%}')
        if share < MIN_CANOPY_SHARE:
            problems.append(f'only {share:.0%} of the area counts as forest at the 30% canopy threshold '
                            '(open woodland), so there is almost nothing to measure')
        elif share < WARN_CANOPY_SHARE:
            print('  WARNING: little of the area counts as forest, so the monitored forest will be small.')
    if problems:
        print('\nProblems:')
        for problem in problems:
            print('  -', problem)
    if getattr(a, 'check_only', False):
        print('\nCheck only, nothing was added. ' + ('This project would be refused (unless added with --force).'
                                                    if problems else 'This project would be accepted.'))
        return
    if problems:
        if getattr(a, 'interactive', False):
            if not confirm('Add the project anyway?'):
                raise SystemExit('Not added.')
        elif not a.force:
            raise SystemExit('Not added. Use --force to add it anyway.')

    entry = {'name': a.name, 'kml': kml.name, 'start': a.start.isoformat(), 'end': a.end.isoformat(),
             'claim_per_year': a.claim}
    for key, value in (('registered_ha', a.registered_ha), ('carbon_tc_per_ha', a.carbon_tc_per_ha),
                       ('projected_ha', a.projected_ha), ('projected_tco2e', a.projected_tco2e),
                       ('kml_layer', layer), ('radd_geography', region), ('min_forest_share', a.min_forest_share),
                       ('search_radius_m', a.search_radius_km and a.search_radius_km * 1000)):
        if value is not None:
            entry[key] = value
    if kml.parent.resolve() != DATA_DIR.resolve():                # keep the boundary file with the others
        shutil.copy(kml, DATA_DIR / kml.name)
        print(f'Copied {kml.name} into data/')
    write_project(slug, entry)
    has_density = a.carbon_tc_per_ha or (a.projected_ha and a.projected_tco2e)
    print(f"\nAdded '{slug}' to data/projects.json"
          + ('' if has_density else ' (no carbon density: hectares only, no NFT status)'))

    publish = a.publish and has_density
    if a.publish and not has_density:
        print('Not publishing: there is no carbon density, so no status to publish.')
    command = [sys.executable, str(ROOT / 'src' / 'run_pipeline.py'), '--project', slug, '--full']
    shown = ['python src/run_pipeline.py', '--project', slug, '--full']
    if publish:
        command.append('--publish')
        shown.append('--publish')
    if getattr(a, 'interactive', False) and a.run:
        print('\nNext the whole pipeline runs: candidate search, selection, snapshot and monitoring.')
        print('It takes about 20 to 40 minutes' + (' and then publishes the NFT.' if publish else '.'))
        a.run = confirm('Run it now?', default=True)
    if not a.run:
        print('Run the whole pipeline later with:\n  ' + ' '.join(shown))
        return
    print('\nRunning the pipeline (this takes a while) ...')
    raise SystemExit(subprocess.run(command, cwd=ROOT).returncode)


def main():
    if len(sys.argv) == 1:
        process(interactive())
        return
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('slug', help='short name for the project, e.g. lomphat')
    parser.add_argument('--name', required=True, help='project name shown on the dashboard')
    parser.add_argument('--kml', required=True, help='boundary file (in data/ or a path to copy)')
    parser.add_argument('--start', type=parse_date, required=True, help='project start, YYYY-MM-DD')
    parser.add_argument('--end', type=parse_date, required=True, help='end of the crediting period')
    parser.add_argument('--claim', type=float, required=True,
                        help='projected average avoided emissions per year, tCO2e')
    parser.add_argument('--registered-ha', type=float, help='registered project area, for a check')
    parser.add_argument('--carbon-tc-per-ha', type=float, help='assumed carbon density, tC per ha')
    parser.add_argument('--projected-ha', type=float, help="the project's projected avoided deforestation, ha")
    parser.add_argument('--projected-tco2e', type=float, help="the project's projected avoided emissions, tCO2e")
    parser.add_argument('--kml-layer', help='which layer (folder) of the KML is the project area')
    parser.add_argument('--region', choices=['sa', 'africa', 'asia', 'ca'],
                        help='RADD tile set (detected when not given)')
    parser.add_argument('--search-radius-km', type=float, help='fixed search radius (widened automatically if not set)')
    parser.add_argument('--min-forest-share', type=float, help='cells with less forest are dropped (default 0.5)')
    parser.add_argument('--no-ee', action='store_true', help='skip the Earth Engine checks')
    parser.add_argument('--check-only', action='store_true', help='run the checks and stop, adding nothing')
    parser.add_argument('--force', action='store_true', help='add the project even if a check fails')
    parser.add_argument('--run', action='store_true', help='run the whole pipeline afterwards')
    parser.add_argument('--publish', action='store_true', help='with --run: also write the status to the NFT')
    process(parser.parse_args())


if __name__ == '__main__':
    main()
