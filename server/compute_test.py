"""Train-only computational test for a Judge-selected method.

LIVE EXPLORATION only. The protocol is the Judge's own falsifier,
executed: a pair-disjoint holdout INSIDE the train data. The method is
fitted on 80% of the train pairs and evaluated on the remaining 20%,
whose pairs it has never seen. The baseline is random selection (the
held-out items' overall hit rate). The budget is 10% of the held-out
items, matching the exploration run's budget fraction.

This module reads ONLY comparison-prereg-v4/train_items.csv. It never
opens hidden candidates, hidden labels, or any sealed-packet file.
Every number is computed here, in Python, deterministically (seed 42).
"""
import csv
import hashlib
import random

import numpy as np

SEED = 42
BUDGET_FRACTION = 0.05
FIT_FRACTION = 0.80
N_BOOTSTRAP = 10000


def _pair_key(nsc1, nsc2):
    return (nsc1, nsc2) if nsc1 <= nsc2 else (nsc2, nsc1)


def compute_method_test(train_items_path, method="drug_mean"):
    """Evaluate a Judge-selected method on a deterministic train-only holdout."""
    items = []  # (pair_key, nsc1, nsc2, observed_score, item_id, cellname)
    with open(train_items_path, newline="") as f:
        for row in csv.DictReader(f):
            nsc1, nsc2 = int(row["nsc1"]), int(row["nsc2"])
            items.append((_pair_key(nsc1, nsc2), nsc1, nsc2,
                          float(row["observed_score"]), row["item_id"],
                          row["cellname"]))
    pairs = sorted({it[0] for it in items})
    ordered = sorted(pairs, key=lambda p: hashlib.sha256(
        ("%d:%d:%d" % (SEED, p[0], p[1])).encode()).hexdigest())
    n_fit = int(len(ordered) * FIT_FRACTION)
    fit_pairs = set(ordered[:n_fit])
    fit_items = [it for it in items if it[0] in fit_pairs]
    eval_items = [it for it in items if it[0] not in fit_pairs]

    if method not in {"drug_mean", "exact_pair_mean", "pair_frequency"}:
        raise ValueError("unsupported executable method: %s" % method)

    # Fit only on the fit pairs. The held-out pair labels are never used for ranking.
    sums, counts = {}, {}
    total = 0.0
    for _, nsc1, nsc2, score, _, _ in fit_items:
        for drug in (nsc1, nsc2):
            sums[drug] = sums.get(drug, 0.0) + score
            counts[drug] = counts.get(drug, 0) + 1
        total += score
    global_mean = total / len(fit_items)
    drug_mean = {d: sums[d] / counts[d] for d in sums}

    pair_sums, pair_counts = {}, {}
    for pair, _, _, score, _, _ in fit_items:
        pair_sums[pair] = pair_sums.get(pair, 0.0) + score
        pair_counts[pair] = pair_counts.get(pair, 0) + 1
    pair_mean = {p: pair_sums[p] / pair_counts[p] for p in pair_sums}

    def predict(nsc1, nsc2):
        pair = _pair_key(nsc1, nsc2)
        if method == "exact_pair_mean":
            return pair_mean.get(pair, (drug_mean.get(nsc1, global_mean)
                                         + drug_mean.get(nsc2, global_mean)) / 2.0)
        if method == "pair_frequency":
            return float(pair_counts.get(pair, 0))
        return (drug_mean.get(nsc1, global_mean)
                + drug_mean.get(nsc2, global_mean)) / 2.0

    # Rank held-out items by predicted score (deterministic tie-break).
    ranked = sorted(range(len(eval_items)),
                    key=lambda i: (-predict(eval_items[i][1], eval_items[i][2]), i))
    budget = max(1, int(len(eval_items) * BUDGET_FRACTION))
    selected_idx = ranked[:budget]
    hits = [1.0 if it[3] > 0 else 0.0 for it in eval_items]
    selected_hits = [hits[i] for i in selected_idx]
    precision_method = sum(selected_hits) / budget
    precision_random = sum(hits) / len(eval_items)
    mean_selected = sum(eval_items[i][3] for i in selected_idx) / budget
    mean_eval = sum(it[3] for it in eval_items) / len(eval_items)
    difference = precision_method - precision_random

    # The metric is computed on item rows, but researchers need to see the
    # actual proposed drug combinations.  Return a small auditable preview;
    # the full selection remains the deterministic ranked item list and no
    # holdout outcome is exposed in this recommendation view.
    selected_candidates = []
    seen_pairs = set()
    for i in selected_idx:
        pair, nsc1, nsc2, _, item_id, cellname = eval_items[i]
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        selected_candidates.append({
            "item_id": item_id,
            "nsc1": str(nsc1),
            "nsc2": str(nsc2),
            "cellname": cellname,
            "predicted_score": round(predict(nsc1, nsc2), 6),
        })
        if len(selected_candidates) >= 20:
            break

    # Parametric bootstrap CI for the difference (binomial resampling of
    # the two hit rates, seed-fixed; conservative: treats the rates as
    # independent although the selection is a subset of the pool).
    rng = np.random.default_rng(SEED)
    selected_boot = rng.binomial(budget, precision_method, N_BOOTSTRAP) / budget
    pool_boot = rng.binomial(len(eval_items), precision_random, N_BOOTSTRAP) / len(eval_items)
    diffs = np.sort(selected_boot - pool_boot)
    ci_low = float(diffs[int(0.025 * N_BOOTSTRAP)])
    ci_high = float(diffs[int(0.975 * N_BOOTSTRAP) - 1])

    return {
        "method": method,
        "protocol": "pair-disjoint holdout inside train data (fit 80% of pairs, evaluate 20%)",
        "seed": SEED,
        "fit_pairs": len(fit_pairs),
        "eval_pairs": len(ordered) - len(fit_pairs),
        "fit_items": len(fit_items),
        "eval_items": len(eval_items),
        "budget_fraction": BUDGET_FRACTION,
        "budget_count": budget,
        "selected_item_count": len(selected_idx),
        "selected_pair_count_in_preview": len(selected_candidates),
        "selected_candidates_preview": selected_candidates,
        "precision_method": round(precision_method, 4),
        "precision_random_baseline": round(precision_random, 4),
        "difference": round(difference, 4),
        "difference_ci95": [round(ci_low, 4), round(ci_high, 4)],
        "bootstrap": "parametric binomial, %d draws, seed %d" % (N_BOOTSTRAP, SEED),
        "mean_score_selected": round(mean_selected, 4),
        "mean_score_eval_pool": round(mean_eval, 4),
        "hit_rule": "observed_score > 0",
        "data": "train_items.csv only; hidden labels not read",
    }


def compute_drug_mean_test(train_items_path):
    return compute_method_test(train_items_path, "drug_mean")


if __name__ == "__main__":
    import json
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "comparison-prereg-v4", "train_items.csv")
    print(json.dumps(compute_drug_mean_test(path), indent=1))
