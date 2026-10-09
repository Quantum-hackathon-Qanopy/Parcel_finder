"""Publish the project's monitoring status to the Qanopy NFT (Base Sepolia).

    python src/publish_nft.py                 # dry run: status, metadata, chain checks. Changes nothing.
    python src/publish_nft.py --publish       # upload the metadata to IPFS and write it on-chain
    python src/publish_nft.py --publish --inline       # same, metadata inside the token URI (no Pinata)
    python src/publish_nft.py --publish --token-id 3   # update an existing token

What --publish does:
  1. work out the status (nft_status.py) from the newest monitoring run (monitor.py)
  2. pin the metadata JSON to IPFS through Pinata
  3. the first time, mint the token (mintNft); afterwards update the same token's URI
     (changeTokenURI), so one NFT always shows the latest status
  4. check the chain now returns the new URI and record the result in outputs/nft_state.json

Secrets come from the .env file in the repository root (git ignores it) or from environment
variables, never from code. Names that are accepted (not case sensitive):
    private key of the contract owner (needs a little Base Sepolia ETH):  PRIVATE_KEY or
                                QANOPY_OWNER_PRIVATE_KEY
    RPC endpoint (default https://sepolia.base.org):  SEPOLIA_RPC or QANOPY_RPC_URL
    Pinata, either a JWT:  PINATA_JWT
            or an API key and its secret:  PINATA_API_KEY (or Pinatakey, API Key) and
                                PINATA_SECRET (or API Secret)
    A line may be written NAME=value or NAME: value; a JWT line named JWT also works.
Only the contract's owner can mint or update. If you do not want to use Pinata, --inline puts
the metadata JSON inside the token URI itself (a data: URI), so nothing is uploaded; the
images stay on IPFS.

Every publish is a public, permanent transaction (the contract is on a test network).
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone

from config import ROOT, get_env
from nft_status import (CHAIN_ID, CONTRACT_ADDRESS, EXPLORER_URL, METADATA_JSON, RPC_URL,
                        STATE_JSON, build_metadata, build_status, load_latest, load_state)

ABI_JSON = ROOT / 'nft' / 'qanopy_nft_abi.json'
PINATA_URL = os.environ.get('PINATA_API_URL', 'https://api.pinata.cloud')
ZERO_ADDRESS = '0x' + '0' * 40


# ------------------------------------------------------------------------------ chain
def connect():
    """(web3, contract) for the configured network, or raises with a readable message."""
    from web3 import Web3
    w3 = Web3(Web3.HTTPProvider(get_env('QANOPY_RPC_URL', 'SEPOLIA_RPC', default=RPC_URL),
                                request_kwargs={'timeout': 30}))
    if not w3.is_connected():
        raise RuntimeError('cannot reach the network RPC (set QANOPY_RPC_URL to another endpoint)')
    if w3.eth.chain_id != CHAIN_ID:
        raise RuntimeError(f'the RPC is on chain {w3.eth.chain_id}, expected {CHAIN_ID} (Base Sepolia)')
    with open(ABI_JSON) as fh:
        abi = json.load(fh)
    address = Web3.to_checksum_address(get_env('QANOPY_CONTRACT', default=CONTRACT_ADDRESS))
    return w3, w3.eth.contract(address=address, abi=abi)


def hex0x(value):
    """A hash as a 0x-prefixed string (HexBytes.hex() has no prefix in newer versions)."""
    return value.to_0x_hex() if hasattr(value, 'to_0x_hex') else value.hex()


def wait_for_uri(contract, token_id, expected, seconds=40):
    """The token URI the chain returns, retried for a while because RPC nodes can lag a block."""
    deadline = time.time() + seconds
    while True:
        onchain = contract.functions.tokenURI(token_id).call()
        if onchain == expected or time.time() > deadline:
            return onchain
        time.sleep(3)


def token_exists(contract, token_id):
    try:
        contract.functions.ownerOf(token_id).call()
        return True
    except Exception:                       # ownerOf reverts for a token that was never minted
        return False


def send(w3, contract, function, account):
    """Sign and send one transaction, wait for it, and return the receipt."""
    tx = function.build_transaction({'from': account.address,
                                     'nonce': w3.eth.get_transaction_count(account.address)})
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f'  sent {hex0x(tx_hash)}; waiting for confirmation...')
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt['status'] != 1:
        raise RuntimeError(f'the transaction was reverted: {EXPLORER_URL}/tx/{hex0x(tx_hash)}')
    return receipt


def minted_token_id(contract, receipt):
    """Token id from the Transfer event (from the zero address) in a mint receipt."""
    for event in contract.events.Transfer().process_receipt(receipt):
        if event['args']['from'] == ZERO_ADDRESS:
            return int(event['args']['tokenId'])
    raise RuntimeError('minted, but the new token id was not found; pass it with --token-id next time')


# ---------------------------------------------------------------------------- metadata
def pinata_headers():
    """Authentication headers for Pinata (a JWT, or an API key with its secret), or None."""
    jwt = get_env('PINATA_JWT', 'JWT')
    if jwt:
        return {'Authorization': f'Bearer {jwt}'}
    key = get_env('PINATA_API_KEY', 'Pinatakey', 'API_KEY')
    secret = get_env('PINATA_SECRET', 'PINATA_SECRET_API_KEY', 'API_SECRET')
    if key and secret:
        return {'pinata_api_key': key, 'pinata_secret_api_key': secret}
    return None


def inline_uri(metadata):
    """The metadata JSON as a data: URI, so it can live in the token itself."""
    import base64
    raw = json.dumps(metadata, separators=(',', ':')).encode('utf-8')
    return 'data:application/json;base64,' + base64.b64encode(raw).decode('ascii')


def pin_metadata(metadata, headers, name):
    """Pin the metadata JSON to IPFS through Pinata and return its ipfs:// URI."""
    import requests
    response = requests.post(
        f'{PINATA_URL}/pinning/pinJSONToIPFS',
        headers={**headers, 'Content-Type': 'application/json'},
        json={'pinataContent': metadata,
              'pinataMetadata': {'name': name}},
        timeout=60)
    if response.status_code != 200:
        raise RuntimeError(f'Pinata refused the upload ({response.status_code}): {response.text[:200]}')
    return 'ipfs://' + response.json()['IpfsHash']


