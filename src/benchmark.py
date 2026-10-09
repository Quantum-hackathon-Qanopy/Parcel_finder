"""Compare the quantum run with classical methods on the same small problem.

    python src/benchmark.py

Needs outputs/qaoa_hardware_result.json from qaoa_hardware.py (run that first, ideally with
--ibm). Costs no QPU time: the classical methods run here, and the quantum numbers are read
from the saved file. It prints two things:

    1. a table for the 8-parcel problem: exact search, greedy, simulated annealing, random
       search (each given the same budget as the quantum run) and QAOA on the chip
    2. how the circuit grows with problem size, and how accurate the gates would need to be
       for the circuit to succeed half the time. This is why QAOA cannot yet be run on the
       real 200-candidate problem.
"""

import argparse
import json
import math
import time

import numpy as np
from qiskit import QuantumCircuit, transpile
from qiskit.circuit.library import XXPlusYYGate

from config import OUTPUT_DIR
from qaoa import ising_terms
from qaoa_hardware import HARDWARE_JSON, TWO_QUBIT_GATES
from select_parcels import (SEED, exact_best, greedy_swap, load_pool, make_problem,
                            reduced_problem, simulated_annealing)

BENCHMARK_JSON = OUTPUT_DIR / 'benchmark_result.json'
TRIALS = 20                                  # repeats of each randomised classical method
SCALING_SIZES = (8, 16, 32, 64, 100)         # number of qubits for the scaling table
GATE_ERROR_TODAY = 0.003                     # typical two-qubit error on IBM Heron, about 0.3%


def timed(function, *args, **kwargs):
    start = time.perf_counter()
    result = function(*args, **kwargs)
    return result, time.perf_counter() - start


def classical_rows(problem, budget):
    """Run each classical method; returns rows with its success rate and mean runtime."""
    optimum_idx, t_exact = timed(exact_best, problem)
    optimum = problem.cost(optimum_idx)
    hit = lambda idx: math.isclose(problem.cost(idx), optimum, rel_tol=1e-9)
    n_sets = math.comb(problem.n, problem.k)
    rows = [{'method': 'Exact (all sets)', 'found': 1.0, 'per_attempt': 1.0,
             'budget': f'{n_sets} sets', 'seconds': t_exact}]

    idx, seconds = timed(greedy_swap, problem)
    rows.append({'method': 'Greedy + swaps', 'found': float(hit(idx)), 'per_attempt': float(hit(idx)),
                 'budget': 'deterministic', 'seconds': seconds})

    results = [timed(simulated_annealing, problem, restarts=1, steps=budget, seed=SEED + t)
               for t in range(TRIALS)]
    wins = [hit(idx) for idx, _ in results]
    rows.append({'method': 'Simulated annealing', 'found': float(np.mean(wins)),
                 'per_attempt': float(np.mean(wins)), 'budget': f'{budget} steps per run',
                 'seconds': float(np.mean([s for _, s in results]))})

    found = []
    start = time.perf_counter()
    for t in range(TRIALS):
        rng = np.random.default_rng(SEED + t)
        sets = [rng.permutation(problem.n)[:problem.k] for _ in range(budget)]
        found.append(any(hit(s) for s in sets))
    seconds = (time.perf_counter() - start) / TRIALS
    rows.append({'method': 'Random search', 'found': float(np.mean(found)),
                 'per_attempt': 1.0 / n_sets, 'budget': f'{budget} random sets per run',
                 'seconds': seconds})
    return rows, optimum


def quantum_rows(saved, optimum):
    """Rows for the saved QAOA hardware runs."""
    rows = []
    for method, items in saved['runs'].items():
        raw = [it['stages']['raw'] for it in items]
        post = [it['stages'].get('+ postselect') or {} for it in items]
        found = [bool(r.get('best_cost_seen') is not None
                      and math.isclose(r['best_cost_seen'], optimum, rel_tol=1e-6))
                 for r in raw]
        seconds = [it['qpu_seconds'] for it in items if it['qpu_seconds']]
        label = 'QAOA, IBM ' + saved['backend'] + (' (error suppression)' if method == 'suppressed' else '')
        rows.append({'method': label, 'found': float(np.mean(found)),
                     'per_attempt': float(np.mean([r['p_optimal'] for r in raw])),
                     'budget': f"{saved['shots']} shots per job",
                     'seconds': float(np.mean(seconds)) if seconds else None})
        if method == 'raw':
            rows.append({'method': '  + readout fix + keep valid',
                         'found': rows[-1]['found'],
                         'per_attempt': float(np.mean([p.get('p_optimal_given_valid',
                                                             p.get('p_optimal', 0))
                                                       for p in post])),
                         'budget': 'of the valid shots', 'seconds': rows[-1]['seconds']})
    return rows


