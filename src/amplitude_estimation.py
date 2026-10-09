"""Quantum amplitude estimation (QAE) of the comparison set's average forest loss.

    python src/amplitude_estimation.py

The estimation step of the project averages a quantity over the comparison parcels. If that
average can only be reached by sampling (one parcel and one random outcome per evaluation),
classical Monte Carlo needs about 1/e^2 evaluations for error e, while amplitude estimation
needs about 1/e. This is a proven quadratic speedup in the number of evaluations, unlike QAOA.

This script shows that scaling on the real selected parcels (outputs/selected_parcels.csv):

    * a state-preparation circuit A puts the 50 parcels in superposition and rotates an extra
      qubit so that P(qubit = 1) equals the mean recent loss rate (scaled to 0..1)
    * the Grover operator Q built from A is applied m times for m = 0, 1, 2, 4, ...
    * the estimate is the maximum-likelihood value from the measured counts (MLAE)
    * the same number of evaluations is given to ordinary Monte Carlo for comparison

It also shows why this does not run on today's hardware: Q is a deep circuit, every
two-qubit gate fails about 0.3% of the time, and the quantum advantage disappears once the
circuit is deep enough for the speedup to matter. Circuits are simulated exactly here.

Honest limits: with all 50 parcel values in hand, the average is one line of code. QAE
only matters when the values must be sampled from something expensive, which is not the case
for this project at its current size. This is a statement about what the algorithm offers,
not about what this project needs today.
"""

import csv
import math

import numpy as np
from qiskit import QuantumCircuit, transpile
from qiskit.circuit.library import StatePreparation, UCRYGate, grover_operator
from qiskit.quantum_info import Statevector

from config import OUTPUT_DIR, SELECTED_CSV
from select_parcels import SEED

QAE_JSON = OUTPUT_DIR / 'qae_result.json'
QAE_PLOT = OUTPUT_DIR / 'qae_scaling.png'
SCHEDULE = [0, 1, 2, 4, 8, 16, 32, 64]       # Grover powers m
SHOTS = 100                                  # shots per power
TRIALS = 200                                 # repeats used to measure the typical error
GATE_ERRORS = {'perfect (error-corrected)': 0.0, '10x better than today': 3e-4,
               'today (0.3%)': 3e-3}
BASIS = ['cz', 'rz', 'sx', 'x']


# --------------------------------------------------------------------------- circuits
def load_values():
    """Recent forest-loss rate of each selected parcel, scaled to 0..1."""
    with open(SELECTED_CSV, newline='') as fh:
        loss = np.array([float(r['loss_recent']) for r in csv.DictReader(fh)])
    return loss / loss.max(), loss.max()


def build_a(values):
    """Circuit A: P(last qubit = 1) = mean of `values`, over a uniform choice of parcel."""
    n = len(values)
    bits = math.ceil(math.log2(n))
    amplitudes = np.zeros(2 ** bits)
    amplitudes[:n] = 1 / math.sqrt(n)
    angles = np.zeros(2 ** bits)
    angles[:n] = 2 * np.arcsin(np.sqrt(values))
    a = QuantumCircuit(bits + 1)
    a.append(StatePreparation(amplitudes), range(bits))
    a.append(UCRYGate(list(angles)), [bits] + list(range(bits)))   # target first, then controls
    return a


def good_probabilities(a, schedule):
    """P(last qubit = 1) after Q^m A|0>, for every m in the schedule (exact simulation)."""
    n = a.num_qubits
    oracle = QuantumCircuit(n)
    oracle.z(n - 1)                                        # marks the 'loss' outcome
    q = grover_operator(oracle, state_preparation=a)
    state = Statevector(a)
    probs, previous = {}, 0
    for m in sorted(schedule):
        for _ in range(m - previous):
            state = state.evolve(q)
        previous = m
        probs[m] = float(state.probabilities([n - 1])[1])
    return probs, a, q


def gate_counts(a, q):
    """Two-qubit gates in A and in Q, compiled to a CZ gate set (before chip routing)."""
    count = lambda qc: transpile(qc, basis_gates=BASIS, optimization_level=1,
                                 seed_transpiler=SEED).count_ops().get('cz', 0)
    return count(a), count(q)


# -------------------------------------------------------------------------- estimators
def visibility(m, g_a, g_q, error):
    """Share of the signal left after the circuit's gates, if each gate fails with `error`."""
    return math.exp(-error * (g_a + m * g_q))


def mlae(hits, shots, schedule, g_a, g_q, error):
    """Maximum-likelihood estimate of a = sin^2(theta) from the counts at each power m."""
    theta = np.linspace(1e-6, math.pi / 2 - 1e-6, 20001)
    loglik = np.zeros_like(theta)
    for m, h in zip(schedule, hits):
        p = np.sin((2 * m + 1) * theta) ** 2
        v = visibility(m, g_a, g_q, error)
        p = 0.5 + (p - 0.5) * v                            # known noise shrinks the contrast
        loglik += h * np.log(p) + (shots - h) * np.log(1 - p)
    return float(np.sin(theta[np.argmax(loglik)]) ** 2)


def quantum_estimates(probs, powers, g_a, g_q, error, rng, trials):
    """Estimates of a from `trials` repeats of the experiment using the first `powers`."""
    schedule = SCHEDULE[:powers]
    estimates = []
    for _ in range(trials):
        hits = []
        for m in schedule:
            p = 0.5 + (probs[m] - 0.5) * visibility(m, g_a, g_q, error)
            hits.append(rng.binomial(SHOTS, p))
        estimates.append(mlae(hits, SHOTS, schedule, g_a, g_q, error))
    return np.array(estimates)


