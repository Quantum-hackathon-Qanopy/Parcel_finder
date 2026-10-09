# NFT status

The project's monitoring result is published as a status NFT on **Base Sepolia** (a test network).
One token shows the latest status and its URI is updated each time results are published.

| | |
| --- | --- |
| Contract | `0x386f0C26891AdAb4041AB547Dd59D5C8d7B2A492` (name Qanopy, symbol QAY) |
| Source | `QanopyNft.sol` here is a copy; the Foundry project, tests and deploy script are in https://github.com/Quantum-hackathon-Qanopy/qanopynft (copied at commit `42a431f`) |
| ABI | `qanopy_nft_abi.json`, the functions the publisher uses |
| Who can write | Only the contract owner (`getOwner()`) can call `mintNft` and `changeTokenURI` |

## Five statuses

Neutral means on track. The status compares the measured avoided emissions with the claim pro-rated over the monitored window (`src/nft_status.py` holds the bands and the five IPFS images):

| Status | Measured / claim to date | Image |
| --- | --- | --- |
| Worst | below 50% | `ipfs://bafkreigjqxkph5qrbysc7fs42boplu6d5qrq6tmqzieznuojnt475y4tcq` |
| Bad | 50 to 90% | `ipfs://bafkreighulbrlr45rxesw7fcui2yojp5f2vholhopatobl24o7ais5snoq` |
| Neutral (on track) | 90 to 110% | `ipfs://bafkreib7zvsz2y7yyu6frjcqr3sruti42pard65hzjci645lhwsjlec7y4` |
| Good | 110 to 150% | `ipfs://bafkreiaou2gn3gy2k7m67wjx5gnobonpbpsyqem4hkvvoqvwehfc6i562m` |
| Best | above 150% | `ipfs://bafybeif4n7vnvr6kw5a7bsk3mqzb67oq4bo2xbjr463e44awj5zejruybq` |

## Published

Token 0 (project `algar`) was minted on 2026-10-09 (status Worst, week of 2026-09-14): transaction `0xe744cb4e38b27db3d748fe9a89b813600c5fcc09a45b9c0f5d609000f0e0e9cb`, metadata `ipfs://QmRNSG4Toe9nYr5MyVD46whjnM7sDxJfEXg8mfG3nptKrC`. On the same day the token was updated (transaction `0x431548ca835963ca406e094c5eddb093912f6a544f2eba312aa71fa0bccb14a4`, metadata `ipfs://QmNxnGeg7CzQg3bpYFPF2xTyHbDyxtpQSxh7nfrHWERXZ4`) so that the metadata's `image` is an HTTPS gateway URL (with `image_ipfs` as the `ipfs://` link), because Basescan showed no image for the `ipfs://` form. The latest publish is also in `outputs/<project>/nft_state.json`.

Token 1 (project `agroflorestal`) was minted on 2026-10-09 with the status Good: transaction `0xe618bd53da4d9e8452f1fb10105299860655373a6e3b553f14c96155d5903f60`, metadata `ipfs://QmRy44K5RQw6jP9tygvrQ3eZnQibuoE3BtKiDXAWftHfcp`. Its carbon density is an assumption, stated in the token's description and in its `Carbon density source` property.

Token 2 (project `lomphat`) was minted on 2026-10-09 with the status Worst: transaction `0xfe37d8e8be263404ab4f6c182ee6138282fd76c1e5a24e61ab730cf42d893774`, metadata `ipfs://QmTpoc8W86dRUrcPr86XEvWec8BXnqdRqsusTREeTnTJVS`. Its carbon density is an assumption, stated in the token.

Each project has its own token on this contract: the first publish for a project mints a new one and records its id in that project's `nft_state.json`; later publishes update it. Choose the project with `--project`.

## Publishing

```bash
python src/monitor.py                       # produces the status (also shown on the dashboard)
python src/publish_nft.py                   # dry run: shows the status, writes outputs/<project>/nft_metadata.json, checks the chain
python src/publish_nft.py --publish         # pins the metadata to IPFS and writes it on-chain
python src/monitor.py --publish-nft         # monitor and publish in one go
```

`--publish` needs the contract owner's private key (with a little Base Sepolia ETH for gas), an RPC endpoint and Pinata credentials. They are read from the `.env` file in the repository root, which git ignores, or from environment variables: `PRIVATE_KEY`, `SEPOLIA_RPC`, and either `PINATA_JWT` or `PINATA_API_KEY` plus `PINATA_SECRET`. Never put them anywhere else in the repository. `--inline` stores the metadata inside the token URI instead of uploading it to Pinata (no Pinata needed, but about 1.4 million gas instead of about 185,000). `python src/run_pipeline.py --publish` runs the whole pipeline and publishes at the end. The first publish mints the token and records its id in `outputs/<project>/nft_state.json`; later publishes update that token. Each publish is a public transaction that cannot be undone, and the metadata and status are an estimate, not a verified credit.
