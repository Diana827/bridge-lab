"""Rehearse the E0004 comparison with SYNTHETIC labels and scripted model replies.

This never reads a hidden label and never calls a model. It uses the public
packet inputs (train rows, hidden candidate ids) and a seeded synthetic label
function, so the driver, the label gate and the study-level rule can be checked
before anything real is run. Synthetic output is not a result.

Usage: python3 scripts/rehearse_e0004.py
"""
import csv
import json
import os
import random
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bridgelab.comparison_driver_v2 import run_comparison_v2  # noqa: E402
from bridgelab.decision_compiler_v2 import score_candidates_v2  # noqa: E402
from bridgelab.ledger import Ledger  # noqa: E402
from bridgelab.muse_runtime import EXPECTED_MODEL  # noqa: E402
from bridgelab.orchestration import load_split_ids_file  # noqa: E402

V4 = os.path.join(ROOT, "comparison-prereg-v4")
BUDGETS = (3125, 6251, 12502)
INDICES = (0, 1, 2)


class ScriptedClient:
    """Scripted replies. NOT model output."""
    model = EXPECTED_MODEL

    def __init__(self, replies):
        self.replies = list(replies)

    def chat(self, messages, tools=None):
        text = self.replies.pop(0)
        return {"model": EXPECTED_MODEL, "choices": [{"message": {"role": "assistant", "content": text}}], "usage": None}


def load_inputs():
    """A fully synthetic universe: real NSC ids (public, so the structure method can use fingerprints),
    synthetic scores. It does not use the packet's train or candidate files."""
    with open(os.path.join(V4, "inputs", "nsc_smiles.csv"), newline="") as f:
        drugs = sorted(r["nsc"] for r in csv.DictReader(f))[:60]
    cells = ["C%02d" % i for i in range(60)]
    rnd = random.Random(5)
    drug_eff = {d: rnd.gauss(0, 4) for d in drugs}
    cell_eff = {c: rnd.gauss(0, 2) for c in cells}
    drug_cell = {(d, c): rnd.gauss(0, 3) for d in drugs for c in cells}
    pairs = [(a, b) for i, a in enumerate(drugs) for b in drugs[i + 1:]]
    pairs.sort(key=lambda p: sha("%d:%s:%s" % (5, p[0], p[1])))
    cut = int(len(pairs) * 0.8)
    train_pairs, hidden_pairs = pairs[:cut], pairs[cut:]
    pid = lambda p: ":".join(sorted(p))
    split_ids = {"train_ids": [pid(p) for p in train_pairs], "hidden_ids": [pid(p) for p in hidden_pairs],
                 "start_set_ids": [], "split_seed": 5, "split_method": "sha256_hash_order"}

    def score(a, b, c, noise):
        return drug_eff[a] + drug_eff[b] + cell_eff[c] + drug_cell[(a, c)] + drug_cell[(b, c)] + rnd.gauss(0, noise)

    train, cand, labels = [], [], {}
    for a, b in train_pairs:
        for c in cells:
            train.append({"item_id": "%s:%s:%s" % (a, b, c), "nsc1": a, "nsc2": b, "cellname": c,
                          "observed_score": score(a, b, c, 6.0)})
    for a, b in hidden_pairs:
        for c in cells:
            item = "H:%s:%s:%s" % (a, b, c)
            cand.append({"item_id": item, "nsc1": a, "nsc2": b, "cellname": c})
            labels[item] = score(a, b, c, 6.0)
    return split_ids, train, cand, labels


def sha(text):
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()


def judge(method):
    return {"selected_domain": "mathematics", "selected_principle": "rehearsal", "selected_method": method,
            "method_parameters": {}, "prediction": "rehearsal", "falsifier": "rehearsal",
            "required_data_fields": ["item_id"]}


def scenario(name, split_ids, train, cand, labels, harness_methods, baseline_replies):
    ledger = Ledger(os.path.join(tempfile.mkdtemp(), "rehearsal.jsonl"))
    loads = {"n": 0}

    def loader():
        loads["n"] += 1
        return labels

    t0 = time.time()
    report = run_comparison_v2(
        ScriptedClient(baseline_replies), ledger, {i: judge(m) for i, m in zip(INDICES, harness_methods)},
        train, cand, BUDGETS, loader, run_indices=INDICES, n_bootstrap=300, split_ids=split_ids)
    study = report["study"]
    print("%-34s outcome=%-32s passing=%s ties=%s loader_calls=%d  (%.0fs)" % (
        name, study["outcome"], study["indices_passing_gate"], study["tie_indices"], loads["n"], time.time() - t0))
    return report, study, loads["n"]


def reply(method):
    return json.dumps({"selected_method": method, "method_parameters": {}})


def main():
    print("SYNTHETIC LABELS, SCRIPTED REPLIES. Nothing below is a result.\n")
    split_ids, train, cand, labels = load_inputs()
    print("train rows %d, hidden candidates %d" % (len(train), len(cand)))
    n = len(BUDGETS) * len(INDICES)
    ok = True

    rep, study, calls = scenario("A harness structure, baseline drug_mean", split_ids, train, cand, labels,
                                 ["structure_cell_knn"] * 3, [reply("drug_mean")] * n)
    ok &= calls == 1
    print("   precisions (index 0):", {b: (rep["scores"][b][0]["baseline"]["precision"], rep["scores"][b][0]["harness"]["precision"]) for b in BUDGETS})

    rep, study, calls = scenario("B same method (tie)", split_ids, train, cand, labels,
                                 ["drug_cell_mean"] * 3, [reply("drug_cell_mean")] * n)
    ok &= calls == 1 and study["outcome"] == "no_difference" and not study["claim_superiority"]

    rep, study, calls = scenario("C baseline invalid JSON (failed arm)", split_ids, train, cand, labels,
                                 ["structure_cell_knn"] * 3, ["not json"] * (3 * n))
    ok &= calls == 1 and rep["gate"]["baseline_status"] == "failed" and not study["claim_superiority"]
    ok &= all(rep["scores"][b][i]["baseline"]["precision"] == 0.0 for b in BUDGETS for i in INDICES)

    rep, study, calls = scenario("D baseline better than harness", split_ids, train, cand, labels,
                                 ["pair_frequency"] * 3, [reply("structure_cell_knn")] * n)
    ok &= calls == 1 and not study["claim_superiority"] and study["outcome"] == "baseline_favored_at_some_index"

    print("\nrehearsal checks:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
