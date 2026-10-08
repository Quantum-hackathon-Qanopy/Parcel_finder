"""QAOA for the parcel selection problem, run on a Qiskit simulator.

The problem is  minimise x^T A x + const  with exactly K of N bits set (select_parcels.py).

Writing each bit as x_i = (1 - Z_i) / 2 turns the cost into an Ising energy
    E = const' + sum_i h_i Z_i + sum_{i<j} J_ij Z_i Z_j
which a quantum circuit can apply as phase rotations:

    start   equal superposition of all states with exactly K ones (a "Dicke state")
    repeat  cost layer:  RZZ(J_ij) on every pair, RZ(h_i) on every qubit
            mixer layer: XY gates around a ring of qubits
    measure the qubits; each result is a candidate set of parcels

The XY mixer never changes how many ones there are, so every result has exactly K
parcels and no penalty term is needed. The angles (gamma, beta) are tuned by a
classical optimiser to lower the average cost of the measured sets.

On a simulator the Dicke start state is loaded directly. On a real device it would be
built from gates (O(N*K) of them), which adds depth, as would routing the dense
RZZ layer onto the chip's limited qubit connections. hardware_cost() measures that.
"""

import math

import numpy as np
from scipy.optimize import minimize
from qiskit import QuantumCircuit, transpile
from qiskit.circuit import Parameter
from qiskit.circuit.library import XXPlusYYGate
from qiskit_aer import AerSimulator
from qiskit_aer.library import SetStatevector

MAX_QUBITS = 20            # statevector simulation needs 2^N numbers


# ------------------------------------------------------------------- Ising form
def ising_terms(A):
    """Fields h_i, couplings J_ij and constant for  x^T A x  with x_i = (1 - Z_i) / 2."""
    n = len(A)
    J = np.triu(A, 1) / 2.0
    h = -np.diag(A) / 2.0 - (A.sum(axis=1) - np.diag(A)) / 2.0
    offset = np.diag(A).sum() / 2.0 + np.triu(A, 1).sum() / 2.0
    return h, J, offset


def all_state_costs(problem):
    """Cost of all 2^N bitstrings (index bit i = qubit i) and which have exactly K ones."""
    n = problem.n
    states = np.arange(2 ** n)
    bits = ((states[:, None] >> np.arange(n)) & 1).astype(float)
    costs = np.einsum('si,ij,sj->s', bits, problem.A, bits) + problem.const
    return costs, bits.sum(axis=1) == problem.k


# ---------------------------------------------------------------------- circuit
def build_circuit(problem, layers, load_start_state=True):
    """Parametrised QAOA circuit. Returns (circuit, gammas, betas, scale).
    load_start_state=False leaves out the simulator-only state loading."""
    n = problem.n
    h, J, _ = ising_terms(problem.A)
    costs, valid = all_state_costs(problem)
    scale = 1.0 / max(costs[valid].std(), 1e-12)         # keeps gamma around 1

    gammas = [Parameter(f'g{i}') for i in range(layers)]
    betas = [Parameter(f'b{i}') for i in range(layers)]
    qc = QuantumCircuit(n)
    if load_start_state:
        qc.append(SetStatevector(dicke_amplitudes(n, problem.k)), range(n))
    ring = [(i, (i + 1) % n) for i in range(n)]
    for g, b in zip(gammas, betas):
        for i in range(n):
            for j in range(i + 1, n):
                if J[i, j] != 0:
                    qc.rzz(2 * g * J[i, j] * scale, i, j)
        for i in range(n):
            qc.rz(2 * g * h[i] * scale, i)
        for i, j in ring:
            qc.append(XXPlusYYGate(2 * b), [i, j])
    return qc, gammas, betas, scale


def dicke_amplitudes(n, k):
    """Equal superposition over all n-bit strings that have exactly k ones."""
    states = np.arange(2 ** n)
    ones = np.array([bin(s).count('1') for s in states])
    amp = (ones == k).astype(complex)
    return amp / np.linalg.norm(amp)


# ------------------------------------------------------------------------ run
def run_qaoa(problem, layers=1, seed=0, n_starts=4, maxiter=120, shots=1000):
    """Tune the angles on a statevector simulator and report how good the result is."""
    if problem.n > MAX_QUBITS:
        raise ValueError(f'{problem.n} qubits is too many to simulate (limit {MAX_QUBITS})')
    rng = np.random.default_rng(seed)
    costs, valid = all_state_costs(problem)
    valid_costs = costs[valid]
    best_cost, worst_cost = valid_costs.min(), valid_costs.max()
    mean_random = valid_costs.mean()
    optimal_states = np.flatnonzero(valid & np.isclose(costs, best_cost))

    qc, gammas, betas, _ = build_circuit(problem, layers)
    sim = AerSimulator(method='statevector')
    measured = qc.copy()
    measured.save_statevector()
    compiled = transpile(measured, sim, optimization_level=1)
    parameters = list(gammas) + list(betas)

    def probabilities(angles):
        bound = compiled.assign_parameters(dict(zip(parameters, angles)))
        state = sim.run(bound).result().get_statevector().data
        return np.abs(state) ** 2

    def expected_cost(angles):
        return float(probabilities(angles) @ costs)

    best = None
    for _ in range(n_starts):
        start = np.concatenate([rng.uniform(0, 1.5, layers), rng.uniform(0, math.pi / 2, layers)])
        res = minimize(expected_cost, start, method='COBYLA', options={'maxiter': maxiter})
        if best is None or res.fun < best.fun:
            best = res

    probs = probabilities(best.x)
    probs = probs / probs.sum()
    sampled = rng.choice(len(probs), size=shots, p=probs)
    transpiled_gates = qc.count_ops()
    return {
        'angles': best.x.tolist(),
        'expected_cost': float(best.fun),
        'optimal_cost': float(best_cost),
        'ratio': float((worst_cost - best.fun) / (worst_cost - best_cost)),
        'ratio_random': float((worst_cost - mean_random) / (worst_cost - best_cost)),
        'p_optimal': float(probs[optimal_states].sum()),
        'p_optimal_random': float(len(optimal_states) / valid.sum()),
        'best_sampled_cost': float(costs[sampled].min()),
        'best_sampled_set': [int(i) for i in np.flatnonzero(
            (sampled[np.argmin(costs[sampled])] >> np.arange(problem.n)) & 1)],
        'shots': shots,
        'two_qubit_gates': int(transpiled_gates.get('rzz', 0) + transpiled_gates.get('xx_plus_yy', 0)),
        'depth': int(qc.depth()),
    }


def hardware_cost(problem, layers, backend):
    """Gate count and depth after routing onto a real or fake device (a Qiskit backend)."""
    qc, gammas, betas, _ = build_circuit(problem, layers, load_start_state=False)
    qc = qc.assign_parameters({p: 0.1 for p in list(gammas) + list(betas)})
    compiled = transpile(qc, backend, optimization_level=3, seed_transpiler=0)
    ops = compiled.count_ops()
    two_qubit = sum(count for name, count in ops.items() if name in ('cz', 'ecr', 'cx', 'iswap'))
    return {'two_qubit_gates': int(two_qubit), 'depth': int(compiled.depth())}