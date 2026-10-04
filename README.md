# Quantum-Enhanced Public Carbon Credit Verification

A public, independently checkable way to see whether a forest protection (REDD+) carbon credit project is delivering the impact it claims.

## The problem

Satellite-based rating platforms such as Sylvera, Pachama and BeZero already monitor carbon credit projects, and Verra now sets REDD+ baselines itself (methodology VM0048). What is still missing is a public record. A project's current status sits inside paid dashboards, and a company can choose not to share an unfavorable result.

## The idea

1. Compare a registered project against similar, unprotected land nearby, using real satellite data.
2. Use quantum computing to select the best comparison parcels and estimate what would have happened without the project.
3. Publish the result as an on-chain certificate (NFT) that updates as new satellite data arrives, so anyone can check it.

The comparison land is required, not optional. Avoided deforestation only has meaning relative to what would have happened anyway, so a project's claimed impact cannot be checked without a stand-in for that hypothetical.

## How it works

| Step | What it does |
|---|---|
| 1. Pick a project | Take a registered project's boundary and claimed impact |
| 2. Read documents | AI extracts the claimed baseline and tonnage from public project documents |
| 3. Generate candidates | Create comparison parcels around the project, with a buffer zone excluded to limit leakage |
| 4. Match | Rank candidates by similarity to the project, using only information the outcome could not have influenced |
| 5. Select | Quantum optimization (QAOA) picks the best set of parcels, balancing similarity and geographic spread |
| 6. Measure | Satellite forest loss for the project and the chosen parcels since project start |
| 7. Estimate | Quantum amplitude estimation gives the expected loss without the project, with a confidence interval |
| 8. Compare | Convert to tons of CO2 and compare with the project's claim |
| 9. Certificate | Record the result on-chain, publicly and permanently |

## Design principles

- **No circularity.** Parcels are matched on traits fixed before the outcome period, never on the outcome itself.
- **Leakage buffer.** Land next to the project is excluded, since deforestation pressure can shift there.
- **Spread-out comparison set.** Selected parcels should be far enough apart to count as independent evidence.
- **Scaled objective.** Similarity and distance are z-scored and averaged so that neither term dominates as the number of selected parcels grows.
