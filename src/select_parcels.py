"""Choose the final comparison parcels from the candidate pool.

The finder (parcel_finder.py) scores candidates one by one. This step judges a whole
SET of K parcels at once: together they should look like the project.

Cost of a set (lower is better), with every feature measured in units of its spread:

    balance   how far the set's average features are from the project's average
    fit       average distance of the set's parcels to the project's own cells
    crowding  share of parcel pairs closer than SPACING_KM

Each term is quadratic in the 0/1 choice variables x_i (1 = parcel i is picked), so
the cost is a QUBO:   cost(x) = x^T A x + const,   with exactly K ones in x.
That is the form QAOA needs (see qaoa.py). This file builds the QUBO and solves it
with classical methods, which are the baselines QAOA is compared against.

Input : candidates.csv and project_cells.csv written by example.py.
Output: selected_parcels.csv (the best set found), selected_matches.csv (the project cell
        each picked parcel resembles most) and a comparison printed to screen.
        selection_map.py draws the result on a map.
"""

import csv
import itertools
import json
import math
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))   # find qaoa.py

# ------------------------------------------------------------------ settings
ROOT = Path(__file__).resolve().parent.parent   # repository root
DATA_DIR = ROOT / 'data'
OUTPUT_DIR = ROOT / 'outputs'
OUTPUT_DIR.mkdir(exist_ok=True)

CANDIDATES_CSV = OUTPUT_DIR / 'candidates.csv'
PROJECT_CELLS_CSV = OUTPUT_DIR / 'project_cells.csv'
PROFILE_JSON = OUTPUT_DIR / 'project_profile.json'       # optional: gives the finder's feature spreads
SELECTED_CSV = OUTPUT_DIR / 'selected_parcels.csv'
MATCHES_CSV = OUTPUT_DIR / 'selected_matches.csv'        # each picked parcel and the project cell it matches

K = 50                  # parcels to pick
POOL_SIZE = 200         # use at most this many candidates (most similar first)
SPACING_KM = 6.0        # pairs closer than this count as crowded
WEIGHT_FIT = 0.25       # weight of the "fit" term relative to "balance"
WEIGHT_CROWDING = 0.5   # weight of the "crowding" term relative to "balance"

SMALL_POOL = 16         # size of the reduced problem that fits a simulator / device
SMALL_K = 6
QAOA_LAYERS = (1, 2)    # QAOA depths to try on the reduced problem
SEED = 7


# ------------------------------------------------------------------- loading
def read_rows(path):
    with open(path, newline='') as fh:
        return list(csv.DictReader(fh))


def load_pool(candidates_csv=CANDIDATES_CSV, project_csv=PROJECT_CELLS_CSV,
              profile_json=PROFILE_JSON, pool_size=POOL_SIZE):
    """Read the finder's output. Returns a dict with scaled features and positions."""
    cand_rows = read_rows(candidates_csv)
    proj_rows = read_rows(project_csv)
    names = [c for c in cand_rows[0].keys() if c not in ('id', 'lon', 'lat', 'similarity')]
    cand_rows.sort(key=lambda r: -float(r['similarity']))
    cand_rows = cand_rows[:pool_size]

    cand = np.array([[float(r[n]) for n in names] for r in cand_rows])
    proj = np.array([[float(r[n]) for n in names] for r in proj_rows])

    # Spread of each feature: the finder's own value if saved, otherwise from the pool.
    if os.path.exists(profile_json):
        with open(profile_json) as fh:
            saved = json.load(fh).get('feature_spread', {})
        spread = np.array([saved.get(n, cand[:, i].std()) for i, n in enumerate(names)])
    else:
        spread = cand.std(axis=0)
    spread[spread <= 0] = 1.0

    similarity = np.array([float(r['similarity']) for r in cand_rows])
    has_position = 'lon' in proj_rows[0]       # older project_cells.csv files have none
    return {
        'names': names,
        'rows': cand_rows,
        'ids': [r['id'] for r in cand_rows],
        'project': proj / spread,                      # every project cell, in spread units
        'project_ids': [r['id'] for r in proj_rows],
        'project_lonlat': (np.array([[float(r['lon']), float(r['lat'])] for r in proj_rows])
                           if has_position else None),
        'raw': cand,                                   # original units, for reporting
        'features': cand / spread,                     # in spread units
        'project_mean': proj.mean(axis=0) / spread,
        'project_mean_raw': proj.mean(axis=0),
        'spread': spread,
        'delta': 1.0 / similarity - 1.0,               # the finder's distance to the project
        'lon': np.array([float(r['lon']) for r in cand_rows]),
        'lat': np.array([float(r['lat']) for r in cand_rows]),
    }


