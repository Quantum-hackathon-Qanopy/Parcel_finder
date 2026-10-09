"""The NFT status and its metadata (pure logic: no network, no keys).

The project's result is published as a status NFT with five levels. Neutral means on track;
one level either side means doing a bit better or worse than the estimate, and Worst and Best
mean far below or far above it. The status compares the measured avoided emissions with the
claim, pro-rated over the monitored window:

    ratio = measured avoided tCO2e / claimed tCO2e to date

    ratio < 0.5          Worst
    0.5 to 0.9           Bad
    0.9 to 1.1           Neutral (on track)
    1.1 to 1.5           Good
    above 1.5            Best

The bands are set in STATUSES below. Contract, network and image links come from the
qanopynft repository (see nft/README.md).
"""

import json

from config import OUTPUT_DIR

LATEST_JSON = OUTPUT_DIR / 'monitoring_latest.json'     # written by monitor.py
STATE_JSON = OUTPUT_DIR / 'nft_state.json'              # written by publish_nft.py
METADATA_JSON = OUTPUT_DIR / 'nft_metadata.json'        # the metadata that is (or would be) published

CONTRACT_ADDRESS = '0x386f0C26891AdAb4041AB547Dd59D5C8d7B2A492'    # Base Sepolia
CHAIN_ID = 84532
RPC_URL = 'https://sepolia.base.org'
EXPLORER_URL = 'https://sepolia.basescan.org'

# Block explorers do not always resolve ipfs:// image links (Basescan showed no image for one), so
# the metadata's `image` is an https gateway URL and the ipfs:// link is kept as `image_ipfs`.
IMAGE_GATEWAY = 'https://gateway.pinata.cloud/ipfs/'

# (key, label, upper limit of measured/claimed, image on IPFS). The last limit is open-ended.
STATUSES = [
    ('worst', 'Worst', 0.5, 'ipfs://bafkreigjqxkph5qrbysc7fs42boplu6d5qrq6tmqzieznuojnt475y4tcq'),
    ('bad', 'Bad', 0.9, 'ipfs://bafkreighulbrlr45rxesw7fcui2yojp5f2vholhopatobl24o7ais5snoq'),
    ('neutral', 'Neutral (on track)', 1.1, 'ipfs://bafkreib7zvsz2y7yyu6frjcqr3sruti42pard65hzjci645lhwsjlec7y4'),
    ('good', 'Good', 1.5, 'ipfs://bafkreiaou2gn3gy2k7m67wjx5gnobonpbpsyqem4hkvvoqvwehfc6i562m'),
    ('best', 'Best', float('inf'), 'ipfs://bafybeif4n7vnvr6kw5a7bsk3mqzb67oq4bo2xbjr463e44awj5zejruybq'),
]


def status_for(ratio):
    """(key, label, image) for a measured/claimed ratio."""
    for key, label, upper, image in STATUSES:
        if ratio < upper:
            return key, label, image
    raise ValueError('unreachable')


def build_status(latest):
    """Status and headline numbers from monitoring_latest.json (the newest week), or None when
    the project has no carbon density and so no tCO2e figure to compare with the claim."""
    record, project = latest['record'], latest['project']
    if record['avoided_tco2'] is None:
        return None
    claimed = record['claim_tco2']
    ratio = record['avoided_tco2'] / claimed if claimed > 0 else 0.0
    key, label, image = status_for(ratio)
    return {'key': key, 'label': label, 'image': image, 'ratio': ratio,
            'levels': [{'key': k, 'label': lab, 'upper': None if up == float('inf') else up}
                       for k, lab, up, _ in STATUSES],
            'avoided_ha': record['avoided_ha'], 'avoided_tco2': record['avoided_tco2'],
            'claimed_tco2': claimed, 'week_start': record['week_start'],
            'provisional': record['provisional'], 'project': project}


def build_metadata(latest):
    """ERC-721 metadata (the JSON a token URI points to) for the current status."""
    status = build_status(latest)
    if status is None:
        raise ValueError('no carbon density for this project, so there is no status to publish')
    record, project = latest['record'], latest['project']
    attributes = [
        ('Status', status['label']),
        ('Measured vs claim to date (%)', round(100 * status['ratio'], 1)),
        ('Avoided deforestation (ha)', round(record['avoided_ha'])),
        ('Avoided emissions (tCO2e)', round(record['avoided_tco2'])),
        ('Claimed to date (tCO2e)', round(status['claimed_tco2'])),
        ('Interval low (ha)', round(record['avoided_ci_lo'])),
        ('Interval high (ha)', round(record['avoided_ci_hi'])),
        ('Data through (week of)', record['week_start']),
        ('Provisional', 'yes' if record['provisional'] else 'no'),
        ('Monitoring window start', latest['since']),
        ('Project start', project['start']),
        ('Project end', project['end']),
        ('Claim per year (tCO2e)', round(project['claim_per_year'])),
        ('Carbon density (tCO2e/ha)', round(project['tco2e_per_ha'], 1)),
        ('Carbon density source', {'projection': "Project's own projection",
                                   'assumption': 'Assumption (typical Amazon value)'}.get(
            project.get('density_source'), 'Not stated')),
        ('Snapshot SHA-256', latest['snapshot_sha256']),
    ]
    return {
        'name': f"Qanopy: {project['name']} forest monitoring",
        'description': (
            f"Status of the {project['name']} project's measured avoided deforestation against "
            f"its own projection: {status['label']}. Measured from weekly RADD radar alerts against "
            'a frozen set of matched comparison parcels. This is an estimate with wide uncertainty, '
            'not a verified carbon credit. The newest weeks are provisional.'
            + (' The carbon density behind the tCO2e figures is an assumption (a typical Amazon value), '
               'not a figure from the project, so the status depends on it.'
               if project.get('density_source') == 'assumption' else '')),
        'image': IMAGE_GATEWAY + status['image'].removeprefix('ipfs://'),
        'image_ipfs': status['image'],
        'attributes': [{'trait_type': k, 'value': v} for k, v in attributes],
    }


def load_latest():
    if not LATEST_JSON.exists():
        raise SystemExit(f'{LATEST_JSON} not found. Run monitor.py first.')
    with open(LATEST_JSON) as fh:
        return json.load(fh)


def load_state():
    """What was published last (token id, URI, transaction), or {} if nothing yet."""
    if STATE_JSON.exists():
        with open(STATE_JSON) as fh:
            return json.load(fh)
    return {}
