"""Freeze the selected comparison parcels into a hashed snapshot (pipeline step 6).

    python src/snapshot.py             # create outputs/snapshot.json from the current selection
    python src/snapshot.py --verify    # check that the file still matches its hash
    python src/snapshot.py --force     # replace an existing snapshot (the old hash is lost)

The snapshot stores the picked parcels, the project cells, the settings and the hash of the
project boundary file, plus a SHA-256 over all of it. Monitoring (monitor.py) loads the
snapshot and refuses to run if the hash does not match, so the parcels cannot be swapped
after the outcome is known. This is a tamper check on a local file. Publishing the hash
somewhere public (the planned on-chain certificate) is what makes it binding.
"""

import argparse
import csv
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from config import (CELL_KM, PROFILE_JSON, PROJECT_CELLS_CSV, PROJECT_NAME, PROJECT_SLUG,
                    SELECTED_CSV, SNAPSHOT_JSON, require_boundary)
from select_parcels import POOL_SIZE, SPACING_KM, WEIGHT_CROWDING, WEIGHT_FIT


def canonical_hash(content):
    """SHA-256 of the content with sorted keys, so the same data always gives the same hash."""
    text = json.dumps(content, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_rows(path):
    with open(path, newline='') as fh:
        return [{k: (float(v) if k not in ('id',) else v) for k, v in row.items()}
                for row in csv.DictReader(fh)]


def git_commit():
    try:
        out = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True,
                             cwd=Path(__file__).resolve().parent, timeout=10)
        return out.stdout.strip() or None
    except Exception:
        return None


def create(force=False):
    if SNAPSHOT_JSON.exists() and not force:
        raise SystemExit(f'{SNAPSHOT_JSON} already exists. A snapshot is meant to stay fixed; '
                         'use --force only to start a new one.')
    parcels = read_rows(SELECTED_CSV)
    project = read_rows(PROJECT_CELLS_CSV)
    with open(PROFILE_JSON) as fh:
        profile = json.load(fh)
    content = {
        'created_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'git_commit': git_commit(),
        'project_start_year': profile['start_year'],
        'cell_km': CELL_KM,
        'selection_settings': {'k': len(parcels), 'pool_size': POOL_SIZE, 'spacing_km': SPACING_KM,
                               'weight_fit': WEIGHT_FIT, 'weight_crowding': WEIGHT_CROWDING},
        'project': {'slug': PROJECT_SLUG, 'name': PROJECT_NAME},
        'project_boundary_sha256': file_hash(require_boundary()),
        'parcels': parcels,
        'project_cells': [{'id': r['id'], 'lon': r['lon'], 'lat': r['lat'],
                           'forest_start': r['forest_start']} for r in project],
    }
    snapshot = dict(content, sha256=canonical_hash(content))
    with open(SNAPSHOT_JSON, 'w') as fh:
        json.dump(snapshot, fh, indent=1)
    print(f"Saved {SNAPSHOT_JSON}: {len(parcels)} parcels, {len(project)} project cells")
    print(f"SHA-256 {snapshot['sha256']}")
    return snapshot


def load(verify=True):
    """Read the snapshot; raise if it was changed after it was written."""
    with open(SNAPSHOT_JSON) as fh:
        snapshot = json.load(fh)
    if verify:
        content = {k: v for k, v in snapshot.items() if k != 'sha256'}
        if canonical_hash(content) != snapshot['sha256']:
            raise SystemExit('Snapshot hash mismatch: snapshot.json was modified after it '
                             'was created. Refusing to continue.')
    return snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--verify', action='store_true', help='check the existing snapshot')
    parser.add_argument('--force', action='store_true', help='replace an existing snapshot')
    args = parser.parse_args()
    if args.verify:
        snapshot = load()
        print(f"OK: snapshot matches its hash {snapshot['sha256']}")
        boundary_ok = snapshot['project_boundary_sha256'] == file_hash(require_boundary())
        print('Project boundary file ' + ('matches.' if boundary_ok else
                                          'has CHANGED since the snapshot.'))
        return
    create(force=args.force)


if __name__ == '__main__':
    main()
