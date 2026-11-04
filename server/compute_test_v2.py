"""Train-only computational test for the E0004 Harness (compute-test v2).

Same protocol as compute_test.compute_method_test (pair-disjoint holdout
inside train_items.csv, 80/20 by the same sha256 pair order, seed 42, 5%
budget, hit = observed_score > 0, parametric bootstrap CI), with one
addition: it also runs drug_cell_mean and structure_cell_knn, ranking through
bridgelab.decision_compiler_v2 so the test and the sealed ranking share one
implementation. The v1 methods are delegated to compute_test unchanged, so
their numbers match the E0003-era live runs exactly.

Reads only comparison-prereg-v4/train_items.csv. Never opens a hidden label.
"""
import csv
import hashlib
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bridgelab.decision_compiler_v2 import NEW_METHODS, score_candidates_v2  # noqa: E402
from compute_test import (BUDGET_FRACTION, FIT_FRACTION, N_BOOTSTRAP, SEED,  # noqa: E402
                          _pair_key, compute_method_test)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRAIN_ITEMS = os.path.join(ROOT, "comparison-prereg-v4", "train_items.csv")


def compute_method_test_v2(train_items_path, method, seed=SEED):
    if method not in NEW_METHODS:
        return compute_method_test(train_items_path, method)
    rows = []
    with open(train_items_path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append({"item_id": r["item_id"], "nsc1": r["nsc1"], "nsc2": r["nsc2"],
                         "cellname": r["cellname"], "observed_score": float(r["observed_score"])})
    key = lambda r: _pair_key(int(r["nsc1"]), int(r["nsc2"]))  # noqa: E731
    pairs = sorted({key(r) for r in rows})
    ordered = sorted(pairs, key=lambda p: hashlib.sha256(("%d:%d:%d" % (seed, p[0], p[1])).encode()).hexdigest())
    fit_pairs = set(ordered[:int(len(ordered) * FIT_FRACTION)])
    fit = [r for r in rows if key(r) in fit_pairs]
    ev = [r for r in rows if key(r) not in fit_pairs]
    candidates = [{k: r[k] for k in ("item_id", "nsc1", "nsc2", "cellname")} for r in ev]
    scored = sorted(score_candidates_v2(method, {}, fit, candidates), key=lambda item: (-item[1], item[0]))
    ranked_ids = [item_id for item_id, _ in scored]
    score_of = dict(scored)
    by_id = {r["item_id"]: r for r in ev}
    budget = max(1, int(len(ev) * BUDGET_FRACTION))
    selected = [by_id[i] for i in ranked_ids[:budget]]
    hits = [1.0 if r["observed_score"] > 0 else 0.0 for r in ev]
    precision_method = sum(1.0 for r in selected if r["observed_score"] > 0) / budget
    precision_random = sum(hits) / len(ev)
    preview, seen = [], set()
    for r in selected:
        pair = key(r)
        if pair in seen:
            continue
        seen.add(pair)
        preview.append({"item_id": r["item_id"], "nsc1": r["nsc1"], "nsc2": r["nsc2"],
                        "cellname": r["cellname"], "predicted_score": round(score_of[r["item_id"]], 6)})
        if len(preview) >= 20:
            break
    rng = np.random.default_rng(seed)
    diffs = np.sort(rng.binomial(budget, precision_method, N_BOOTSTRAP) / budget
                    - rng.binomial(len(ev), precision_random, N_BOOTSTRAP) / len(ev))
    ci_low, ci_high = float(diffs[int(0.025 * N_BOOTSTRAP)]), float(diffs[int(0.975 * N_BOOTSTRAP) - 1])
    return {
        "method": method,
        "protocol": "pair-disjoint holdout inside train data (fit 80% of pairs, evaluate 20%)",
        "seed": seed, "fit_pairs": len(fit_pairs), "eval_pairs": len(ordered) - len(fit_pairs),
        "fit_items": len(fit), "eval_items": len(ev), "budget_fraction": BUDGET_FRACTION,
        "budget_count": budget, "selected_item_count": len(selected),
        "selected_pair_count_in_preview": len(preview), "selected_candidates_preview": preview,
        "precision_method": round(precision_method, 4),
        "precision_random_baseline": round(precision_random, 4),
        "difference": round(precision_method - precision_random, 4),
        "difference_ci95": [round(ci_low, 4), round(ci_high, 4)],
        "bootstrap": "parametric binomial, %d draws, seed %d" % (N_BOOTSTRAP, seed),
        "mean_score_selected": round(sum(r["observed_score"] for r in selected) / budget, 4),
        "mean_score_eval_pool": round(sum(r["observed_score"] for r in ev) / len(ev), 4),
        "hit_rule": "observed_score > 0",
        "data": "train_items.csv only; hidden labels not read",
    }


def execution_callback_v2(judge, sealed_plan, ledger, train_items_path=TRAIN_ITEMS):
    """Execution callback for harness_v2.run_pipeline: same contract as the live
    server's _live_execution_callback, with drug_cell_mean supported."""
    method = judge.get("selected_method")
    try:
        result = compute_method_test_v2(train_items_path, method)
    except Exception:  # noqa: BLE001
        return "failed", [], [], "execution_failed", method
    entry = ledger.append("tool_call", "executed", {
        "tool": "python.compute_method_test",
        "arguments": {"method": method, "dataset": "train_items.csv"},
        "result": {"status": "ok", "output": result},
        "provenance": "python-executed",
    })
    return "ok", [entry], ["python.compute_method_test"], None, None


if __name__ == "__main__":
    import json
    print(json.dumps(compute_method_test_v2(TRAIN_ITEMS, "structure_cell_knn"), indent=1)[:1200])
