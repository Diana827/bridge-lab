"""Deterministic decision compiler (decision-compiler v1).

Sits after the Transfer Judge (option (a)): it turns a Judge v2 output
into an ordered selected_item_ids list using only train data. It makes
no model call, uses no randomness, reads no file, and takes no hidden
label: candidate rows are rejected outright if they carry any outcome
column. It modifies no existing module.

Judge v2 output contract (validated by validate_judge_output):
  required fields: selected_domain, selected_principle, selected_method,
  method_parameters, prediction, falsifier, required_data_fields.
  selected_domain in {mathematics, physics, chemistry, none}.
  selected_method in the allowlist below, or "none".
  Consistency: domain "none" requires method "none"; a selected domain
  requires a real method. Other Judge fields (candidate_scores,
  biological_hypothesis, mechanism, ...) are accepted but ignored by
  the compiler; only the seven contract fields are consumed.

Allowlisted methods (train data only; all deterministic; final
tie-break is always item_id ascending):
  exact_pair_mean: key = mean train observed_score over train items
    with the same unordered NSC pair. Pairs with fewer than
    min_pair_items train items fall back to drug_mean, then to the
    global train mean. Parameters: {min_pair_items: int >= 1} (default 1).
  drug_mean: key = mean of the two drugs' train means (a drug's train
    mean is over train items containing that NSC in either position;
    a drug with no train items uses the global train mean).
    Parameters: {} (any key is invalid).
  pair_frequency: key = count of train items with the candidate's
    unordered pair (descending); ties break by exact_pair_mean
    descending, then item_id ascending. Pairs with zero train items
    have no exact-pair mean, so the global train mean is used as the
    tie-break key for them (all such pairs tie on the fallback, then
    item_id ascending). Parameters: {}.

  The existing compute.selected_method tool is NOT used by this
  compiler, and its name does not appear in the allowlist. If a Judge
  explicitly selects a method whose ranking equals that tool's
  ranking, the compiler still computes the ranking itself from train
  rows and records the selected method name; the tool is never called
  as the Harness result. (Per the user's rule, the tool may only count
  as a Harness result when explicitly selected by the Judge and
  recorded as the selected method; this compiler satisfies the
  recording half by construction, and its methods are computed here,
  not delegated to the tool.)

Selection rules (frozen, via muse_runtime.validate_selection):
  exact item_id match against the candidate vocabulary; over-budget
  truncated in ranking order and flagged; duplicates deduplicated and
  flagged (a ranking over unique candidates cannot contain duplicates,
  but the validator is applied regardless); empty selection only via
  Judge none. Unknown ids invalidate the whole selection (failed).
  Budget is enforced for the budget_count given; the caller (the
  future driver) must pass a frozen budget count.

Outcomes: ok (validated selection), none (Judge none, valid, 0 hits),
failed (invalid Judge output, unknown method, invalid parameters,
candidate rows carrying outcomes, or invalid selection). A failed
compile is a failed run under ITT (0 hits), never a partial selection.
"""
from __future__ import annotations

from .ledger import canon, sha256
from .muse_runtime import validate_selection

VERSION = "decision-compiler v1"

DOMAINS = ("mathematics", "physics", "chemistry", "none")
METHODS = ("exact_pair_mean", "drug_mean", "pair_frequency", "none")
ALLOWED_DATA_FIELDS = ("item_id", "nsc1", "nsc2", "cellname", "grid_n",
                       "observed_score", "per_point_differences",
                       "conc1_values", "conc2_values")
OUTCOME_KEYS = ("observed_score", "score", "expectedgrowth", "percentgrowth")


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value == value and abs(value) != float("inf"))


