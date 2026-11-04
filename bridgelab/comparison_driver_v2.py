"""Two-arm comparison driver for E0004 (comparison-driver-v2 v1).

New module only. It modifies no existing module and no sealed packet, so E0003
stays verifiable. It reuses comparison_driver's scoring, bootstrap, gate and
reporting helpers unchanged and swaps the two arm runners:

  Baseline arm : criterion_baseline.run_criterion_baseline. One model call per
                 budget and run index picks a ranking method; Python ranks.
  Harness arm  : decision_compiler_v2.compile_decision_v2 on the Transfer
                 Judge outputs of the harness_v2 pipeline runs.

Both arms rank with the same code (decision_compiler_v2), so they differ only in
how the method was chosen.

Order of events, as in the E0003 driver and enforced by HiddenLabelGate:
  1. both arms run (labels are not an argument of either arm);
  2. both arms are marked done (ok, none or failed all count);
  3. the label loader is called once, through the gate;
  4. every run index is scored, bootstrapped and put through the per-index gate.

Study-level rule (comparison-prereg-v5/PREREG_E0004.md):
  superiority is claimed only if the per-index gate passes at 2 or more of the
  run indices and no index has a primary CI favouring the baseline. An index at
  which both arms chose the same method at every budget is a tie. A study in
  which every index is a tie is reported as "no difference".
"""
from __future__ import annotations

from .comparison import FROZEN_BUDGET_COUNTS, HiddenLabelGate, PRIMARY_BUDGET
from .comparison_driver import (_check_budgets, _check_indices, _summary, aggregate_usage,
                                arm_status, evaluate_index, unify_arm_record)
from .criterion_baseline import run_criterion_baseline
from .decision_compiler_v2 import compile_decision_v2
from .ledger import canon, sha256
from .orchestration import validate_split_ids

VERSION = "comparison-driver-v2 v1"


def _usage_dict(ledger, start):
    usage = aggregate_usage(ledger.entries()[start:])
    return ({"prompt_tokens": usage["prompt_tokens"], "completion_tokens": usage["completion_tokens"],
             "total_tokens": usage["total_tokens"], "cost": usage["cost"],
             "n_model_calls": usage["n_model_calls"], "usage_missing_calls": usage["usage_missing_calls"]},
            usage["latency_ms_total"])


def run_baseline_arm_v2(client, ledger, train_rows, candidate_rows, budgets, run_indices=(0, 1, 2),
                        run_id=None, ledger_path=None):
    _check_budgets(budgets)
    _check_indices(run_indices)
    comparison_run_id = run_id if run_id is not None else "run-comparison"
    path = ledger_path if ledger_path is not None else getattr(ledger, "path", None)
    results = {}
    for budget in budgets:
        results[budget] = {}
        for index in run_indices:
            start = len(ledger.entries())
            record = run_criterion_baseline(client, ledger, train_rows, candidate_rows, budget, run_index=index)
            model_usage, latency = _usage_dict(ledger, start)
            results[budget][index] = unify_arm_record(
                record, "%s:baseline:%d:%d" % (comparison_run_id, budget, index), "baseline", path,
                model_usage=model_usage, latency=latency)
    return results


def run_harness_arm_v2(judge_outputs_by_index, train_rows, candidate_rows, budgets, run_indices=(0, 1, 2),
                       ledger=None, run_id=None, ledger_path=None, harness_usage_by_index=None,
                       harness_latency_by_index=None):
    _check_budgets(budgets)
    _check_indices(run_indices)
    for index in run_indices:
        if index not in judge_outputs_by_index:
            raise ValueError("missing Judge output for run index %s" % index)
    comparison_run_id = run_id if run_id is not None else "run-comparison"
    path = ledger_path if ledger_path is not None else (getattr(ledger, "path", None) if ledger is not None else None)
    usage_by_index = harness_usage_by_index or {}
    latency_by_index = harness_latency_by_index or {}
    results = {}
    for budget in budgets:
        results[budget] = {}
        for index in run_indices:
            judge_output = judge_outputs_by_index[index]
            if judge_output is None:
                record = {"status": "failed", "error": "pipeline_judge_failed", "ids": [], "flags": [],
                          "budget_count": budget, "run_index": index, "selected_domain": None,
                          "selected_method": None}
            else:
                record = dict(compile_decision_v2(judge_output, train_rows, candidate_rows, budget, ledger=ledger))
                record["run_index"] = index
            results[budget][index] = unify_arm_record(
                record, "%s:harness:%d:%d" % (comparison_run_id, budget, index), "harness", path,
                model_usage=usage_by_index.get(index), latency=latency_by_index.get(index))
    return results


def _summary_v2(record):
    summary = _summary(record)
    summary["selected_method"] = record.get("selected_method")
    return summary


