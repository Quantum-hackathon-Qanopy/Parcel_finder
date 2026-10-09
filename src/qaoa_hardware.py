"""Run the QAOA parcel selection on an IBM quantum computer.

    python src/qaoa_hardware.py                          # noisy simulation of a 156-qubit chip
    python src/qaoa_hardware.py --ibm --backend ibm_fez  # a named 156-qubit Heron device
    python src/qaoa_hardware.py --ibm --backend ibm_fez --repeats 3   # error bars
    python src/qaoa_hardware.py --qubits 10 --k 4        # a bigger problem (more noise)

Steps: build a small version of the selection problem, tune the QAOA angles on a perfect
simulator, run the tuned circuit on the chosen backend, and compare what comes back with the
perfect simulation and with random guessing. Real devices are noisy, so the script also
tries to reduce the noise, and reports every stage separately so the gain is visible:

    raw           the counts as the chip returns them (no suppression)
    suppressed    same circuit with IBM's error suppression switched on: dynamical
                  decoupling (pulses that protect idle qubits) and Pauli twirling (random
                  gate dressing that turns coherent errors into plain noise). Real chips only
    + readout     the suppressed counts corrected for measurement errors, using two
                  calibration circuits (all qubits 0, all qubits 1) run in the same job
    + postselect  the corrected counts restricted to answers with exactly K parcels,
                  because noise that changes the number of parcels is known to be wrong

One-time setup for --ibm (run in your own terminal; keep the key out of the repository):

    python -c "from qiskit_ibm_runtime import QiskitRuntimeService as S; \
S.save_account(channel='ibm_quantum_platform', token='<YOUR API KEY>', \
instance='<YOUR INSTANCE CRN>', overwrite=True)"

Key and instance come from https://quantum.cloud.ibm.com. The free Open Plan gives a few
minutes of QPU time per month. Each job here uses a few seconds; the script prints the exact
usage. --repeats N runs N jobs per method, so cost grows with N.
"""

import argparse
import json

import numpy as np
from qiskit import QuantumCircuit, transpile

from config import OUTPUT_DIR
from qaoa import all_state_costs, build_circuit, run_qaoa
from select_parcels import SEED, load_pool, make_problem, reduced_problem

HW_POOL = 8              # candidates in the hardware problem (= qubits). Keep this small:
HW_K = 3                 # every extra qubit adds many two-qubit gates that add noise
HW_LAYERS = 2            # QAOA depth
SHOTS = 4000
HARDWARE_JSON = OUTPUT_DIR / 'qaoa_hardware_result.json'
TWO_QUBIT_GATES = ('cz', 'ecr', 'cx', 'iswap')


def pick_backend(use_ibm, name, n_qubits):
    """Return (backend, how to run). Real device: the named one, else the least busy with
    enough qubits. Simulation: the noise model of a 156-qubit IBM Heron chip."""
    if use_ibm:
        from qiskit_ibm_runtime import QiskitRuntimeService
        service = QiskitRuntimeService()                     # uses the saved account
        if name:
            return service.backend(name), 'ibm'
        return service.least_busy(operational=True, simulator=False,
                                  min_num_qubits=n_qubits), 'ibm'
    from qiskit_aer import AerSimulator
    from qiskit_ibm_runtime.fake_provider import FakeFez
    return AerSimulator.from_backend(FakeFez()), 'fake'


# ------------------------------------------------------------------------- running
def calibration_circuits(n, compiled, backend):
    """All-0 and all-1 circuits on the physical qubits the real circuit is measured on."""
    physical = compiled.layout.final_index_layout(filter_ancillas=True)   # logical -> chip
    circuits = []
    for ones in (False, True):
        qc = QuantumCircuit(n)
        if ones:
            qc.x(range(n))
        qc.measure_all()
        circuits.append(transpile(qc, backend, initial_layout=physical,
                                  optimization_level=0))
    return circuits


def counts_to_probs(counts, n):
    """Counts dict (bit i of the key from the right = qubit i) to a vector over 2^n states."""
    probs = np.zeros(2 ** n)
    for bits, c in counts.items():
        probs[int(bits.replace(' ', ''), 2)] += c
    return probs / probs.sum()