def validate_judge_output(judge):
    """Return a list of contract errors (empty = valid). Never echoes
    model prose (prediction/falsifier text) in error messages."""
    errors = []
    if not isinstance(judge, dict):
        return ["judge output must be an object"]
    for field in ("selected_domain", "selected_principle", "selected_method",
                  "method_parameters", "prediction", "falsifier", "required_data_fields"):
        if field not in judge:
            errors.append("%s: missing required field" % field)
    if errors:
        return errors
    domain = judge["selected_domain"]
    method = judge["selected_method"]
    if domain not in DOMAINS:
        errors.append("selected_domain: must be one of %s" % ", ".join(DOMAINS))
    if method not in METHODS:
        errors.append("selected_method: must be one of %s" % ", ".join(METHODS))
    if domain == "none" and method != "none":
        errors.append("selected_domain none requires selected_method none")
    if domain != "none" and method == "none":
        errors.append("selected domain requires a real selected_method")
    if domain != "none":
        for field in ("selected_principle", "prediction", "falsifier"):
            if not isinstance(judge[field], str) or not judge[field].strip():
                errors.append("%s: must be a non-empty string" % field)
    else:
        for field in ("selected_principle", "prediction", "falsifier"):
            if not isinstance(judge[field], str):
                errors.append("%s: must be a string" % field)
    if not isinstance(judge["method_parameters"], dict):
        errors.append("method_parameters: must be an object")
    else:
        errors.extend(_validate_parameters(method, judge["method_parameters"]))
    fields = judge["required_data_fields"]
    if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
        errors.append("required_data_fields: must be a list of strings")
    else:
        for field in fields:
            if field not in ALLOWED_DATA_FIELDS:
                errors.append("required_data_fields: unknown field %r" % field)
    return errors


def _validate_parameters(method, params):
    if method == "exact_pair_mean":
        allowed = {"min_pair_items"}
        errors = ["method_parameters: unknown parameter %r" % k for k in params if k not in allowed]
        if "min_pair_items" in params:
            value = params["min_pair_items"]
            if not _is_int(value) or value < 1:
                errors.append("method_parameters.min_pair_items: must be an integer >= 1")
        return errors
    if method in ("drug_mean", "pair_frequency"):
        return ["method_parameters: %s takes no parameters" % method] if params else []
    if method == "none":
        return []
    return []


def _normalized_params(method, params):
    if method == "exact_pair_mean":
        return {"min_pair_items": params.get("min_pair_items", 1)}
    return {}


def _check_rows(train_rows, candidate_rows):
    if not isinstance(train_rows, list) or not train_rows:
        raise ValueError("train_rows must be a non-empty list")
    if not isinstance(candidate_rows, list) or not candidate_rows:
        raise ValueError("candidate_rows must be a non-empty list")
    seen_train, seen_cand = set(), set()
    for row in train_rows:
        if not isinstance(row, dict):
            raise ValueError("train rows must be objects")
        for key in ("item_id", "nsc1", "nsc2", "observed_score"):
            if key not in row:
                raise ValueError("train rows require %s" % key)
        if not _is_finite_number(row["observed_score"]):
            raise ValueError("train observed_score must be a finite number")
        if row["item_id"] in seen_train:
            raise ValueError("train item_ids must be unique")
        seen_train.add(row["item_id"])
    for row in candidate_rows:
        if not isinstance(row, dict):
            raise ValueError("candidate rows must be objects")
        for key in ("item_id", "nsc1", "nsc2"):
            if key not in row:
                raise ValueError("candidate rows require %s" % key)
        lowered = {str(k).lower() for k in row}
        if lowered & set(OUTCOME_KEYS):
            raise ValueError("candidate rows must not carry outcome columns")
        if row["item_id"] in seen_cand:
            raise ValueError("candidate item_ids must be unique")
        seen_cand.add(row["item_id"])


def _pair_key(nsc1, nsc2):
    return tuple(sorted((str(nsc1), str(nsc2))))


def _train_stats(train_rows):
    """Per-pair and per-drug train means/counts plus the global mean."""
    pair_sums, pair_counts = {}, {}
    drug_sums, drug_counts = {}, {}
    total, n = 0.0, 0
    for row in train_rows:
        score = row["observed_score"]
        total += score
        n += 1
        pair = _pair_key(row["nsc1"], row["nsc2"])
        pair_sums[pair] = pair_sums.get(pair, 0.0) + score
        pair_counts[pair] = pair_counts.get(pair, 0) + 1
        for nsc in (str(row["nsc1"]), str(row["nsc2"])):
            drug_sums[nsc] = drug_sums.get(nsc, 0.0) + score
            drug_counts[nsc] = drug_counts.get(nsc, 0) + 1
    global_mean = total / n
    pair_mean = {p: pair_sums[p] / pair_counts[p] for p in pair_sums}
    drug_mean = {d: drug_sums[d] / drug_counts[d] for d in drug_sums}
    return {"pair_mean": pair_mean, "pair_count": pair_counts,
            "drug_mean": drug_mean, "global_mean": global_mean}


