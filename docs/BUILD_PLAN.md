# Build Plan: Quantum Carbon Credit Verification

Last updated: 2026-10-05

## Goal and pipeline

We are building a public check on whether a forest-protection (REDD+) carbon credit project really protects the forest it claims to, by comparing it with similar unprotected land and publishing the result on-chain.

The pipeline has seven stages. We pick a target project, find 200 candidate parcels similar to it, and let QAOA choose the best 20 for similarity and spacing. We then freeze that choice, monitor the project and the 20 parcels with weekly satellite data, estimate how much forest the project saved, and record the result in an on-chain certificate that updates as new data arrives.

The core test is simple: if the parcels lose forest and the project does not, the project is likely protecting it. If both lose about the same, it is not.

## Where the repo stands

The repo covers only the candidate search today, and the rest of the pipeline still has to be built. [Pull request #1](https://github.com/Quantum-hackathon-Qanopy/Parcel_finder/pull/1) fixes the crashes in the existing code and defaults to 200 candidates.

| Piece | What it does today | Gap |
| --- | --- | --- |
| `example.py` | Loads the farm boundary from a KML file, stacks three layers (tree cover in 2000, a past-loss flag, elevation) and runs `FindParcels` | The KML file is not in the repo; `ee.Initialize()` needs a Google Cloud project ID |
| `parcel_finder.py` | Scores 400 m grid cells within 50 km of the project by similarity to the project's average features | Matching uses outcome-period data, has no leakage buffer and no spacing between parcels |
| Everything else | Not started | QAOA selection, snapshot, monitoring, estimate, certificate |

## Step 1: Fix the matching

The 200 candidates must be matched only on traits fixed before the project started, and they must be spread out. Otherwise the final comparison is not credible.

1. Add a `project_start` year to `FindParcels`. Replace the `history` feature (`lossyear > 0`, which includes years after the start) with loss before `project_start` only.
2. Exclude a leakage buffer around the project, for example the first 5 km, so land next to it is not a candidate. The buffer size is an open question (see the last section).
3. Exclude cells that overlap other protected areas or have no forest at the start date.
4. Build the 200-candidate pool so it covers the whole search ring and not just the single most similar patch. One option is to keep the best cell per region of the ring, or to sample from the top few hundred by score.
5. Consider more features than the current three, such as distance to roads and rivers and forest type, if the data is available in Earth Engine.

**Done when:** `fit()` returns 200 cells, none inside the buffer, all scored only on pre-start data, and plotting them on a map shows they are spread around the project.

## Step 2: Choose 20 parcels with QAOA

QAOA picks the 20 parcels that are most similar to the project while staying far apart from each other. This is a subset-selection problem with one yes/no variable per candidate.

1. Export the 200 candidates from Earth Engine to Python: centre point, similarity score, and forest area.
2. Build the cost function. For each pair of candidates, penalise choosing two that are close together, and reward choosing candidates with high similarity. Z-score both terms and average them, as the README says, so neither dominates as the number of selected parcels grows.
3. Add a constraint that exactly 20 are chosen, usually as a penalty term, to get a QUBO (quadratic binary optimisation problem).
4. Run QAOA on it with a quantum SDK such as Qiskit or PennyLane, and tune the number of layers and the optimiser.
5. Run a classical baseline on the same cost function (greedy selection or simulated annealing) and compare. If QAOA does not match the baseline, we want to know that early.

**Hardware:** we have access to real quantum hardware for the hackathon, so the final QAOA run goes on the device. Check its qubit count and connectivity first. Current devices are small and noisy, and 200 variables will probably not fit, so pre-filter the pool classically (for example to the 30 to 60 most similar candidates) before the run. Develop and test on a simulator first, then run on the device, and compare the result with the classical baseline.

**Done when:** a script takes the candidate file and returns 20 chosen parcels with their combined similarity and minimum spacing, and the same numbers for the classical baseline.

## Step 3: Freeze the baseline

Once QAOA has chosen the 20 parcels, they are saved in a snapshot that cannot be changed afterward, so nobody can swap in more convenient controls later.

1. Save one JSON file containing the project polygon, the 20 parcel geometries, the project start date, and every matching setting (buffer size, features, forest threshold, candidate count).
2. Compute a hash (for example SHA-256) of that file.
3. Commit the file to the repo and keep the hash. The hash goes on the certificate, so anyone can check that monitoring used the original parcels.
4. Treat the file as read-only. A new parcel set means a new snapshot and a new hash, never an edit.

**Done when:** one command produces the snapshot file and its hash from the QAOA output, and running it twice on the same input gives the same hash.

## Step 4: Monitor weekly

A scheduled script measures forest loss in the project and in each of the 20 parcels, and saves the result every week. It uses two data sources: annual data for numbers we can certify, and alerts for early signals.

1. Write a function `measure_loss(geometry, start, end, source)` that returns the forest area at the start date and the area lost since, in hectares. Forest means canopy above an agreed threshold (open question below).
2. **Annual source:** the Hansen data already used in the repo. It updates about once a year, so it is the reference record.
3. **Alert source:** a near-real-time forest-disturbance layer such as GLAD or RADD. Before building, confirm the exact Earth Engine dataset, its update frequency and its coverage for the project's region, and decide which confidence level counts as loss.
4. Write a `monitor.py` script that loads the frozen snapshot, calls `measure_loss` for the project and all 20 parcels, and appends one row to a results file: timestamp, source, project loss, each parcel's loss, and the snapshot hash.
5. Mark every alert-based row as provisional. When a new Hansen year is published, re-score that period and replace the provisional rows.
6. Schedule the script weekly, for example with GitHub Actions. This needs an Earth Engine service account whose key is stored as a repository secret, never in the code.

**Done when:** the scheduled job runs unattended for three weeks and adds a correct row each time, and the results file matches a manual check of one parcel in the Earth Engine code editor.

## Step 5: Estimate, compare and certify

This step turns the weekly measurements into one verdict: how much forest the project saved, whether that matches its claim, and a public certificate that records it.

1. Compute loss rates (hectares lost divided by starting forest hectares) for the project and for each parcel over the same period.
2. Estimate avoided loss as the average parcel loss rate minus the project loss rate, times the project's starting forest area:

```latex
\text{avoided loss} = \left(\bar{r}_{\text{parcels}} - r_{\text{project}}\right) \times A_{\text{project}}
```

3. Get an uncertainty range from how much the 20 parcels differ from each other. Start with resampling the parcels; quantum amplitude estimation can replace this later, as the README plans.
4. Convert hectares to tons of CO2 using a carbon-per-hectare source. We have not chosen one yet (open question below).
5. Extract the project's claimed baseline and tonnage from its public documents, and show claimed against measured side by side.
6. Write a certificate contract that stores the snapshot hash, the latest estimate with its range, and the claim, and updates when a new result is published. Suggestion: keep only the hash and summary numbers on-chain, and the full results file off-chain with a link.

**Done when:** for one real test project, a single command reads the results file and produces the avoided-loss number, its range and the claim comparison, and the certificate shows the same numbers.

## Open questions and risks

Eight decisions block parts of the build. Assign an owner to each in a comment.

- [ ] Which registered project is the first test target, and what are its start date, boundary file and claimed baseline?
- [ ] How large is the leakage buffer around the project (for example 5 km)?
- [ ] What canopy percentage counts as forest?
- [ ] Which alert dataset do we use, and does it cover the target project's region at the confidence level we need?
- [ ] Which source gives carbon per hectare for the conversion to tons of CO2?
- [ ] Which quantum device do we run QAOA on, and how many qubits and what connectivity does it offer?
- [ ] Which blockchain hosts the certificate, and what exactly is stored on-chain?
- [ ] Does every teammate have an Earth Engine account and a Google Cloud project ID?

**Risks to keep in mind**

- Alerts can include fire, degradation and cloud artefacts, so they are early signals, and only the annual data should back a certified number.
- Similar land losing more forest than the project is evidence of protection, not proof. Other factors we do not match on could explain part of the gap, so the certificate should say "estimate".
- Earth Engine has usage limits. A large candidate grid and weekly runs may hit them, so measure small first.
