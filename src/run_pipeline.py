"""Run the pipeline from one command.

    python src/run_pipeline.py                  # weekly run: monitor, then a dry run of the NFT publish
    python src/run_pipeline.py --publish        # weekly run, then publish the status to the NFT
    python src/run_pipeline.py --full           # also rebuild candidates, selection and the snapshot first
    python src/run_pipeline.py --plan           # show the steps without running anything
    python src/run_pipeline.py --project agroflorestal # the same, for another project (see config.py)

Steps, in order:
    search    example.py          find candidate parcels (Earth Engine; --full only)
    select    select_parcels.py   pick the comparison parcels (--full only)
    snapshot  snapshot.py         freeze the selection; created only if missing, never replaced
    monitor   monitor.py          weekly alerts, avoided-loss estimate, dashboard, NFT status
    publish   publish_nft.py      write the status to the NFT (dry run unless --publish)

Publishing is a public transaction, so it only happens with --publish. The weekly run does not
touch the frozen snapshot; to start a new one, run snapshot.py --force by hand.
"""

import argparse
import subprocess
import sys
import time

from config import (CARBON_TCO2E_PER_HA, PROJECT_NAME, PROJECT_SLUG, ROOT, SNAPSHOT_JSON,
                    require_boundary)

SRC = ROOT / 'src'


def plan(args):
    """The steps to run, as (name, script, extra arguments)."""
    steps = []
    if args.full:
        steps += [('search', 'example.py', []), ('select', 'select_parcels.py', [])]
    if not SNAPSHOT_JSON.exists():
        steps.append(('snapshot', 'snapshot.py', []))
    steps.append(('monitor', 'monitor.py',
                  ['--offline'] * args.offline + ['--no-open'] * (not args.open)))
    if CARBON_TCO2E_PER_HA is None:
        print(f"Note: '{PROJECT_SLUG}' has no carbon density yet, so there is no NFT status and the "
              'publish step is skipped. Set it in config.py to enable it.')
    else:
        publish = ['--publish'] * args.publish + ['--inline'] * args.inline
        steps.append(('publish', 'publish_nft.py', publish))
    if args.only:
        steps = [s for s in steps if s[0] == args.only] or [(args.only, f'{args.only}.py', [])]
    return steps


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--full', action='store_true', help='rebuild candidates and selection first')
    parser.add_argument('--publish', action='store_true', help='really publish to the NFT (default: dry run)')
    parser.add_argument('--inline', action='store_true', help='with --publish, store the metadata in the token URI')
    parser.add_argument('--offline', action='store_true', help='monitor from the last download')
    parser.add_argument('--open', action='store_true', help='open the dashboard when done')
    parser.add_argument('--only', choices=['search', 'select', 'snapshot', 'monitor', 'publish'],
                        help='run just this step')
    parser.add_argument('--plan', action='store_true', help='print the steps and stop')
    args = parser.parse_args()

    require_boundary()                                # every step needs the project's boundary
    steps = plan(args)
    print(f'Project: {PROJECT_NAME} ({PROJECT_SLUG}); results in outputs/{PROJECT_SLUG}/')
    if args.full and SNAPSHOT_JSON.exists():
        print('Note: a snapshot already exists and is kept, so monitoring keeps using its parcels even if '
              'the selection changes. Run snapshot.py --force to start a new one.')
    print('Pipeline: ' + ' -> '.join(name for name, _, _ in steps))
    if args.plan:
        for name, script, extra in steps:
            print(f'  {name:9} python src/{script} {" ".join(extra)}'.rstrip())
        return
    for number, (name, script, extra) in enumerate(steps, 1):
        print(f'\n=== Step {number} of {len(steps)}: {name} ===', flush=True)
        started = time.time()
        result = subprocess.run([sys.executable, str(SRC / script), *extra], cwd=ROOT)
        if result.returncode != 0:
            raise SystemExit(f'\nStep "{name}" failed (exit code {result.returncode}); stopping.')
        print(f'--- {name} finished in {time.time() - started:.0f} s')
    print('\nPipeline finished.')


if __name__ == '__main__':
    main()
