"""Two-arm comparison driver (comparison-driver v1.3).

Sequences the already-built components into one comparison run. It is
new code only: it modifies no existing module and no sealed packet.

Pipeline (in order, every step recorded in the run ledger):
  1. Baseline arm: comparison.run_baseline per frozen (or, for fake
     verification only, synthetic) budget and per run index. Loop
     order is budgets outer, run indices inner; fake clients must
     queue responses in that order.
  2. Harness arm: decision_compiler.compile_decision per budget and
     per run index, from caller-supplied Judge v2 outputs (for the
     live run these come from run_pipeline; for fake verification
     they are scripted). The driver never generates Judge output.
  3. HiddenLabelGate: one gate for the whole comparison. Both arms
     are marked done once all their runs finished (ok, none, or
     failed all count as done, per the sealed rule); an arm with any
     failed run is marked "failed", an all-none arm "none", otherwise
     "ok". Labels open exactly once, after both arms are done, via
     the caller-supplied loader. Labels never reach arm execution:
     arms take no labels argument; only post-gate scoring functions
     receive the opened labels.
  4. Scoring: comparison.score_selection per budget, index, and arm
     (ITT denominator = budget_count; failed/none = 0 hits).
  5. Bootstrap + claim gate: per budget and bootstrap index,
     comparison.paired_bootstrap_ci over per-item pair rows (pair =
     the candidate's unordered NSC pair, treated as an opaque group
     key). The PRIMARY CI is on the ITT precision difference
     (hits / budget_count), matching the ITT precisions the gate
     compares for wins; the per-protocol CI is reported only as
     secondary inside each bootstrap entry and never gates a claim.
     The claim gate (comparison.evaluate_claim_gate) applies
     ONLY when the budgets are exactly the frozen budgets; otherwise
     the report marks the claim not applicable with a reason, rather
     than forcing a False claim. When an arm selects nothing the CI
     is None and the gate correctly fails.
  6. Repeat summary: per budget and arm, the report carries
     repeat_summary with the mean, min, max, and range of the
     per-index ITT precisions across run indices (no run index is
     dropped).

The report carries selection hashes and counts, never id lists.

Unified execution interface: both arm runners return records with
run_id, arm, run_index, selected_item_ids, budget, status,
failure_reason, model_usage, latency, and ledger_path (plus legacy
keys ids/budget_count/flags). Both arms' candidate lists reach the
Python scorer through the same function, to_scorer_input, which
normalizes any arm record to {status, ids} before score_selection.
Saved reports carry summaries of the unified records (hashes and
counts, never id lists); the in-memory unified records carry the
lists the scorer needs.
The report also carries a "usage" section aggregating the model_call
ledger entries appended during run_comparison (the driver segment:
Baseline model calls via run_model_call, which record latency_ms,
usage, and cost). Token sums are reported only when every counted
call returned a complete integer usage object; otherwise the sums are
None and usage_missing_calls counts the incomplete calls. Cost is
"MISSING" unless a documented price produced numeric costs. Latency
is timing and therefore inherently non-deterministic: fake-run
determinism is verified on the report excluding latency_ms_total.
Pipeline (Harness expert/Judge) model calls are recorded by the caller
in the same ledger when the pipeline runs; the complete live run
record must aggregate the full ledger, not just this segment.
The driver reads no file and takes no dataset path: train rows,
candidate rows, Judge outputs, and the label loader are all supplied
by the caller.
"""
from __future__ import annotations

from .comparison import (
    FROZEN_BUDGET_COUNTS,
    PRIMARY_BUDGET,
    HiddenLabelGate,
    evaluate_claim_gate,
    paired_bootstrap_ci,
    run_baseline,
    score_selection,
)
from .decision_compiler import compile_decision
from .ledger import canon, sha256
from .orchestration import validate_split_ids

VERSION = "comparison-driver v1.3"


def _check_budgets(budgets):
    if not isinstance(budgets, (list, tuple)) or not budgets:
        raise ValueError("budgets must be a non-empty list")
    if len(set(budgets)) != len(budgets):
        raise ValueError("budgets must not contain duplicates")
    for budget in budgets:
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
            raise ValueError("budgets must be positive integers")


