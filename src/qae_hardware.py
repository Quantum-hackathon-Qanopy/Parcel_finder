"""Quantum amplitude estimation on a real IBM quantum computer.

    python src/qae_hardware.py                              # noisy simulation of ibm_fez (free)
    python src/qae_hardware.py --ibm --backend ibm_fez      # the real 156-qubit device

Two experiments, run in one job:

    A. Small QAE. One qubit is rotated so that P(1) equals the mean recent loss rate of the
       selected parcels. The Grover step Q is applied m = 0, 1, 2, 4, ... 64 times and the
       value is estimated by maximum likelihood (MLAE). Its error is compared with plain
       Monte Carlo using the same number of calls of A. One qubit keeps every circuit short
       enough to survive on today's hardware, so this tests the quadratic speedup itself.
    B. The real circuit. The 50 selected parcels (6 qubits plus 1) at m = 0, 1, 2. This shows
       how fast the signal is lost once the circuit has the size of the real problem.

Setup and key handling are the same as in qaoa_hardware.py. Each job uses a few seconds of
QPU time. Results go to outputs/<project>/qae_hardware_result.json.
"""

import argparse
import json
import math

import numpy as np
from qiskit import QuantumCircuit, transpile
from qiskit.circuit.library import grover_operator

from amplitude_estimation import build_a, gate_counts, load_values
from config import OUTPUT_DIR
from select_parcels import SEED

HARDWARE_JSON = OUTPUT_DIR / 'qae_hardware_result.json'
SCHEDULE = [0, 1, 2, 4, 8, 16, 32, 64]           # Grover steps for the small experiment
SHOTS_SMALL = 1000
REAL_POWERS = [0, 1, 2]                          # Grover steps for the 50-parcel circuit
SHOTS_REAL = 4000
SHOTS_CAL = 4000


# ------------------------------------------------------------------------- circuits
def qae_circuit(a_circuit, m):
    """A, then m Grover steps (barriers keep the compiler from merging them), then measure."""
    n = a_circuit.num_qubits
    oracle = QuantumCircuit(n)
    oracle.z(n - 1)
    q = grover_operator(oracle, state_preparation=a_circuit)
    qc = QuantumCircuit(n)
    qc.compose(a_circuit, inplace=True)
    for _ in range(m):
        qc.barrier()
        qc.compose(q, inplace=True)
    qc.measure_all()
    return qc


def small_a(a_true):
    qc = QuantumCircuit(1)
    qc.ry(2 * math.asin(math.sqrt(a_true)), 0)
    return qc


def best_qubit(backend):
    """The physical qubit with the lowest readout error, for the one-qubit experiment."""
    try:
        errors = {q: backend.target['measure'][(q,)].error for q in range(backend.num_qubits)
                  if (q,) in backend.target['measure'] and backend.target['measure'][(q,)].error is not None}
        return min(errors, key=errors.get)
    except Exception:                                        # the simulator has no such data
        return 0


def p_one(counts, n):
    """P(last qubit = 1) from counts of an n-qubit measurement."""
    shots = sum(counts.values())
    return sum(c for key, c in counts.items() if key.replace(' ', '')[0] == '1') / shots


# ------------------------------------------------------------------------ estimators
def mlae(probs, shots, schedule, decay=0.0):
    """Maximum-likelihood a = sin^2(theta) from P(1) at each power m. `decay` shrinks the
    contrast like exp(-decay * m), to fit the loss of signal on a noisy device."""
    theta = np.linspace(1e-6, math.pi / 2 - 1e-6, 20001)
    loglik = np.zeros_like(theta)
    for m, p in zip(schedule, probs):
        model = 0.5 + (np.sin((2 * m + 1) * theta) ** 2 - 0.5) * math.exp(-decay * m)
        hits = p * shots
        loglik += hits * np.log(model) + (shots - hits) * np.log(1 - model)
    return float(np.sin(theta[np.argmax(loglik)]) ** 2), float(loglik.max())


def mlae_fit_decay(probs, shots, schedule):
    """MLAE with the decay of the signal fitted from the data (the best of a grid)."""
    best = None
    for decay in np.linspace(0, 0.08, 33):
        a, ll = mlae(probs, shots, schedule, decay)
        if best is None or ll > best[1]:
            best = (a, ll, float(decay))
    return best[0], best[2]


def evaluations(schedule, shots):
    return sum(shots * (2 * m + 1) for m in schedule)