def km_matrix(lon, lat):
    """Great-circle distance in km between every pair of points."""
    p, q = np.radians(lat), np.radians(lon)
    dp, dq = p[:, None] - p[None, :], q[:, None] - q[None, :]
    a = np.sin(dp / 2) ** 2 + np.cos(p)[:, None] * np.cos(p)[None, :] * np.sin(dq / 2) ** 2
    return 6371.0 * 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


# ------------------------------------------------------------------- the QUBO
class Problem:
    """Pick exactly k of n candidates to minimise  x^T A x + const."""

    def __init__(self, features, project_mean, delta, lon, lat, k,
                 spacing_km=SPACING_KM, w_fit=WEIGHT_FIT, w_crowd=WEIGHT_CROWDING):
        n, d = features.shape
        if not 2 <= k < n:
            raise ValueError('need 2 <= k < number of candidates')
        self.n, self.k, self.d = n, k, d
        self.features, self.project_mean, self.delta = features, project_mean, delta
        self.w_fit, self.w_crowd = w_fit, w_crowd
        self.n_pairs = k * (k - 1) / 2
        self.close = (km_matrix(lon, lat) < spacing_km) & ~np.eye(n, dtype=bool)

        # balance = mean over features of (set average - project average)^2
        #         = x^T (F F^T / (d k^2)) x  -  (2 / (d k)) (F m)^T x  +  m.m / d
        A = features @ features.T / (d * k * k)
        A[np.diag_indices(n)] += (-2.0 / (d * k)) * (features @ project_mean) \
                                 + w_fit * delta / k                      # fit is linear
        A += w_crowd * self.close / (2 * self.n_pairs)                   # crowding is pairwise
        self.A = A
        self.const = float(project_mean @ project_mean / d)

    def subset(self, n):
        """Row numbers of n candidates drawn with a fixed seed (for a smaller problem)."""
        return np.random.default_rng(SEED).permutation(self.n)[:n]

    def cost(self, idx):
        idx = np.asarray(idx)
        return float(self.A[np.ix_(idx, idx)].sum() + self.const)

    def parts(self, idx):
        """The three terms of the cost for a set, computed directly (not through A)."""
        idx = np.asarray(idx)
        balance = float(np.mean((self.features[idx].mean(axis=0) - self.project_mean) ** 2))
        fit = float(self.delta[idx].mean())
        crowding = float(self.close[np.ix_(idx, idx)].sum() / 2 / self.n_pairs)
        return {'balance': balance, 'fit': fit, 'crowding': crowding,
                'total': balance + self.w_fit * fit + self.w_crowd * crowding}


# ------------------------------------------------------------ classical solvers
def top_k_similarity(problem):
    """Naive baseline: the K parcels with the best individual similarity."""
    return list(np.argsort(problem.delta)[:problem.k])


def greedy_swap(problem):
    """Add parcels one at a time (cheapest cost first), then improve by single swaps."""
    chosen = []
    for _ in range(problem.k):
        rest = [i for i in range(problem.n) if i not in chosen]
        chosen.append(min(rest, key=lambda i: _partial_cost(problem, chosen + [i])))
    best = _partial_cost(problem, chosen)
    improved = True
    while improved:
        improved = False
        for pos in range(problem.k):
            for cand in range(problem.n):
                if cand in chosen:
                    continue
                trial = chosen[:pos] + [cand] + chosen[pos + 1:]
                c = _partial_cost(problem, trial)
                if c < best - 1e-12:
                    chosen, best, improved = trial, c, True
    return chosen


def _partial_cost(problem, idx):
    """Cost of a partial set, scaled as if it were the full K (keeps greedy comparable)."""
    return float(problem.A[np.ix_(idx, idx)].sum())