def _check_indices(run_indices):
    if not isinstance(run_indices, (list, tuple)) or not run_indices:
        raise ValueError("run_indices must be a non-empty list")
    if len(set(run_indices)) != len(run_indices):
        raise ValueError("run_indices must not contain duplicates")
    for index in run_indices:
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError("run_indices must be non-negative integers")


def _summary(record):
    """Report summary of an arm record: hashes and counts, never id lists.

    Unified fields (run_id, model_usage, latency, ledger_path, and so
    on) are carried through when present; id lists never are."""
    summary = {"status": record["status"], "selected_count": len(record["ids"]),
               "flags": list(record["flags"]),
               "ids_sha256": sha256(canon(record["ids"]))}
    for key in ("run_id", "arm", "run_index", "budget", "failure_reason",
                "model_usage", "latency", "ledger_path", "attempts"):
        if key in record:
            summary[key] = record[key]
    return summary


def to_scorer_input(record):
    """Normalize any arm record to the scorer's identical input shape.

    Both arms pass through this function before score_selection, so
    the Python scorer always receives {status, ids} in the same way
    regardless of which arm produced the selection."""
    ids = record.get("selected_item_ids", record.get("ids", []))
    return {"status": record["status"], "ids": list(ids)}


def _compiler_usage():
    """Model usage for a compiler-only arm segment: no model calls."""
    return {"prompt_tokens": None, "completion_tokens": None,
            "total_tokens": None, "cost": "MISSING",
            "n_model_calls": 0, "usage_missing_calls": 0}


def unify_arm_record(record, run_id, arm, ledger_path, model_usage=None,
                     latency=None):
    """Attach the unified execution-interface fields to an arm record.

    Unified fields: run_id, arm, run_index, selected_item_ids, budget,
    status, failure_reason, model_usage, latency, ledger_path. Legacy
    keys (ids, budget_count, flags, attempts, selected_*) are kept so
    existing callers keep working. The record itself (in memory) does
    carry selected_item_ids for the scorer; saved reports use
    _summary, which never carries id lists."""
    ids = list(record.get("ids", []))
    usage = model_usage if model_usage is not None else _compiler_usage()
    unified = dict(record)
    unified.update({
        "run_id": run_id, "arm": arm, "run_index": record.get("run_index"),
        "selected_item_ids": ids, "budget": record.get("budget_count"),
        "failure_reason": record.get("error"),
        "model_usage": usage,
        "latency": latency if latency is not None else 0.0,
        "ledger_path": ledger_path})
    return unified


def arm_status(records):
    """Collapse arm records to one gate status: any failed -> failed,
    all none -> none, otherwise ok."""
    statuses = [r["status"] for r in records]
    if "failed" in statuses:
        return "failed"
    if all(s == "none" for s in statuses):
        return "none"
    return "ok"


def run_baseline_arm(client, ledger, prompt_template, candidate_ids, budgets,
                     run_indices=(0, 1, 2), run_id=None, ledger_path=None):
    """Run the Baseline arm for every budget and run index.

    Returns unified arm records (see unify_arm_record). Per-call model
    usage and latency are sliced from the ledger segment of that call,
    so each record's model_usage/latency describe exactly its own
    model calls."""
    _check_budgets(budgets)
    _check_indices(run_indices)
    comparison_run_id = run_id if run_id is not None else "run-comparison"
    effective_ledger_path = ledger_path if ledger_path is not None else getattr(ledger, "path", None)
    results = {}
    for budget in budgets:
        results[budget] = {}
        for index in run_indices:
            start = len(ledger.entries())
            record = run_baseline(
                client, ledger, prompt_template, candidate_ids, budget, run_index=index)
            usage = aggregate_usage(ledger.entries()[start:])
            model_usage = {"prompt_tokens": usage["prompt_tokens"],
                           "completion_tokens": usage["completion_tokens"],
                           "total_tokens": usage["total_tokens"],
                           "cost": usage["cost"],
                           "n_model_calls": usage["n_model_calls"],
                           "usage_missing_calls": usage["usage_missing_calls"]}
            results[budget][index] = unify_arm_record(
                record, "%s:baseline:%d:%d" % (comparison_run_id, budget, index),
                "baseline", effective_ledger_path, model_usage=model_usage,
                latency=usage["latency_ms_total"])
    return results


