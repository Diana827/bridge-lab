"""Offline tests for the E0004 design code. No model call, no dataset file, no hidden label.
Run: python3 server/test_e0004.py
"""
import json
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridgelab.criterion_baseline import PROMPT_TEMPLATE, run_criterion_baseline
from bridgelab.decision_compiler_v2 import (METHODS_V2, compile_selection_v2,
                                            rank_candidates_v2, validate_method_choice)
from bridgelab.ledger import Ledger
from bridgelab.muse_runtime import EXPECTED_MODEL


class StubClient:
    """Scripted replies. NOT model output."""
    model = EXPECTED_MODEL

    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0

    def chat(self, messages, tools=None):
        self.calls += 1
        text = self.replies.pop(0)
        return {"model": EXPECTED_MODEL, "choices": [{"message": {"role": "assistant", "content": text}}],
                "usage": None}


def make_rows(seed=1):
    """Synthetic: drug 'a' is strong only in cell X, drug 'b' only in cell Y."""
    rnd = random.Random(seed)
    train, cand = [], []
    drugs = ["a", "b", "c", "d"]
    for i, d1 in enumerate(drugs):
        for d2 in drugs[i + 1:]:
            for cell in ("X", "Y"):
                strong = (("a" in (d1, d2) and cell == "X") or ("b" in (d1, d2) and cell == "Y"))
                train.append({"item_id": "%s:%s:%s" % (d1, d2, cell), "nsc1": d1, "nsc2": d2,
                              "cellname": cell, "observed_score": (5.0 if strong else -5.0) + rnd.random()})
    for n in range(10):
        for cell in ("X", "Y"):
            cand.append({"item_id": "n%d:a:%s" % (n, cell), "nsc1": "a", "nsc2": "n%d" % n, "cellname": cell})
            cand.append({"item_id": "n%d:b:%s" % (n, cell), "nsc1": "b", "nsc2": "n%d" % n, "cellname": cell})
    return train, cand


def test_drug_cell_mean_uses_cell_line():
    train, cand = make_rows()
    ranked = rank_candidates_v2("drug_cell_mean", {}, train, cand)
    top = ranked[:20]
    assert all(i.endswith(":X") if ":a:" in i else i.endswith(":Y") for i in top), top
    plain = rank_candidates_v2("drug_mean", {}, train, cand)
    assert ranked != plain


def test_v1_methods_unchanged():
    from bridgelab import decision_compiler as v1
    train, cand = make_rows()
    for m, p in (("exact_pair_mean", {"min_pair_items": 1}), ("drug_mean", {}), ("pair_frequency", {})):
        assert rank_candidates_v2(m, p, train, cand) == v1._rank_candidates(m, v1._normalized_params(m, p), train, cand)


def test_contract_errors():
    assert validate_method_choice("drug_cell_mean", {}) == []
    assert validate_method_choice("drug_cell_mean", {"k": 1})
    assert validate_method_choice("magic", {})
    assert validate_method_choice("drug_mean", {"x": 1})
    assert set(METHODS_V2) == {"exact_pair_mean", "drug_mean", "pair_frequency", "drug_cell_mean", "structure_cell_knn"}
    assert validate_method_choice("structure_cell_knn", {}) == []
    assert validate_method_choice("structure_cell_knn", {"k": 3})


def test_candidate_outcome_columns_rejected():
    train, cand = make_rows()
    cand[0]["observed_score"] = 1.0
    try:
        compile_selection_v2("drug_mean", {}, train, cand, 5)
    except ValueError:
        return
    raise AssertionError("candidate rows with an outcome column must be rejected")


def _ledger():
    return Ledger(os.path.join(tempfile.mkdtemp(), "ledger.jsonl"))


def test_baseline_ok_and_deterministic():
    train, cand = make_rows()
    reply = json.dumps({"selected_method": "drug_cell_mean", "method_parameters": {}})
    a = run_criterion_baseline(StubClient([reply]), _ledger(), train, cand, 8)
    b = run_criterion_baseline(StubClient([reply]), _ledger(), train, cand, 8)
    assert a["status"] == "ok" and a["ids"] == b["ids"] and len(a["ids"]) == 8
    assert a["selected_method"] == "drug_cell_mean" and a["attempts"] == 1