def study_level(report, run_indices):
    """Apply the preregistered study-level rule to a finished report."""
    passing, favoring_baseline, ties = [], [], []
    for index in run_indices:
        claim = report["claims"][index]
        if claim.get("applicable") and claim.get("claim_superiority"):
            passing.append(index)
        primary = report["bootstrap"].get(PRIMARY_BUDGET, {}).get(index)
        if primary and primary.get("ci_high") is not None and primary["ci_high"] < 0:
            favoring_baseline.append(index)
        same = all(report["baseline"][b][index]["ids_sha256"] == report["harness"][b][index]["ids_sha256"]
                   and report["baseline"][b][index]["status"] == "ok"
                   and report["harness"][b][index]["status"] == "ok"
                   for b in report["budgets"])
        if same:
            ties.append(index)
    superiority = len(passing) >= 2 and not favoring_baseline
    if superiority:
        outcome = "superiority"
    elif len(ties) == len(run_indices):
        outcome = "no_difference"
    elif favoring_baseline:
        outcome = "baseline_favored_at_some_index"
    else:
        outcome = "no_superiority_claim"
    return {"claim_superiority": superiority, "outcome": outcome,
            "indices_passing_gate": passing, "indices_favoring_baseline": favoring_baseline,
            "tie_indices": ties, "rule": "gate passes at >= 2 indices and no index favours the baseline; "
                                          "identical selections at every budget count as a tie"}


def run_comparison_v2(baseline_client, ledger, judge_outputs_by_index, train_rows, candidate_rows, budgets,
                      label_loader, run_indices=(0, 1, 2), n_bootstrap=1000, seed=42, run_id=None,
                      ledger_path=None, harness_usage_by_index=None, harness_latency_by_index=None,
                      split_ids=None):
    """Run the E0004 comparison and return its report (hashes and counts, never id lists)."""
    _check_budgets(budgets)
    _check_indices(run_indices)
    split_info = None
    if split_ids is not None:
        validate_split_ids(split_ids)

        def _pairs(rows):
            return {":".join(sorted([str(r["nsc1"]), str(r["nsc2"])])) for r in rows}

        if _pairs(train_rows) != set(split_ids["train_ids"]):
            raise ValueError("split_ids train_ids do not match train_rows pairs")
        if _pairs(candidate_rows) != set(split_ids["hidden_ids"]):
            raise ValueError("split_ids hidden_ids do not match candidate_rows pairs")
        split_info = {"split_ids_sha256": sha256(canon(split_ids)),
                      "split_train_pairs": len(split_ids["train_ids"]),
                      "split_hidden_pairs": len(split_ids["hidden_ids"])}
    ids = [row["item_id"] for row in candidate_rows]
    if len(set(ids)) != len(ids):
        raise ValueError("candidate_rows item_ids must be unique")
    start = len(ledger.entries())
    rid = run_id if run_id is not None else "run-comparison"
    path = ledger_path if ledger_path is not None else getattr(ledger, "path", None)
    baseline = run_baseline_arm_v2(baseline_client, ledger, train_rows, candidate_rows, budgets, run_indices,
                                   run_id=rid, ledger_path=path)
    harness = run_harness_arm_v2(judge_outputs_by_index, train_rows, candidate_rows, budgets, run_indices,
                                 ledger=ledger, run_id=rid, ledger_path=path,
                                 harness_usage_by_index=harness_usage_by_index,
                                 harness_latency_by_index=harness_latency_by_index)
    b_status = arm_status([baseline[b][i] for b in budgets for i in run_indices])
    h_status = arm_status([harness[b][i] for b in budgets for i in run_indices])
    gate = HiddenLabelGate()
    gate.mark_baseline_done(b_status)
    gate.mark_harness_done(h_status)
    calls = {"count": 0}

    def counted_loader():
        calls["count"] += 1
        return label_loader()

    labels = gate.open_labels(counted_loader)
    report = {"driver_version": VERSION, "run_id": rid, "split": split_info,
              "budgets": list(budgets), "run_indices": list(run_indices), "primary_budget": PRIMARY_BUDGET,
              "baseline": {b: {i: _summary_v2(baseline[b][i]) for i in run_indices} for b in budgets},
              "harness": {b: {i: _summary_v2(harness[b][i]) for i in run_indices} for b in budgets},
              "gate": {"baseline_status": b_status, "harness_status": h_status,
                       "label_loader_calls": calls["count"]},
              "usage": aggregate_usage(ledger.entries()[start:]),
              "scores": {}, "bootstrap": {}, "claims": {}}
    for index in run_indices:
        evaluated = evaluate_index({b: baseline[b][index] for b in budgets},
                                   {b: harness[b][index] for b in budgets},
                                   candidate_rows, labels, budgets, n_bootstrap=n_bootstrap, seed=seed)
        for budget in budgets:
            report["scores"].setdefault(budget, {})[index] = evaluated["scores"][budget]
            report["bootstrap"].setdefault(budget, {})[index] = evaluated["bootstrap"][budget]
        report["claims"][index] = evaluated["claim"]
    report["repeat_summary"] = {}
    for budget in budgets:
        report["repeat_summary"][budget] = {}
        for arm in ("baseline", "harness"):
            p = [report["scores"][budget][i][arm]["precision"] for i in run_indices]
            report["repeat_summary"][budget][arm] = {"n_repeats": len(p), "mean_precision": sum(p) / len(p),
                                                     "min_precision": min(p), "max_precision": max(p),
                                                     "range_precision": max(p) - min(p)}
    report["study"] = study_level(report, run_indices)
    return report