def run_harness_arm(judge_outputs_by_index, train_rows, candidate_rows, budgets,
                    run_indices=(0, 1, 2), ledger=None, run_id=None,
                    ledger_path=None, harness_usage_by_index=None,
                    harness_latency_by_index=None):
    """Run the Harness arm: compile each index's Judge output per budget.

    Returns unified arm records. A missing index is a caller error
    (ValueError); a None Judge output (e.g. the live pipeline's Judge
    role failed) is a failed run for that index, not a caller error.
    Pipeline model usage/latency for an index, when supplied by the
    caller (the live caller aggregates the pipeline ledger segment),
    is attached to that index's records; otherwise the records carry
    the compiler-only usage (no model calls in the driver segment)."""
    _check_budgets(budgets)
    _check_indices(run_indices)
    for index in run_indices:
        if index not in judge_outputs_by_index:
            raise ValueError("missing Judge output for run index %s" % index)
    comparison_run_id = run_id if run_id is not None else "run-comparison"
    effective_ledger_path = ledger_path if ledger_path is not None else (
        getattr(ledger, "path", None) if ledger is not None else None)
    usage_by_index = harness_usage_by_index or {}
    latency_by_index = harness_latency_by_index or {}
    candidate_ids = [row["item_id"] for row in candidate_rows]
    results = {}
    for budget in budgets:
        results[budget] = {}
        for index in run_indices:
            judge_output = judge_outputs_by_index[index]
            if judge_output is None:
                record = {"status": "failed", "error": "pipeline_judge_failed",
                          "ids": [], "flags": [], "budget_count": budget,
                          "run_index": index, "selected_domain": None,
                          "selected_method": None}
            else:
                record = compile_decision(
                    judge_output, train_rows, candidate_rows,
                    budget, ledger=ledger)
                record = dict(record)
                record["run_index"] = index
            results[budget][index] = unify_arm_record(
                record, "%s:harness:%d:%d" % (comparison_run_id, budget, index),
                "harness", effective_ledger_path,
                model_usage=usage_by_index.get(index),
                latency=latency_by_index.get(index))
    return results


def build_pair_rows(candidate_rows, baseline_ids, harness_ids, labels):
    """Build per-item pair rows for the paired bootstrap.

    Call only with labels opened through the gate. pair is the
    candidate's unordered NSC pair joined by ':', treated as an
    opaque group key (never interpreted as drug names)."""
    baseline_set = set(baseline_ids)
    harness_set = set(harness_ids)
    rows = []
    for row in candidate_rows:
        item_id = row["item_id"]
        pair = ":".join(sorted((str(row["nsc1"]), str(row["nsc2"]))))
        score = labels[item_id]
        rows.append({"pair": pair,
                     "b_sel": 1 if item_id in baseline_set else 0,
                     "b_hit": 1 if item_id in baseline_set and score > 0 else 0,
                     "h_sel": 1 if item_id in harness_set else 0,
                     "h_hit": 1 if item_id in harness_set and score > 0 else 0})
    return rows