def print_rows(rows, n_qubits, k):
    print(f'\nPick {k} of {n_qubits} parcels  (same problem and cost for every method)')
    print(f"  {'method':<36}{'found optimum':>14}{'success/attempt':>17}  {'budget':<24}{'time':>12}")
    for r in rows:
        seconds = r['seconds']
        time_text = '-' if seconds is None else (f'{seconds * 1000:.1f} ms' if seconds < 1
                                                 else f'{seconds:.1f} s')
        print(f"  {r['method']:<36}{r['found']:>14.0%}{r['per_attempt']:>17.1%}  "
              f"{r['budget']:<24}{time_text:>12}")
    print('  (QAOA time is QPU time per job, without queueing. "found optimum" means the best '
          'set seen was optimal.)')


def scaling_rows(layers, backend):
    """Two-qubit gates of the QAOA circuit after routing onto the chip, by problem size."""
    rows = []
    for n in SCALING_SIZES:
        k = max(3, n // 5)
        problem = make_problem(load_pool(pool_size=n), k)
        h, J, _ = ising_terms(problem.A)
        qc = QuantumCircuit(n)
        for q in range(k):
            qc.x(q)
        for _ in range(layers):
            for i in range(n):
                for j in range(i + 1, n):
                    if J[i, j] != 0:
                        qc.rzz(0.1, i, j)
            for i in range(n):
                qc.rz(0.1, i)
            for i in range(n):
                qc.append(XXPlusYYGate(0.2), [i, (i + 1) % n])
        compiled = transpile(qc, backend, optimization_level=1, seed_transpiler=SEED)
        gates = sum(c for name, c in compiled.count_ops().items() if name in TWO_QUBIT_GATES)
        rows.append({'qubits': n, 'k': k, 'gates': int(gates), 'depth': int(compiled.depth()),
                     'survive_today': float(math.exp(-gates * GATE_ERROR_TODAY)),
                     'error_needed': math.log(2) / gates})
    return rows


def print_scaling(rows, layers):
    print(f'\nHow the circuit grows ({layers} QAOA layers, compiled for a 156-qubit chip)')
    print(f"  {'qubits':>7}{'pick':>6}{'2-qubit gates':>15}{'depth':>8}"
          f"{'no-error chance today':>24}{'gate error needed':>20}")
    for r in rows:
        print(f"  {r['qubits']:>7}{r['k']:>6}{r['gates']:>15,}{r['depth']:>8,}"
              f"{r['survive_today']:>24.2e}{r['error_needed']:>20.5%}")
    print(f"  'no-error chance today' assumes a {GATE_ERROR_TODAY:.1%} error per two-qubit gate. "
          "'gate error needed' is the error\n  at which the whole circuit runs error-free "
          "half the time, with no error correction.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--skip-scaling', action='store_true', help='skip the gate-count table')
    args = parser.parse_args()
    if not HARDWARE_JSON.exists():
        raise SystemExit(f'{HARDWARE_JSON} not found. Run qaoa_hardware.py first.')
    with open(HARDWARE_JSON) as fh:
        saved = json.load(fh)
    n_qubits, k, layers = saved['n_qubits'], saved['k'], saved['layers']

    data = load_pool()
    small, _ = reduced_problem(data, make_problem(data, 50), n_qubits, k)
    rows, optimum = classical_rows(small, saved['shots'])
    rows += quantum_rows(saved, optimum)
    print_rows(rows, n_qubits, k)

    result = {'problem': {'qubits': n_qubits, 'k': k, 'backend': saved['backend'],
                          'real_device': saved['real_device']}, 'methods': rows}
    if not args.skip_scaling:
        from qiskit_ibm_runtime.fake_provider import FakeFez
        scaling = scaling_rows(layers, FakeFez())
        print_scaling(scaling, layers)
        result['scaling'] = scaling
    with open(BENCHMARK_JSON, 'w') as fh:
        json.dump(result, fh, indent=2)
    print(f'\nSaved {BENCHMARK_JSON}')


if __name__ == '__main__':
    main()