def save_state(entry):
    state = load_state()
    state.update(entry)                                  # the latest publish, at the top level
    state['history'] = (state.get('history', []) + [entry])[-25:]
    STATE_JSON.write_text(json.dumps(state, indent=1))


# ---------------------------------------------------------------------------------- run
def publish(dry_run=True, token_id=None, inline=False):
    latest = load_latest()
    status = build_status(latest)
    if status is None:
        raise SystemExit(f"No carbon density for project '{latest['project']['slug']}', so there is "
                         'no status to publish. Set it in config.py first.')
    metadata = build_metadata(latest)
    METADATA_JSON.write_text(json.dumps(metadata, indent=1))
    print(f"Status: {status['label']}  (measured {status['avoided_tco2']:,.0f} tCO2e of "
          f"{status['claimed_tco2']:,.0f} claimed to date = {status['ratio']:.0%}; "
          f"week of {status['week_start']}{', provisional' if status['provisional'] else ''})")
    print(f'Metadata written to {METADATA_JSON}')

    state = load_state()
    if token_id is None and state.get('contract') == CONTRACT_ADDRESS:
        token_id = state.get('token_id')

    try:
        w3, contract = connect()
        owner = contract.functions.getOwner().call()
        exists = token_id is not None and token_exists(contract, token_id)
    except Exception as err:
        if not dry_run:
            raise SystemExit(f'Cannot check the contract: {err}')
        print(f'Chain not checked ({err})')
        return
    action = f'update token {token_id}' if exists else 'mint a new token'
    print(f'Contract {contract.address} on Base Sepolia, owner {owner}; would {action}.')

    pinata, key = pinata_headers(), get_env('QANOPY_OWNER_PRIVATE_KEY', 'PRIVATE_KEY')
    print('Credentials found: owner key ' + ('yes' if key else 'NO') + ', Pinata '
          + ('yes' if pinata else 'NO (needs a JWT, or an API key plus its secret)')
          + (' (not needed with --inline)' if inline else ''))

    if dry_run:
        sample = inline_uri(metadata) if inline else 'ipfs://' + 'b' * 59
        try:
            function = (contract.functions.changeTokenURI(sample, token_id) if exists
                        else contract.functions.mintNft(sample))
            print(f'Estimated gas: {function.estimate_gas({"from": owner}):,}')
        except Exception as err:
            print(f'Gas estimate failed ({str(err)[:100]})')
        print('Dry run only. Nothing was uploaded or sent. Add --publish to publish.')
        return

    if not key or not (pinata or inline):
        raise SystemExit('Missing credentials. Put the owner key and the Pinata credentials in the .env '
                         'file (see the top of this file), or use --inline to skip Pinata.')
    from eth_account import Account
    account = Account.from_key(key if key.startswith('0x') else '0x' + key)
    if account.address != owner:
        raise SystemExit(f'Your key is for {account.address}, but only the contract owner {owner} '
                         'can mint or update.')
    if w3.eth.get_balance(account.address) == 0:
        raise SystemExit(f'{account.address} has no Base Sepolia ETH for gas. Use a faucet first.')

    if inline:
        uri = inline_uri(metadata)
        print(f'Metadata stored inline in the token URI ({len(uri):,} characters)')
    else:
        uri = pin_metadata(metadata, pinata, f"qanopy-{latest['project']['slug']}-{status['week_start']}-{status['key']}")
        print(f'Metadata pinned: {uri}')
    if exists:
        receipt = send(w3, contract, contract.functions.changeTokenURI(uri, token_id), account)
    else:
        receipt = send(w3, contract, contract.functions.mintNft(uri), account)
        token_id = minted_token_id(contract, receipt)
    tx_hash = hex0x(receipt['transactionHash'])
    save_state({'contract': contract.address, 'chain_id': CHAIN_ID, 'token_id': token_id,
                'token_uri': uri, 'tx_hash': tx_hash, 'action': 'update' if exists else 'mint',
                'status': status['key'], 'ratio': status['ratio'], 'week_start': status['week_start'],
                'published_at': datetime.now(timezone.utc).isoformat(timespec='seconds')})
    onchain = wait_for_uri(contract, token_id, uri)           # nodes can lag a few seconds behind
    if onchain != uri:
        print(f'Warning: the node still returns {onchain!r} for the token URI. The transaction was '
              f'confirmed and recorded; check {EXPLORER_URL}/tx/{tx_hash}')
    print(f'Done: token {token_id} now shows "{status["label"]}".')
    print(f'  transaction {EXPLORER_URL}/tx/{tx_hash}')
    print(f'  token       {EXPLORER_URL}/token/{contract.address}?a={token_id}')
    try:
        import monitor
        monitor.refresh_dashboard()
        print('Dashboard updated with the new token.')
    except Exception as err:                 # the publish itself already succeeded
        print(f'Dashboard not refreshed ({err}); the next monitor.py run will show the token.')


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--publish', action='store_true', help='really upload and send (default: dry run)')
    parser.add_argument('--token-id', type=int, help='update this existing token instead of the saved one')
    parser.add_argument('--inline', action='store_true',
                        help='store the metadata inside the token URI instead of uploading to Pinata')
    args = parser.parse_args()
    publish(dry_run=not args.publish, token_id=args.token_id, inline=args.inline)


if __name__ == '__main__':
    main()