def simulated_annealing(problem, restarts=10, steps=20000, seed=SEED):
    """Swap-based annealing: move one picked parcel out, one unpicked parcel in."""
    rng = np.random.default_rng(seed)
    A, n, k = problem.A, problem.n, problem.k
    best_idx, best_cost = None, math.inf
    for _ in range(restarts):
        x = np.zeros(n)
        x[rng.permutation(n)[:k]] = 1.0
        Ax = A @ x
        energy = float(x @ Ax)
        # temperature range from the size of typical swap moves
        sample = []
        for _ in range(200):
            a = rng.choice(np.flatnonzero(x == 0))
            b = rng.choice(np.flatnonzero(x == 1))
            sample.append(abs(2 * Ax[a] - 2 * Ax[b] + A[a, a] + A[b, b] - 2 * A[a, b]))
        t_hot, t_cold = max(np.mean(sample), 1e-9), max(np.mean(sample), 1e-9) * 1e-3
        for step in range(steps):
            t = t_hot * (t_cold / t_hot) ** (step / steps)
            a = rng.choice(np.flatnonzero(x == 0))
            b = rng.choice(np.flatnonzero(x == 1))
            delta = 2 * Ax[a] - 2 * Ax[b] + A[a, a] + A[b, b] - 2 * A[a, b]
            if delta <= 0 or rng.random() < math.exp(-delta / t):
                x[a], x[b] = 1.0, 0.0
                Ax += A[:, a] - A[:, b]
                energy += delta
        if energy < best_cost - 1e-12:
            best_cost, best_idx = energy, list(np.flatnonzero(x == 1))
    return best_idx


def exact_best(problem, limit=5_000_000, chunk=200_000):
    """Check every possible set. Only for small problems; returns None if too many."""
    if math.comb(problem.n, problem.k) > limit:
        return None
    best_idx, best_cost = None, math.inf
    combos = itertools.combinations(range(problem.n), problem.k)
    while True:
        block = np.array(list(itertools.islice(combos, chunk)))
        if block.size == 0:
            break
        # cost of each combination = sum of A over all pairs of its members
        costs = np.zeros(len(block))
        for a in range(problem.k):
            for b in range(problem.k):
                costs += problem.A[block[:, a], block[:, b]]
        i = int(np.argmin(costs))
        if costs[i] < best_cost:
            best_cost, best_idx = float(costs[i]), list(block[i])
    return best_idx


# --------------------------------------------------------------------- report
def print_comparison(problem, results):
    """results: {method name: list of picked indices}"""
    print(f"  {'method':<26}{'cost':>9}{'balance':>10}{'fit':>8}{'crowding':>10}")
    for name, idx in results.items():
        if idx is None:
            continue
        p = problem.parts(idx)
        print(f"  {name:<26}{p['total']:>9.4f}{p['balance']:>10.4f}"
              f"{p['fit']:>8.3f}{p['crowding']:>10.3f}")


def print_balance(data, idx_by_name):
    """Feature averages of each set next to the project's, in original units."""
    names = data['names']
    print(f"  {'feature':<14}{'project':>11}" + ''.join(f"{n:>26}" for n in idx_by_name))
    for j, feat in enumerate(names):
        row = f"  {feat:<14}{data['project_mean_raw'][j]:>11.3f}"
        for idx in idx_by_name.values():
            row += f"{data['raw'][idx, j].mean():>26.3f}"
        print(row)


def make_problem(data, k):
    return Problem(data['features'], data['project_mean'], data['delta'],
                   data['lon'], data['lat'], k)


def best_matches(data, idx):
    """For each picked parcel, the project cell it resembles most.
    Returns (project cell row numbers, distances in spread units)."""
    picked = data['features'][np.asarray(idx)]
    gaps = picked[:, None, :] - data['project'][None, :, :]
    distance = np.sqrt((gaps ** 2).mean(axis=2))           # picked parcels x project cells
    return distance.argmin(axis=1), distance.min(axis=1)