def run_job(circuits, backend, kind, shots, suppress, seed=SEED):
    """Run the circuits in one job. Returns (list of counts dicts, job id, QPU seconds)."""
    if kind == 'ibm':
        from qiskit_ibm_runtime import SamplerV2
        sampler = SamplerV2(mode=backend)
        sampler.options.dynamical_decoupling.enable = suppress
        sampler.options.twirling.enable_gates = suppress
        sampler.options.twirling.enable_measure = suppress
        job = sampler.run(circuits, shots=shots)
        print(f'  submitted job {job.job_id()} to {backend.name}; waiting for the result...')
        result = job.result()
        seconds = None
        try:
            seconds = job.usage()
        except Exception:                                    # usage is informational only
            pass
        return [r.data.meas.get_counts() for r in result], job.job_id(), seconds
    result = backend.run(circuits, shots=shots, seed_simulator=seed).result()
    return [result.get_counts(i) for i in range(len(circuits))], None, None


# --------------------------------------------------------------------- mitigation
def readout_correct(probs, n, cal0, cal1):
    """Undo independent per-qubit measurement errors.

    cal0 and cal1 are probability vectors from the all-0 and all-1 circuits. For each qubit
    the confusion matrix is built from them and inverted; negative entries that appear
    because of shot noise are clipped and the result renormalised."""
    tensor = probs.reshape((2,) * n)                         # axis n-1-i is qubit i
    for qubit in range(n):
        marginal0 = cal0.reshape((2,) * n).sum(axis=tuple(a for a in range(n)
                                                         if a != n - 1 - qubit))
        marginal1 = cal1.reshape((2,) * n).sum(axis=tuple(a for a in range(n)
                                                         if a != n - 1 - qubit))
        confusion = np.array([[marginal0[0], marginal1[0]],
                              [marginal0[1], marginal1[1]]])  # [measured, true]
        tensor = np.moveaxis(np.tensordot(np.linalg.inv(confusion), tensor,
                                          axes=([1], [n - 1 - qubit])), 0, n - 1 - qubit)
    corrected = np.clip(tensor.reshape(-1), 0, None)
    return corrected / corrected.sum()


def analyse(problem, probs):
    """Quality of a distribution over bitstrings, in the same terms as the simulator."""
    costs, valid = all_state_costs(problem)
    best, worst, mean_random = costs[valid].min(), costs[valid].max(), costs[valid].mean()
    optimal = np.isclose(costs, best) & valid
    p_valid = float(probs[valid].sum())
    out = {'valid_fraction': p_valid, 'valid_fraction_random': float(valid.mean()),
           'p_optimal': float(probs[optimal].sum()),                  # over all shots
           'p_optimal_random': float(optimal.sum() / len(valid)),     # random bitstring
           'p_optimal_random_valid': float(optimal.sum() / valid.sum()),   # random K-set
           'optimal_cost': float(best)}
    if p_valid > 0:
        kept = np.where(valid, probs, 0) / p_valid
        out['p_optimal_given_valid'] = float(kept[optimal].sum())     # among K-sets only
        out['mean_cost'] = float(kept @ costs)
        out['ratio'] = float((worst - out['mean_cost']) / (worst - best))
        out['ratio_random'] = float((worst - mean_random) / (worst - best))
        out['best_cost_seen'] = float(costs[probs > 0][valid[probs > 0]].min()) \
            if (valid & (probs > 0)).any() else None
    return out


def stages(problem, raw_probs, n, cal0, cal1):
    """Report stages for one job: raw counts, readout-corrected, and keeping only valid sets."""
    corrected = readout_correct(raw_probs, n, cal0, cal1)
    valid = all_state_costs(problem)[1]
    kept = np.where(valid, corrected, 0)
    return {'raw': analyse(problem, raw_probs),
            '+ readout': analyse(problem, corrected),
            '+ postselect': analyse(problem, kept / kept.sum()) if kept.sum() > 0 else {}}


def print_table(title, summary):
    print(f'\n{title}')
    header = f"  {'':18}{'valid K-sets':>14}{'P(optimal)':>13}{'P(opt | valid)':>16}{'ratio':>8}"
    print(header)
    for name, r in summary.items():
        print(f"  {name:18}{r['valid_fraction']:>14.1%}{r['p_optimal']:>13.1%}"
              f"{r.get('p_optimal_given_valid', 0):>16.1%}{r.get('ratio', 0):>8.3f}")
    ref = next(iter(summary.values()))
    print(f"  {'random bitstring':18}{ref['valid_fraction_random']:>14.1%}"
          f"{ref['p_optimal_random']:>13.1%}{ref['p_optimal_random_valid']:>16.1%}"
          f"{ref.get('ratio_random', 0):>8.3f}")