def test_baseline_ignores_extra_claims():
    train, cand = make_rows()
    reply = json.dumps({"selected_method": "drug_mean", "method_parameters": {},
                        "selected_item_ids": ["bogus"], "precision": 0.99})
    rec = run_criterion_baseline(StubClient([reply]), _ledger(), train, cand, 4)
    assert rec["status"] == "ok" and "bogus" not in rec["ids"]


def test_baseline_retries_then_fails():
    train, cand = make_rows()
    client = StubClient(["not json", "still not json", "nope"])
    rec = run_criterion_baseline(client, _ledger(), train, cand, 4)
    assert rec["status"] == "failed" and rec["error"] == "invalid_json_after_retries" and client.calls == 3
    assert rec["ids"] == []


def test_baseline_retry_succeeds():
    train, cand = make_rows()
    ok = json.dumps({"selected_method": "drug_mean", "method_parameters": {}})
    rec = run_criterion_baseline(StubClient(["oops", ok]), _ledger(), train, cand, 4)
    assert rec["status"] == "ok" and rec["attempts"] == 2


def test_baseline_unknown_method_and_bad_params_fail():
    train, cand = make_rows()
    rec = run_criterion_baseline(StubClient([json.dumps({"selected_method": "oracle", "method_parameters": {}})]),
                                 _ledger(), train, cand, 4)
    assert rec["status"] == "failed" and rec["error"] == "unknown_method"
    rec = run_criterion_baseline(StubClient([json.dumps({"selected_method": "drug_mean", "method_parameters": {"k": 2}})]),
                                 _ledger(), train, cand, 4)
    assert rec["status"] == "failed"


def test_prompt_has_no_data_and_keeps_json_braces():
    prompt = PROMPT_TEMPLATE.replace("{budget_count}", "6251")
    assert "6251" in prompt and '"selected_method"' in prompt and "{budget_count}" not in prompt
    assert "observed_score" not in prompt and "item_id" not in prompt


def test_structure_cell_knn_borrows_from_similar_drugs():
    import numpy as np
    from bridgelab.decision_compiler_v2 import score_candidates_v2
    train, cand = [], []
    # x and y are structurally similar. Pair (x,p) scores high in cell X; (x,q) low. Candidates pair y with p and q.
    for partner, score in (("p", 10.0), ("q", -10.0), ("r", 0.0)):
        for cell in ("X", "Y"):
            train.append({"item_id": "x:%s:%s" % (partner, cell), "nsc1": "x", "nsc2": partner, "cellname": cell,
                          "observed_score": score if cell == "X" else 0.0})
            train.append({"item_id": "z:%s:%s" % (partner, cell), "nsc1": "z", "nsc2": partner, "cellname": cell,
                          "observed_score": 0.0})
    for partner in ("p", "q"):
        cand.append({"item_id": "y:%s:X" % partner, "nsc1": "y", "nsc2": partner, "cellname": "X"})

    def sim(drugs):
        m = np.eye(len(drugs))
        i, j = drugs.index("x"), drugs.index("y")
        m[i, j] = m[j, i] = 1.0
        return m

    scores = dict(score_candidates_v2("structure_cell_knn", {}, train, cand, similarity=sim))
    assert scores["y:p:X"] > scores["y:q:X"], scores
    plain = dict(score_candidates_v2("drug_cell_mean", {}, train, cand))
    assert plain["y:p:X"] != scores["y:p:X"]
    again = dict(score_candidates_v2("structure_cell_knn", {}, train, cand, similarity=sim))
    assert again == scores


def test_real_fingerprints_load():
    from bridgelab.decision_compiler_v2 import load_fingerprints
    fps = load_fingerprints()
    assert len(fps) >= 100, len(fps)


def _judge(method):
    return {"selected_domain": "mathematics", "selected_principle": "t", "selected_method": method,
            "method_parameters": {}, "prediction": "t", "falsifier": "t", "required_data_fields": ["item_id"]}


def _run_driver(baseline_replies, harness_methods):
    from bridgelab.comparison_driver_v2 import run_comparison_v2
    train, cand = make_rows()
    rnd = random.Random(3)
    labels = {r["item_id"]: rnd.gauss(0, 5) for r in cand}
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return labels

    report = run_comparison_v2(StubClient(baseline_replies), _ledger(),
                               {i: _judge(m) for i, m in enumerate(harness_methods)},
                               train, cand, (3125, 6251, 12502), loader, n_bootstrap=100)
    return report, calls["n"]