# ------------------------------------------------------------------------------ main
def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--ibm', action='store_true', help='use a real IBM device')
    parser.add_argument('--backend', help='device name, for example ibm_fez')
    parser.add_argument('--no-suppress', action='store_true', help='switch error suppression off')
    args = parser.parse_args()

    from qaoa_hardware import pick_backend, run_job
    values, vmax = load_values()
    a_true = float(values.mean())
    print(f'Mean recent loss of the selected parcels, scaled: a = {a_true:.4f}')
    backend, kind = pick_backend(args.ibm, args.backend, 7)
    print(f'Backend: {backend.name} ({"real device" if kind == "ibm" else "noisy simulation"})')

    qubit = best_qubit(backend)
    a1 = small_a(a_true)
    small = [transpile(qae_circuit(a1, m), backend, initial_layout=[qubit], optimization_level=1,
                       seed_transpiler=SEED) for m in SCHEDULE]
    cal = []
    for flip in (0, 1):
        qc = QuantumCircuit(1)
        if flip:
            qc.x(0)
        qc.measure_all()
        cal.append(transpile(qc, backend, initial_layout=[qubit], optimization_level=0))
    a_real = build_a(values)
    real = [transpile(qae_circuit(a_real, m), backend, optimization_level=3, seed_transpiler=SEED)
            for m in REAL_POWERS]
    two_q = lambda qc: sum(qc.count_ops().get(g, 0) for g in ('cz', 'ecr', 'cx'))
    print(f'Physical qubit {qubit} for the small experiment. Compiled sizes: '
          f'small m=64: {small[-1].size()} gates; real circuit m=0/1/2: '
          f'{[two_q(c) for c in real]} two-qubit gates')

    suppress = not args.no_suppress
    jobs = [(small, SHOTS_SMALL), (cal, SHOTS_CAL), (real, SHOTS_REAL)]
    counts, usage, job_ids = [], 0.0, []
    for circuits, shots in jobs:
        out, job_id, seconds = run_job(circuits, backend, kind, shots, suppress)
        counts.append(out)
        job_ids.append(job_id)
        usage += seconds or 0.0
    small_counts, cal_counts, real_counts = counts

    # ---- experiment A
    e0 = p_one(cal_counts[0], 1)                       # prepared 0, read 1
    e1 = 1 - p_one(cal_counts[1], 1)                   # prepared 1, read 0
    raw = [p_one(c, 1) for c in small_counts]
    corrected = [min(max((p - e0) / (1 - e0 - e1), 0.0), 1.0) for p in raw]
    theta = math.asin(math.sqrt(a_true))
    ideal = [math.sin((2 * m + 1) * theta) ** 2 for m in SCHEDULE]
    print(f'\nA. Small QAE on one qubit. Readout error: 0->1 {e0:.2%}, 1->0 {e1:.2%}')
    print(f"  {'m':>3}{'ideal P(1)':>12}{'measured':>10}{'corrected':>11}")
    for m, i, r, c in zip(SCHEDULE, ideal, raw, corrected):
        print(f'  {m:>3}{i:>12.3f}{r:>10.3f}{c:>11.3f}')
    table = []
    print(f"\n  {'Grover steps up to':>19}{'calls of A':>12}{'Monte Carlo error':>19}"
          f"{'QAE (ideal model)':>19}{'QAE (fitted decay)':>20}")
    for upto in range(1, len(SCHEDULE) + 1):
        sched, probs = SCHEDULE[:upto], corrected[:upto]
        n_eval = evaluations(sched, SHOTS_SMALL)
        mc_error = math.sqrt((1 - a_true) / (a_true * n_eval))         # relative RMS error
        a_ideal, _ = mlae(probs, SHOTS_SMALL, sched)
        a_fit, decay = mlae_fit_decay(probs, SHOTS_SMALL, sched)
        row = {'max_m': sched[-1], 'evaluations': n_eval, 'monte_carlo_rel_error': mc_error,
               'qae_ideal_estimate': a_ideal, 'qae_ideal_rel_error': abs(a_ideal - a_true) / a_true,
               'qae_fitted_estimate': a_fit, 'qae_fitted_rel_error': abs(a_fit - a_true) / a_true,
               'fitted_decay_per_step': decay}
        table.append(row)
        print(f"  {sched[-1]:>19}{n_eval:>12,}{mc_error:>19.2%}{row['qae_ideal_rel_error']:>19.2%}"
              f"{row['qae_fitted_rel_error']:>20.2%}")

    # ---- experiment B
    g_a, g_q = gate_counts(a_real, grover_operator(_oracle(a_real), state_preparation=a_real))
    real_rows = []
    print('\nB. The 50-parcel circuit (7 qubits), no readout correction')
    print(f"  {'m':>3}{'two-qubit gates':>17}{'ideal P(1)':>12}{'measured':>10}{'signal left':>13}")
    for m, circuit, c in zip(REAL_POWERS, real, real_counts):
        ideal_p = math.sin((2 * m + 1) * theta) ** 2
        p = p_one(c, 7)
        left = (p - 0.5) / (ideal_p - 0.5)
        real_rows.append({'m': m, 'two_qubit_gates': two_q(circuit), 'ideal': ideal_p, 'measured': p,
                          'signal_left': left})
        print(f'  {m:>3}{two_q(circuit):>17}{ideal_p:>12.3f}{p:>10.3f}{left:>13.0%}')

    result = {'backend': backend.name, 'real_device': kind == 'ibm', 'suppression': suppress,
              'a': a_true, 'physical_qubit': qubit, 'readout_error': {'zero_to_one': e0, 'one_to_zero': e1},
              'small': {'schedule': SCHEDULE, 'shots': SHOTS_SMALL, 'ideal': ideal, 'raw': raw,
                        'corrected': corrected, 'table': table},
              'real_circuit': {'shots': SHOTS_REAL, 'rows': real_rows, 'gates_a': g_a, 'gates_q': g_q},
              'job_ids': job_ids, 'qpu_seconds': usage or None}
    HARDWARE_JSON.write_text(json.dumps(result, indent=1))
    print(f'\nSaved {HARDWARE_JSON}' + (f'; QPU time used {usage:.0f} s' if usage else ''))


def _oracle(a_circuit):
    qc = QuantumCircuit(a_circuit.num_qubits)
    qc.z(a_circuit.num_qubits - 1)
    return qc


if __name__ == '__main__':
    main()