def evaluate_index(baseline_by_budget, harness_by_budget, candidate_rows, labels,
                   budgets, n_bootstrap=1000, seed=42, run_bootstrap=True):
    """Score one run index at every budget and apply the claim gate.

    baseline_by_budget / harness_by_budget map budget -> run record.
    Returns scores, bootstrap CIs, and the claim evaluation (or a
    not-applicable marker when budgets are not the frozen budgets or
    when bootstrap was skipped for this index)."""
    scores = {}
    for budget in budgets:
        # Both arms pass through to_scorer_input first, so the Python
        # scorer receives both candidate lists in exactly the same way.
        baseline_input = to_scorer_input(baseline_by_budget[budget])
        harness_input = to_scorer_input(harness_by_budget[budget])
        scores[budget] = {
            "baseline": score_selection(baseline_input, labels, budget),
            "harness": score_selection(harness_input, labels, budget)}
    bootstrap = {}
    if run_bootstrap:
        for budget in budgets:
            pair_rows = build_pair_rows(
                candidate_rows, to_scorer_input(baseline_by_budget[budget])["ids"],
                to_scorer_input(harness_by_budget[budget])["ids"], labels)
            bootstrap[budget] = paired_bootstrap_ci(pair_rows, n_bootstrap=n_bootstrap,
                                                    seed=seed, budget_count=budget)
    if not run_bootstrap:
        claim = {"applicable": False, "reason": "bootstrap not run for this index"}
    elif tuple(budgets) != tuple(FROZEN_BUDGET_COUNTS) and set(budgets) != set(FROZEN_BUDGET_COUNTS):
        claim = {"applicable": False,
                 "reason": "claim gate applies only to the frozen budgets"}
    else:
        evaluations = {}
        for budget in budgets:
            evaluations[budget] = {
                "baseline_precision": scores[budget]["baseline"]["precision"],
                "harness_precision": scores[budget]["harness"]["precision"],
                "ci_low": bootstrap[budget]["ci_low"],
                "ci_high": bootstrap[budget]["ci_high"]}
        claim = {"applicable": True, **evaluate_claim_gate(evaluations)}
    return {"scores": scores, "bootstrap": bootstrap, "claim": claim}


def aggregate_usage(ledger_entries):
    """Aggregate model_call ledger entries into a usage summary.

    Token sums are integers only when every counted call returned a
    complete integer usage object; otherwise sums are None. Cost sums
    only when every counted call has a numeric cost, else "MISSING".
    """
    n_calls = 0
    latency_total = 0.0
    latency_missing = 0
    prompt = 0
    completion = 0
    total = 0
    usage_missing = 0
    costs = []
    for entry in ledger_entries:
        if entry["kind"] != "model_call":
            continue
        n_calls += 1
        data = entry["data"]
        latency = data.get("latency_ms")
        if isinstance(latency, (int, float)) and not isinstance(latency, bool):
            latency_total += latency
        else:
            latency_missing += 1
        usage = data.get("usage")
        if isinstance(usage, dict) and all(
                isinstance(usage.get(k), int) and not isinstance(usage.get(k), bool)
                for k in ("prompt_tokens", "completion_tokens", "total_tokens")):
            prompt += usage["prompt_tokens"]
            completion += usage["completion_tokens"]
            total += usage["total_tokens"]
        else:
            usage_missing += 1
        cost = data.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            costs.append(cost)
    return {"n_model_calls": n_calls,
            "latency_ms_total": round(latency_total, 3),
            "latency_missing_calls": latency_missing,
            "prompt_tokens": prompt if usage_missing == 0 else None,
            "completion_tokens": completion if usage_missing == 0 else None,
            "total_tokens": total if usage_missing == 0 else None,
            "usage_missing_calls": usage_missing,
            "cost": round(sum(costs), 6) if costs and len(costs) == n_calls else "MISSING"}