def test_driver_v2_opens_labels_once_and_reports_a_tie():
    reply = json.dumps({"selected_method": "drug_cell_mean", "method_parameters": {}})
    report, loads = _run_driver([reply] * 9, ["drug_cell_mean"] * 3)
    assert loads == 1 and report["gate"]["label_loader_calls"] == 1
    assert report["study"]["outcome"] == "no_difference" and not report["study"]["claim_superiority"]
    assert report["study"]["tie_indices"] == [0, 1, 2]
    assert report["baseline"][3125][0]["selected_method"] == "drug_cell_mean"
    assert "ids" not in report["baseline"][3125][0] and "selected_item_ids" not in report["harness"][3125][0]


def test_driver_v2_failed_baseline_scores_zero_and_makes_no_claim():
    report, loads = _run_driver(["not json"] * 27, ["drug_cell_mean"] * 3)
    assert loads == 1 and report["gate"]["baseline_status"] == "failed"
    assert all(report["scores"][b][i]["baseline"]["precision"] == 0.0 for b in (3125, 6251, 12502) for i in range(3))
    assert not report["study"]["claim_superiority"]


def test_driver_v2_rejects_labels_before_both_arms_finish():
    from bridgelab.comparison import HiddenLabelGate
    gate = HiddenLabelGate()
    gate.mark_baseline_done("ok")
    try:
        gate.open_labels(lambda: {})
    except ValueError:
        return
    raise AssertionError("labels must stay closed until both arms are done")


def test_study_level_rule():
    from bridgelab.comparison_driver_v2 import study_level
    budgets = [3125, 6251, 12502]

    def report(passing, baseline_ci_high, same):
        claims = {i: {"applicable": True, "claim_superiority": i in passing} for i in range(3)}
        boot = {6251: {i: {"ci_high": baseline_ci_high.get(i, 0.1)} for i in range(3)}}
        arm = lambda tag: {b: {i: {"ids_sha256": "x" if same else tag, "status": "ok"} for i in range(3)} for b in budgets}
        return {"claims": claims, "bootstrap": boot, "baseline": arm("b"), "harness": arm("h"), "budgets": budgets}

    assert study_level(report([0, 1], {}, False), range(3))["outcome"] == "superiority"
    assert study_level(report([0], {}, False), range(3))["outcome"] == "no_superiority_claim"
    assert study_level(report([0, 1], {2: -0.05}, False), range(3))["claim_superiority"] is False
    assert study_level(report([], {}, True), range(3))["outcome"] == "no_difference"


# ---------------------------------------------------------------- harness v2 (integration)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "server", "fixtures", "recorded_run_3e649c3f36ff.json")


class ReplayClient:
    """Replays the model texts of a real recorded run, in call order. NOT new model output."""
    model = EXPECTED_MODEL

    def __init__(self, calls):
        self.calls, self.n = calls, 0

    def chat(self, messages, tools=None):
        item = self.calls[self.n]
        self.n += 1
        return {"model": EXPECTED_MODEL, "choices": [{"message": {"role": "assistant", "content": item["text"]}}],
                "usage": None}


def _run_harness(judge_edit=None):
    sys.path.insert(0, os.path.join(ROOT, "server"))
    from bridgelab import harness_v2 as h
    from bridgelab.orchestration import load_split_ids_file
    from bridgelab.structure_resolver import build_manifest
    import compute_test_v2

    fx = json.load(open(FIXTURE))
    calls = [dict(c) for c in fx["calls"]]
    if judge_edit:
        for c in calls:
            if c["role"] == "transfer_judge":
                data = json.loads(c["text"].strip().strip("`").removeprefix("json"))
                data.update(judge_edit)
                c["text"] = json.dumps(data)
    v4 = os.path.join(ROOT, "comparison-prereg-v4")
    manifest = json.load(open(os.path.join(v4, "dataset_manifest.json")))
    manifest, _ = build_manifest(manifest, os.path.join(v4, "inputs", "nsc_structure.csv"),
                                 os.path.join(v4, "inputs", "candidate_items.csv"))
    split_ids = load_split_ids_file(os.path.join(v4, "split_ids.json"))
    ledger = _ledger()
    result = h.run_pipeline_v2(
        ReplayClient(calls), ledger, fx["question"], h.load_role_prompts(os.path.join(ROOT, "prompts")),
        manifest, split_ids, metric="precision_at_5pct_budget", baseline="single_muse_call",
        confidence=0.95, minimum_effect=0.0, budget_fraction=0.05, total_items=293626,
        execution_calls=[], max_total_model_steps=100,
        review_callback=lambda payload: {"action": "continue", "direction": ""},
        execution_callback=compute_test_v2.execution_callback_v2)
    return h, result, ledger


