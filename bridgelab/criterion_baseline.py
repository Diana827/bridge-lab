"""Criterion baseline arm (criterion-baseline v1), written for the E0004 design.

New module only; it modifies no existing module and no sealed packet.

The E0003 baseline asked one model call to write thousands of item ids and
failed every run (invalid JSON, unknown ids). This baseline asks one model
call for something small: which allowlisted ranking method to use. Python
then ranks every candidate with the same code the Harness arm uses
(decision_compiler_v2.compile_selection_v2), so the two arms differ only in
how the method was chosen: one call here, five lenses plus Validator,
Judge, Critic and Reporter in the Harness.

The model never sees a hidden label, a candidate file, or an id list. It
sees the question and the method descriptions below. Only selected_method
and method_parameters are read from its reply; any other content, including
claimed numbers, is ignored and never scored.

Failure rules are unchanged from comparison-exec v1: invalid JSON after 3
attempts, an unknown method, or invalid parameters is a failed run and
counts as 0 hits (intention-to-treat). An API/model failure is a failed run
immediately.
"""
from __future__ import annotations

from .comparison import MAX_JSON_ATTEMPTS, _extract_text
from .decision_compiler_v2 import METHODS_V2, compile_selection_v2
from .muse_runtime import run_model_call
from .validation import parse_json_strict

VERSION = "criterion-baseline v1"

PROMPT_TEMPLATE = """You are choosing how to rank drug-pair x cell-line candidates for the next screening round.

Question: Which drug combinations should we validate next, using no more than {budget_count} candidates (the screening budget)?

Rules: use training data only. Do not use hidden labels. Make no clinical claims. Candidates are pairs that never appear in the training data, each tested in one cell line. Python will rank every candidate with the method you choose and take the top {budget_count}.

Allowed methods:
- exact_pair_mean: rank by the mean training score of the same drug pair; pairs unseen in training fall back to drug_mean. Optional parameter {"min_pair_items": integer >= 1}.
- drug_mean: rank by the average of the two drugs' mean training scores.
- pair_frequency: rank by how often the pair appears in training.
- drug_cell_mean: rank by the average of the two drugs' mean training scores in the candidate's own cell line.
- structure_cell_knn: drug_cell_mean, adjusted by how training pairs with structurally similar drugs (molecular fingerprint similarity) scored in the same cell line.

Reply with JSON only, in this form:
{"selected_method": "<one allowed method>", "method_parameters": {}}
"""


def run_criterion_baseline(client, ledger, train_rows, candidate_rows, budget_count, run_index=0,
                           prompt_template=PROMPT_TEMPLATE):
    """One baseline run for one budget. Returns a run record in the same shape
    as comparison.run_baseline (status ok/none/failed, ids, flags, ...)."""
    if "{budget_count}" not in prompt_template:
        raise ValueError("prompt template is missing the {budget_count} placeholder")
    prompt = prompt_template.replace("{budget_count}", str(budget_count))
    messages = [{"role": "user", "content": prompt}]

    def fail(error, attempts, **extra):
        ledger.append("note", "executed", {"arm": "baseline", "run_index": run_index,
                      "status": "failed", "error": error, "budget_count": budget_count,
                      "attempts": attempts})
        return {"arm": "baseline", "run_index": run_index, "status": "failed", "error": error,
                "ids": [], "flags": [], "budget_count": budget_count, "attempts": attempts,
                "selected_method": None, **extra}

    last_error = None
    for attempts in range(1, MAX_JSON_ATTEMPTS + 1):
        call = run_model_call(client, ledger, messages,
                              label="criterion_baseline_run_%d_attempt_%d" % (run_index, attempts))
        if call["status"] != "ok":
            return fail(call["error"], attempts)
        text = _extract_text(call)
        parsed, parse_error = None, None
        if text is None:
            parse_error = "missing_model_text"
        else:
            try:
                parsed = parse_json_strict(text)
            except ValueError as exc:
                parse_error = "invalid_json: %s" % exc
        if (parse_error is not None or not isinstance(parsed, dict)
                or not isinstance(parsed.get("selected_method"), str)
                or not isinstance(parsed.get("method_parameters", {}), dict)):
            last_error = parse_error or "selected_method_missing_or_invalid"
            continue
        method = parsed["selected_method"]
        if method not in METHODS_V2:
            return fail("unknown_method", attempts, selected_method=method)
        record = compile_selection_v2(method, parsed.get("method_parameters", {}),
                                      train_rows, candidate_rows, budget_count, ledger=ledger)
        if record["status"] == "failed":
            return fail(record.get("error", "compile_failed"), attempts, selected_method=method)
        record = dict(record)
        record.update({"arm": "baseline", "run_index": run_index, "attempts": attempts})
        ledger.append("note", "executed", {"arm": "baseline", "run_index": run_index,
                      "status": record["status"], "selected_method": method,
                      "budget_count": budget_count, "attempts": attempts})
        return record
    return fail("invalid_json_after_retries", MAX_JSON_ATTEMPTS, last_error=last_error)
