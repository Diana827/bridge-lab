# E0004 preregistration (DRAFT, not sealed, nothing has been run)

Drafted 2026-11-03. E0003 and the v1 to v4 packets are unchanged.

## Why a new seal
In E0003 the single-call baseline had to write thousands of item ids and failed in every run, so the paired comparison could not be computed and no superiority claim was made. E0004 replaces that baseline. It does not reopen E0003.

## Question
When both arms rank candidates with the same Python code, does the BRIDGE LAB pipeline choose a better ranking method than one model call?

## Arms
- **Harness.** The full pipeline picks one method from the allowlist. Python ranks every hidden candidate and takes the top budget.
- **Baseline (criterion).** One model call sees the question and the method descriptions, and returns `{"selected_method", "method_parameters"}`. It never sees ids, data, or labels. Python ranks with the same code as the Harness.
- **Reference only.** Each Python method and random selection, reported with no claim.

Allowlist: `exact_pair_mean`, `drug_mean`, `pair_frequency`, `drug_cell_mean` (new; uses the cell line) and `structure_cell_knn` (new; adds molecular-fingerprint similarity to `drug_cell_mean`). Definitions are in `bridgelab/decision_compiler_v2.py`.

## Data and split
The E0003 split is reused unchanged: 4,193 train pairs and 1,049 hidden pairs (`comparison-prereg-v4/split_ids.json`), 62,510 hidden candidates. No new split is drawn. Hidden labels are opened once, after the seal.

## Budgets, runs, metric
Budgets 5% (3,125), 10% (6,251, primary) and 20% (12,502), the same frozen counts as E0003. Three run indices. A hit is `observed_score > 0`. Primary metric: precision at the primary budget, intention-to-treat (a failed run scores 0).

## Claim gate
Per index, the E0003 gate is unchanged (`comparison.evaluate_claim_gate`). Study level: superiority is claimed only if the gate passes at 2 or more of 3 indices and no index has a primary CI favoring the baseline. If both arms choose the same method, the selections are identical, the difference is 0, and that index is a tie. A study of all ties is reported as "no difference", which is a valid outcome.

## Known limits (stated before running)
1. **Likely ties.** The arms differ only in how the method is chosen. If one call and five lenses pick the same method, E0004 shows no difference.
2. **The hidden pairs are reused.** They were scored once for E0003, so this is their second evaluation. The method list was extended after train-only analyses (pair-disjoint holdout, 3 seeds, precision at 5%, 10%, 20%): `drug_mean` 0.57, 0.54, 0.49; `drug_cell_mean` 0.66, 0.61, 0.54; `structure_cell_knn` 0.72, 0.66, 0.57. Its constants (30 neighbours, power 2, weight 0.5) were fixed before the analysis finished, and the one other weight tried (1.0) was slightly worse. Those analyses used v4 train labels only. No per-item hidden label has been seen by anyone. What is known about the hidden set is the E0003 Harness precision (0.6070, 0.5769, 0.5034) and that its labels were loaded once.
3. **Same dataset.** E0004 is a design replication on one dataset, not an independent one.
4. **Harness built offline only.** `bridgelab/harness_v2.py` derives the v2 pipeline from `orchestration.py` by three asserted replacements (the method list shown to the Judge, the Judge prompt file, and the Judge instruction line that told it not to require molecular structures, which had blocked structure_cell_knn in live runs) and widens the Judge validator for one call. It was tested by replaying a real recorded run's model outputs. It has never run against a live model. Its pipeline settings mirror the live caller, and the E0003 comparison caller is not in this package, so they must be confirmed against it before sealing.

## Not done yet
Confirm the pipeline settings against the E0003 comparison caller; compute the packet hash with `scripts/prereg_packet_hash.py`; commit it to the ledger with `bridgelab/seal.py`; run both arms; open the labels once. The comparison driver (`bridgelab/comparison_driver_v2.py`) is written and has been rehearsed on synthetic data only.