def _rank_candidates(method, params, train_rows, candidate_rows):
    stats = _train_stats(train_rows)
    ranked = []
    for row in candidate_rows:
        pair = _pair_key(row["nsc1"], row["nsc2"])
        if method == "exact_pair_mean":
            if stats["pair_count"].get(pair, 0) >= params["min_pair_items"]:
                key = stats["pair_mean"][pair]
            else:
                key = _drug_key(row, stats)
            ranked.append((-key, row["item_id"]))
        elif method == "drug_mean":
            ranked.append((-_drug_key(row, stats), row["item_id"]))
        elif method == "pair_frequency":
            count = stats["pair_count"].get(pair, 0)
            fallback = stats["pair_mean"].get(pair, stats["global_mean"])
            ranked.append((-count, -fallback, row["item_id"]))
    ranked.sort()
    return [item_id for *_, item_id in ranked]


def _drug_key(row, stats):
    total = 0.0
    for nsc in (str(row["nsc1"]), str(row["nsc2"])):
        total += stats["drug_mean"].get(nsc, stats["global_mean"])
    return total / 2.0


def compile_decision(judge_output, train_rows, candidate_rows, budget_count, ledger=None):
    """Compile a Judge v2 output into a validated selection.

    Returns a record: {status, ids, flags, budget_count, selected_domain,
    selected_principle, selected_method, method_parameters, prediction,
    falsifier, required_data_fields} plus "error"/"validation_errors" on
    failure. Caller errors (malformed rows, bad budget) raise
    ValueError; Judge/contract problems return failed records.
    When ledger is given, a note records the method and parameters
    (never the full id list: only counts, flags, and a selection hash).
    """
    if isinstance(budget_count, bool) or not isinstance(budget_count, int) or budget_count < 1:
        raise ValueError("budget_count must be a positive integer")
    _check_rows(train_rows, candidate_rows)
    errors = validate_judge_output(judge_output)
    if errors:
        return {"status": "failed", "error": "invalid_judge_output",
                "validation_errors": errors, "ids": [], "flags": [],
                "budget_count": budget_count,
                "selected_domain": judge_output.get("selected_domain") if isinstance(judge_output, dict) else None,
                "selected_method": judge_output.get("selected_method") if isinstance(judge_output, dict) else None}
    domain = judge_output["selected_domain"]
    method = judge_output["selected_method"]
    params = _normalized_params(method, judge_output["method_parameters"])
    base = {"selected_domain": domain,
            "selected_principle": judge_output["selected_principle"],
            "selected_method": method, "method_parameters": params,
            "prediction": judge_output["prediction"],
            "falsifier": judge_output["falsifier"],
            "required_data_fields": judge_output["required_data_fields"],
            "budget_count": budget_count}
    if domain == "none":
        record = {"status": "none", "ids": [], "flags": [], **base}
        if ledger is not None:
            ledger.append("note", "executed", {"stage": "decision_compiler",
                          "status": "none", "selected_domain": "none",
                          "selected_method": "none", "method_parameters": params,
                          "budget_count": budget_count, "selected_count": 0,
                          "selection_sha256": sha256(canon([]))})
        return record
    ranked = _rank_candidates(method, params, train_rows, candidate_rows)
    candidate_ids = [row["item_id"] for row in candidate_rows]
    validated = validate_selection(ranked, candidate_ids, budget_count)
    if validated["status"] == "invalid":
        return {"status": "failed", "error": "invalid_selection",
                "ids": [], "flags": validated["flags"], **base}
    record = {"status": validated["status"], "ids": validated["ids"],
              "flags": validated["flags"], **base}
    if ledger is not None:
        ledger.append("note", "executed", {"stage": "decision_compiler",
                      "status": validated["status"], "selected_domain": domain,
                      "selected_method": method, "method_parameters": params,
                      "budget_count": budget_count, "selected_count": len(validated["ids"]),
                      "flags": validated["flags"],
                      "selection_sha256": sha256(canon(validated["ids"]))})
    return record