def test_harness_v2_derivation_and_prompt():
    from bridgelab import harness_v2 as h, orchestration as v1
    assert "drug_cell_mean" not in open(os.path.join(ROOT, "bridgelab", "orchestration.py")).read()
    assert h.orchestration_v2.ROLE_TO_PROMPT_FILE["transfer_judge"] == "transfer_judge_v3.md"
    assert v1.ROLE_TO_PROMPT_FILE["transfer_judge"] == "transfer_judge_v2.md"
    assert h.source_hashes()["orchestration_v1"] != h.source_hashes()["orchestration_v2"]
    prompts = h.load_role_prompts(os.path.join(ROOT, "prompts"))
    assert "drug_cell_mean" in prompts["transfer_judge"]


def test_harness_v2_replays_recorded_judge_unchanged():
    h, result, ledger = _run_harness()
    assert result["status"] == "ok", result
    assert h.judge_output_of(result)["selected_method"] == "exact_pair_mean"


def test_harness_v2_accepts_drug_cell_mean_and_restores_validator():
    from bridgelab import validation
    before = validation.JUDGE_METHODS
    h, result, ledger = _run_harness({"selected_method": "drug_cell_mean", "method_parameters": {}})
    assert validation.JUDGE_METHODS == before, "JUDGE_METHODS must be restored"
    judge = h.judge_output_of(result)
    assert judge and judge["selected_method"] == "drug_cell_mean"
    runs = [e for e in ledger.entries() if e["kind"] == "tool_call"]
    assert runs and runs[0]["data"]["arguments"]["method"] == "drug_cell_mean"
    assert runs[0]["data"]["result"]["output"]["precision_method"] == 0.6313


def test_harness_v2_accepts_structure_cell_knn():
    h, result, ledger = _run_harness({"selected_method": "structure_cell_knn", "method_parameters": {}})
    judge = h.judge_output_of(result)
    assert judge and judge["selected_method"] == "structure_cell_knn"
    runs = [e for e in ledger.entries() if e["kind"] == "tool_call"]
    out = runs[0]["data"]["result"]["output"]
    assert out["method"] == "structure_cell_knn" and out["precision_method"] == 0.7027
    assert out["selected_candidates_preview"][0]["predicted_score"] > 0


def test_compile_decision_v2_matches_v1_for_v1_methods_and_ranks_new_one():
    from bridgelab import decision_compiler as v1
    from bridgelab.decision_compiler_v2 import compile_decision_v2
    train, cand = make_rows()
    judge = {"selected_domain": "mathematics", "selected_principle": "p", "selected_method": "drug_mean",
             "method_parameters": {}, "prediction": "x", "falsifier": "y", "required_data_fields": ["item_id"]}
    assert compile_decision_v2(judge, train, cand, 6)["ids"] == v1.compile_decision(judge, train, cand, 6)["ids"]
    new = dict(judge, selected_method="drug_cell_mean")
    rec = compile_decision_v2(new, train, cand, 6)
    assert rec["status"] == "ok" and rec["selected_method"] == "drug_cell_mean" and len(rec["ids"]) == 6
    bad = dict(new, method_parameters={"k": 1})
    assert compile_decision_v2(bad, train, cand, 6)["status"] == "failed"
    none = dict(new, selected_domain="none")
    assert compile_decision_v2(none, train, cand, 6)["status"] == "failed"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("ok   ", name)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("FAIL ", name, "->", repr(exc))
    print("%d tests, %d failed" % (len(tests), failed))
    sys.exit(1 if failed else 0)