def run_comparison(baseline_client, ledger, prompt_template, judge_outputs_by_index,
                   train_rows, candidate_rows, budgets, label_loader,
                   run_indices=(0, 1, 2), n_bootstrap=1000, seed=42,
                   bootstrap_indices=None, run_id=None, ledger_path=None,
                   harness_usage_by_index=None, harness_latency_by_index=None,
                   split_ids=None):
    """Run a full two-arm comparison and return its report.

    Arms run first; labels open once through a single HiddenLabelGate
    after both arms are done; then every index is scored, and the
    bootstrap + claim gate run for bootstrap_indices (default: all
    run indices). Arm records are unified (run_id, arm, run_index,
    selected_item_ids, budget, status, failure_reason, model_usage,
    latency, ledger_path); the report carries summaries of those
    records with hashes and counts, never id lists. The report also
    carries repeat_summary: per budget and arm, the mean, min, max,
    and range of the per-index ITT precisions across run indices.
    """
    _check_budgets(budgets)
    _check_indices(run_indices)
    if bootstrap_indices is None:
        bootstrap_indices = tuple(run_indices)
    # The split contract, shared with the live caller and the pipeline:
    # when the caller passes the packet's split_ids dict, the driver
    # enforces the same validation and proves the driver inputs ARE
    # that split — the unordered pair sets from train_rows and
    # candidate_rows must equal the split's train/hidden id sets.
    # split_ids=None (fake mode, older callers) skips this binding.
    split_info = None
    if split_ids is not None:
        validate_split_ids(split_ids)

        def _pair_set(rows):
            pairs = set()
            for r in rows:
                pairs.add(":".join(sorted([str(r["nsc1"]), str(r["nsc2"])])))
            return pairs

        if _pair_set(train_rows) != set(split_ids["train_ids"]):
            raise ValueError("split_ids train_ids do not match train_rows pairs")
        if _pair_set(candidate_rows) != set(split_ids["hidden_ids"]):
            raise ValueError("split_ids hidden_ids do not match candidate_rows pairs")
        split_info = {"split_ids_sha256": sha256(canon(split_ids)),
                      "split_train_pairs": len(split_ids["train_ids"]),
                      "split_hidden_pairs": len(split_ids["hidden_ids"])}
    candidate_ids = [row["item_id"] for row in candidate_rows]
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("candidate_rows item_ids must be unique")
    ledger_start = len(ledger.entries())
    effective_run_id = run_id if run_id is not None else "run-comparison"
    effective_ledger_path = ledger_path if ledger_path is not None else getattr(ledger, "path", None)
    baseline = run_baseline_arm(baseline_client, ledger, prompt_template,
                                candidate_ids, budgets, run_indices,
                                run_id=effective_run_id,
                                ledger_path=effective_ledger_path)
    harness = run_harness_arm(judge_outputs_by_index, train_rows, candidate_rows,
                              budgets, run_indices, ledger=ledger,
                              run_id=effective_run_id,
                              ledger_path=effective_ledger_path,
                              harness_usage_by_index=harness_usage_by_index,
                              harness_latency_by_index=harness_latency_by_index)
    baseline_records = [baseline[b][i] for b in budgets for i in run_indices]
    harness_records = [harness[b][i] for b in budgets for i in run_indices]
    baseline_gate_status = arm_status(baseline_records)
    harness_gate_status = arm_status(harness_records)
    gate = HiddenLabelGate()
    gate.mark_baseline_done(baseline_gate_status)
    gate.mark_harness_done(harness_gate_status)
    loader_calls = {"count": 0}

    def counted_loader():
        loader_calls["count"] += 1
        return label_loader()

    labels = gate.open_labels(counted_loader)
    report = {"driver_version": VERSION, "run_id": effective_run_id,
              "split": split_info,
              "budgets": list(budgets), "run_indices": list(run_indices),
              "primary_budget": PRIMARY_BUDGET,
              "baseline": {b: {i: _summary(baseline[b][i]) for i in run_indices}
                           for b in budgets},
              "harness": {b: {i: _summary(harness[b][i]) for i in run_indices}
                          for b in budgets},
              "gate": {"baseline_status": baseline_gate_status,
                       "harness_status": harness_gate_status,
                       "label_loader_calls": loader_calls["count"]},
              "usage": aggregate_usage(ledger.entries()[ledger_start:]),
              "scores": {}, "bootstrap": {}, "claims": {}}
    for index in run_indices:
        baseline_by_budget = {b: baseline[b][index] for b in budgets}
        harness_by_budget = {b: harness[b][index] for b in budgets}
        evaluated = evaluate_index(
            baseline_by_budget, harness_by_budget, candidate_rows, labels,
            budgets, n_bootstrap=n_bootstrap, seed=seed,
            run_bootstrap=index in bootstrap_indices)
        for budget in budgets:
            report["scores"].setdefault(budget, {})[index] = evaluated["scores"][budget]
            if budget in evaluated["bootstrap"]:
                report["bootstrap"].setdefault(budget, {})[index] = evaluated["bootstrap"][budget]
        report["claims"][index] = evaluated["claim"]
    report["repeat_summary"] = {}
    for budget in budgets:
        report["repeat_summary"][budget] = {}
        for arm in ("baseline", "harness"):
            precisions = [report["scores"][budget][index][arm]["precision"]
                          for index in run_indices]
            report["repeat_summary"][budget][arm] = {
                "n_repeats": len(precisions),
                "mean_precision": sum(precisions) / len(precisions),
                "min_precision": min(precisions),
                "max_precision": max(precisions),
                "range_precision": max(precisions) - min(precisions)}
    return report