def save_matches(data, idx, path=MATCHES_CSV):
    """One row per picked parcel: where it is and which project cell it matches."""
    cells, distance = best_matches(data, idx)
    lonlat = data['project_lonlat']
    with open(path, 'w', newline='') as fh:
        writer = csv.writer(fh)
        writer.writerow(['parcel_id', 'parcel_lon', 'parcel_lat', 'project_cell_id',
                         'project_lon', 'project_lat', 'distance'])
        for i, c, d in zip(idx, cells, distance):
            r = data['rows'][i]
            writer.writerow([r['id'], r['lon'], r['lat'], data['project_ids'][c],
                             lonlat[c, 0] if lonlat is not None else '',
                             lonlat[c, 1] if lonlat is not None else '', f'{d:.4f}'])


def save_selected(data, idx, path=SELECTED_CSV):
    with open(path, 'w', newline='') as fh:
        writer = csv.writer(fh)
        writer.writerow(['id', 'lon', 'lat', 'similarity'] + data['names'])
        for i in sorted(idx):
            r = data['rows'][i]
            writer.writerow([r['id'], r['lon'], r['lat'], r['similarity']]
                            + [r[n] for n in data['names']])


# ----------------------------------------------------------------------- main
def main():
    data = load_pool()
    n = len(data['rows'])
    print(f"Pool: {n} candidates, {len(data['names'])} features")

    # ---- full-size problem: classical methods only
    print(f"\n1. Full problem: pick {K} of {n} (classical methods)")
    full = make_problem(data, K)
    results = {
        'top-K by similarity': top_k_similarity(full),
        'greedy + swaps': greedy_swap(full),
        'simulated annealing': simulated_annealing(full),
    }
    print_comparison(full, results)
    best_name = min(results, key=lambda m: full.cost(results[m]))
    best = results[best_name]
    print(f"\n  Best found: {best_name}. Feature averages against the project:")
    print_balance(data, {'top-K by similarity': results['top-K by similarity'],
                         best_name: best})
    save_selected(data, best)
    save_matches(data, best)
    print(f"\n  Saved {SELECTED_CSV} and {MATCHES_CSV}")

    # ---- reduced problem: small enough for a simulator or a real device
    print(f"\n2. Reduced problem: pick {SMALL_K} of {SMALL_POOL} (exact answer known)")
    from qaoa import run_qaoa            # imported here so step 1 works without Qiskit
    sub = full.subset(SMALL_POOL)
    sdata = dict(data, features=data['features'][sub], delta=data['delta'][sub],
                 lon=data['lon'][sub], lat=data['lat'][sub])
    small = make_problem(sdata, SMALL_K)
    small_results = {
        'top-K by similarity': top_k_similarity(small),
        'greedy + swaps': greedy_swap(small),
        'simulated annealing': simulated_annealing(small, restarts=3, steps=5000),
        'exact (all sets)': exact_best(small),
    }
    print_comparison(small, small_results)
    for p in QAOA_LAYERS:
        out = run_qaoa(small, layers=p, seed=SEED)
        print(f"\n  QAOA, {p} layer(s), {SMALL_POOL} qubits (simulator):")
        print(f"    probability of the optimal set      {out['p_optimal']:.4f}"
              f"   (random guess: {out['p_optimal_random']:.4f})")
        print(f"    approximation ratio                 {out['ratio']:.3f}"
              f"   (random guess: {out['ratio_random']:.3f})")
        print(f"    best of {out['shots']} sampled sets: cost {out['best_sampled_cost']:.4f}"
              f" vs optimum {out['optimal_cost']:.4f}")
        print(f"    two-qubit gates before routing: {out['two_qubit_gates']},"
              f" circuit depth: {out['depth']}")
        report_hardware_cost(small, p)


def report_hardware_cost(problem, layers):
    """Size of the circuit after routing onto a model of a 127-qubit IBM chip."""
    try:
        from qiskit_ibm_runtime.fake_provider import FakeBrisbane
        from qaoa import hardware_cost
    except ImportError:
        return
    cost = hardware_cost(problem, layers, FakeBrisbane())
    print(f"    on a 127-qubit IBM chip model: {cost['two_qubit_gates']} two-qubit gates,"
          f" depth {cost['depth']} (start-state preparation not included)")


if __name__ == '__main__':
    main()