# ------------------------------------------------------------------------- main
def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--ibm', action='store_true', help='use a real IBM device')
    parser.add_argument('--backend', help='name of the IBM device, e.g. ibm_fez (with --ibm)')
    parser.add_argument('--qubits', type=int, default=HW_POOL, help='candidates = qubits')
    parser.add_argument('--k', type=int, default=HW_K, help='parcels to pick')
    parser.add_argument('--layers', type=int, default=HW_LAYERS, help='QAOA depth')
    parser.add_argument('--repeats', type=int, default=1, help='jobs per method (error bars)')
    parser.add_argument('--no-compare', action='store_true',
                        help='skip the run without error suppression (saves QPU time)')
    args = parser.parse_args()
    n_qubits, k, layers = args.qubits, args.k, args.layers

    data = load_pool()
    full = make_problem(data, 50)                           # only used to draw the candidates
    small, sub = reduced_problem(data, full, n_qubits, k)
    print(f'Problem: pick {k} of {n_qubits} parcels ({n_qubits} qubits), {layers} layer(s)')

    # 1. tune the angles on a perfect simulator, starting from an arbitrary guess (first K)
    costs, valid = all_state_costs(small)
    print(f'Starting guess (first {k} parcels): cost {costs[2 ** k - 1]:.4f}; optimum '
          f'{costs[valid].min():.4f}; random valid set {costs[valid].mean():.4f}')
    ideal = run_qaoa(small, layers=layers, seed=SEED, shots=SHOTS, start='basis')
    print(f"Perfect simulator: ratio {ideal['ratio']:.3f} (random {ideal['ratio_random']:.3f}), "
          f"P(optimal) {ideal['p_optimal']:.3f} (random {ideal['p_optimal_random']:.3f})")

    # 2. bind the angles, add measurements, compile for the chip
    qc, gammas, betas, _ = build_circuit(small, layers, start='basis')
    qc = qc.assign_parameters(dict(zip(list(gammas) + list(betas), ideal['angles'])))
    qc.measure_all()
    backend, kind = pick_backend(args.ibm, args.backend, n_qubits)
    compiled = transpile(qc, backend, optimization_level=3, seed_transpiler=SEED)
    two_qubit = sum(n for name, n in compiled.count_ops().items() if name in TWO_QUBIT_GATES)
    print(f'Backend: {backend.name} ({"real device" if kind == "ibm" else "noisy simulation"}); '
          f'{two_qubit} two-qubit gates, depth {compiled.depth()}')
    calibration = calibration_circuits(n_qubits, compiled, backend)

    # 3. run each method; --repeats jobs each. On a simulator suppression has no effect.
    methods = ['suppressed'] if args.no_compare else ['raw', 'suppressed']
    if kind != 'ibm':
        methods = ['raw']
        print('(Error suppression only exists on real devices; the simulator runs raw.)')
    runs = {m: [] for m in methods}
    total_seconds = 0.0
    for method in methods:
        for rep in range(args.repeats):
            print(f'Run: {method}, repeat {rep + 1} of {args.repeats}')
            counts, job_id, seconds = run_job([compiled] + calibration, backend, kind, SHOTS,
                                              suppress=(method == 'suppressed'),
                                              seed=SEED + rep)
            total_seconds += seconds or 0
            probs = [counts_to_probs(c, n_qubits) for c in counts]
            runs[method].append({'job_id': job_id, 'qpu_seconds': seconds,
                                 'stages': stages(small, probs[0], n_qubits, probs[1], probs[2])})

    # 4. report: mean over repeats, with the spread when there is more than one
    summary = {}
    for method, items in runs.items():
        for stage in items[0]['stages']:
            rows = [it['stages'][stage] for it in items if it['stages'][stage]]
            if not rows:
                continue
            mean = {key: float(np.mean([r[key] for r in rows if r.get(key) is not None]))
                    for key in rows[0] if isinstance(rows[0][key], float)}
            if len(rows) > 1:
                mean['p_optimal_std'] = float(np.std([r['p_optimal'] for r in rows]))
            summary[method if stage == 'raw' else f'{method} {stage}'] = mean
    print_table(f'Results ({args.repeats} job(s) per method, {SHOTS} shots each)', summary)
    if total_seconds:
        print(f'\nQPU time used: {total_seconds:.0f} s')

    with open(HARDWARE_JSON, 'w') as fh:
        json.dump({'backend': backend.name, 'real_device': kind == 'ibm',
                   'n_qubits': n_qubits, 'k': k, 'layers': layers, 'shots': SHOTS,
                   'two_qubit_gates': int(two_qubit), 'depth': int(compiled.depth()),
                   'ideal': {key: ideal[key] for key in ('ratio', 'ratio_random', 'p_optimal',
                                                          'p_optimal_random')},
                   'summary': summary, 'runs': runs}, fh, indent=2)
    print(f'Saved {HARDWARE_JSON}')


if __name__ == '__main__':
    main()