def evaluations(powers):
    """Calls of A used: each circuit with m Grover steps calls A 2m + 1 times."""
    return sum(SHOTS * (2 * m + 1) for m in SCHEDULE[:powers])


# ----------------------------------------------------------------------------- report
def main():
    values, vmax = load_values()
    a_true = float(values.mean())
    rng = np.random.default_rng(SEED)
    print(f'{len(values)} selected parcels; mean recent loss {a_true * vmax:.5f} '
          f'(scaled amplitude a = {a_true:.4f})')

    probs, a_circuit, q = good_probabilities(build_a(values), SCHEDULE)
    theta = math.asin(math.sqrt(a_true))
    check = max(abs(probs[m] - math.sin((2 * m + 1) * theta) ** 2) for m in SCHEDULE)
    print(f'Circuit check: P(loss) after m Grover steps matches sin^2((2m+1)theta), '
          f'largest difference {check:.1e}')
    g_a, g_q = gate_counts(a_circuit, q)
    print(f'Gate counts (CZ, before chip routing): state preparation A {g_a}, '
          f'Grover step Q {g_q}')

    results = {'a': a_true, 'gates_a': g_a, 'gates_q': g_q, 'curves': {}}
    print('\nTypical error of the estimate (RMS over %d repeats), in units of the true value'
          % TRIALS)
    print(f"  {'evaluations of A':>17}{'Monte Carlo':>13}"
          + ''.join(f'{name:>27}' for name in GATE_ERRORS))
    table = []
    for powers in range(1, len(SCHEDULE) + 1):
        n_eval = evaluations(powers)
        classical = rng.binomial(n_eval, a_true, size=TRIALS) / n_eval
        row = {'evaluations': n_eval,
               'monte_carlo': float(np.sqrt(np.mean((classical - a_true) ** 2)) / a_true)}
        for name, error in GATE_ERRORS.items():
            est = quantum_estimates(probs, powers, g_a, g_q, error, rng, TRIALS)
            row[name] = float(np.sqrt(np.mean((est - a_true) ** 2)) / a_true)
        table.append(row)
        print(f"  {n_eval:>17,}{row['monte_carlo']:>13.2%}"
              + ''.join(f'{row[name]:>27.2%}' for name in GATE_ERRORS))
    results['table'] = table

    # log-log slope of error against evaluations: -0.5 classical, close to -1 for QAE
    x = np.log([r['evaluations'] for r in table])
    slopes = {name: float(np.polyfit(x, np.log([r[name] for r in table]), 1)[0])
              for name in ['monte_carlo'] + list(GATE_ERRORS)}
    print('\nHow fast the error falls as evaluations grow (slope on log-log axes):')
    for name, slope in slopes.items():
        print(f'  {name:<28}{slope:>7.2f}')
    print('  Monte Carlo is about -0.5. Perfect QAE approaches -1, which is the quadratic '
          'speedup.')
    results['slopes'] = slopes

    # what a certification-grade precision would need
    print('\nEvaluations of A needed for a target relative error on the mean loss:')
    print(f"  {'target':>8}{'Monte Carlo':>16}{'QAE':>14}{'gates in the QAE circuit':>28}"
          f"{'gate error for 50% success':>30}")
    needs = []
    for target in (0.10, 0.01, 0.001):
        classical_n = (1 - a_true) / (a_true * target ** 2)
        grover_calls = 2 * math.pi * math.sqrt(a_true * (1 - a_true)) / (a_true * target)
        gates = g_a + grover_calls * g_q
        needs.append({'target': target, 'monte_carlo': classical_n, 'qae': grover_calls,
                      'qae_gates': gates, 'gate_error': math.log(2) / gates})
        print(f"  {target:>8.1%}{classical_n:>16,.0f}{grover_calls:>14,.0f}{gates:>28,.0f}"
              f"{math.log(2) / gates:>30.2e}")
    print('  QAE constants are approximate. The last column is the gate error at which the '
          'deepest QAE circuit runs\n  error-free half the time, without error correction. '
          'Today it is about 3e-3.')
    results['needs'] = needs

    import json
    with open(QAE_JSON, 'w') as fh:
        json.dump(results, fh, indent=2)
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(7, 4.5))
        evals = [r['evaluations'] for r in table]
        ax.loglog(evals, [r['monte_carlo'] for r in table], 'k-o', label='Monte Carlo (classical)')
        for (name, _), style in zip(GATE_ERRORS.items(), ('tab:green', 'tab:blue', 'tab:red')):
            ax.loglog(evals, [r[name] for r in table], '-s', color=style, label=f'QAE, {name}')
        ax.set_xlabel('evaluations of the state preparation A')
        ax.set_ylabel('typical relative error of the estimate')
        ax.set_title('Amplitude estimation: error against evaluations')
        ax.grid(True, which='both', alpha=0.3)
        ax.legend(fontsize=8)
        fig.tight_layout()
        fig.savefig(QAE_PLOT, dpi=150)
        print(f'\nSaved {QAE_JSON} and {QAE_PLOT}')
    except Exception as err:
        print(f'\nSaved {QAE_JSON} (plot skipped: {err})')


if __name__ == '__main__':
    main()
