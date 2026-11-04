"""Strict validation for role outputs, sealed plans, and tool-call structures.

Source of truth for role schemas: the attached role prompt files in
prompts/. Judge scores are finite numbers in [0, 1] (confirmed by the
user; the prompt file itself only shows 0 as a placeholder).
Every output must be strict JSON, an object, with exactly the fields
defined for that role. Unknown fields, duplicate JSON keys, NaN/Infinity,
and wrong types are all errors. Validation only checks what the model
supplied against a fixed schema; it never invents a number, tool, or result.
"""
from __future__ import annotations
import json, math, re

CLASSIFICATIONS = ("supported", "partially_supported", "not_supported", "inconclusive", "not_executed")
FAILURE_TYPES = ("failed_implementation", "failed_prediction", "insufficient_evidence",
                 "invalid_transfer_principle", "dataset_or_test_limitation", "none")
NEXT_ACTIONS = ("refine_mechanism", "modify_assumption", "collect_evidence",
                "test_competing_hypothesis", "change_method", "stop")
PROBLEM_STRUCTURES = ("search_and_prioritization", "causal", "anomaly", "optimization",
                      "resource_allocation", "experimental_design", "pattern_recognition", "other")
CONFIDENCE_LEVELS = ("low", "medium", "high")
DOMAINS = ("mathematics", "physics", "chemistry")
SELECTED_DOMAINS = ("mathematics", "physics", "chemistry", "none")
# Judge v2 (decision-compiler) fields. The live pipeline Judge schema is
# the UNION of the v1 fields and these three fields: a pipeline-valid
# Judge output carries all 15 fields. Compatibility (see
# _validate_judge): the compiler consumes 7 fields and ignores the
# other 8 v1 fields, so every pipeline-valid Judge output is
# compiler-valid for the 7 shared fields. The converse is not
# guaranteed: the pipeline validator is stricter in two places
# (principle must be empty when domain is none; all v1 fields must be
# present), while the compiler tolerates a non-empty principle for
# none and does not require the v1 fields.
JUDGE_METHODS = ("exact_pair_mean", "drug_mean", "pair_frequency", "none")
JUDGE_DATA_FIELDS = ("item_id", "nsc1", "nsc2", "cellname", "grid_n",
                     "observed_score", "per_point_differences",
                     "conc1_values", "conc2_values")
VALIDATOR_VERDICTS = ("keep", "revise", "reject")
FIT_LEVELS = ("low", "medium", "high")
REPORTER_LABELS = ("user_provided", "retrieved", "model_generated_hypothesis", "executed_result")
_ENTRY_ID_RE = re.compile(r"^E\d{4}$")
_NUMERIC_STRING_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


def _no_duplicates(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate key: %s" % key)
        obj[key] = value
    return obj


def parse_json_strict(text: str):
    if not isinstance(text, str):
        raise ValueError("role output must be JSON text")
    parsed = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    _check_finite(parsed, "output")
    return parsed


def _reject_constant(value):
    raise ValueError("non-finite number: %s" % value)


def _check_finite(obj, path):
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        if not math.isfinite(obj):
            raise ValueError("%s must be finite" % path)
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            _check_finite(value, "%s.%s" % (path, key))
        return
    if isinstance(obj, list):
        for i, value in enumerate(obj):
            _check_finite(value, "%s[%d]" % (path, i))


def _is_str(value):
    return isinstance(value, str) and value.strip() != ""


def _is_str_list(value):
    return isinstance(value, list) and all(isinstance(a, str) for a in value)


def _check_object(parsed, required, path="output"):
    errors = []
    if not isinstance(parsed, dict):
        return ["%s must be a JSON object" % path]
    for key in required:
        if key not in parsed:
            errors.append("%s.%s is required" % (path, key))
    for key in parsed:
        if key not in required:
            errors.append("%s.%s is not an allowed field" % (path, key))
    return errors


def _score(value, path, errors):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= 1:
        errors.append("%s must be a finite number in [0, 1]" % path)


def _validate_problem_structurer(parsed):
    required = ("objective", "inputs", "available_evidence", "desired_output", "constraints",
                "controllable_variables", "observable_outcomes", "unknowns", "data_requirements",
                "success_definition", "failure_definition", "problem_structure", "assumptions", "clarification")
    errors = _check_object(parsed, required)
    if errors:
        return errors
    for key in ("objective", "desired_output", "success_definition", "failure_definition"):
        if not _is_str(parsed[key]):
            errors.append("output.%s must be a non-empty string" % key)
    for key in ("inputs", "available_evidence", "constraints", "controllable_variables",
                "observable_outcomes", "unknowns", "data_requirements", "assumptions"):
        if not _is_str_list(parsed[key]):
            errors.append("output.%s must be a list of strings" % key)
    if parsed["problem_structure"] not in PROBLEM_STRUCTURES:
        errors.append("output.problem_structure must be one of %s" % (", ".join(PROBLEM_STRUCTURES)))
    clarification = parsed["clarification"]
    if clarification is not None and not _is_str(clarification):
        errors.append("output.clarification must be null or a non-empty string")
    return errors


def _expert_fields(parsed, extra):
    required = ("domain", "principle", "original_problem_solved", "mapping_to_biology", "mechanism",
                "required_assumptions", "testable_prediction", "falsifier", "minimum_experiment",
                "baseline", "required_data", "risks_of_misleading", "confidence_in_transfer") + extra
    errors = _check_object(parsed, required)
    if errors:
        return errors
    for key in ("principle", "original_problem_solved", "mapping_to_biology", "mechanism",
                "testable_prediction", "falsifier", "minimum_experiment", "baseline"):
        if not _is_str(parsed[key]):
            errors.append("output.%s must be a non-empty string" % key)
    for key in ("required_assumptions", "required_data", "risks_of_misleading"):
        if not _is_str_list(parsed[key]):
            errors.append("output.%s must be a list of strings" % key)
    if parsed["confidence_in_transfer"] not in CONFIDENCE_LEVELS:
        errors.append("output.confidence_in_transfer must be one of %s" % (", ".join(CONFIDENCE_LEVELS)))
    return errors


def _validate_mathematician(parsed):
    errors = _expert_fields(parsed, ())
    if not errors and parsed.get("domain") != "mathematics":
        errors.append("output.domain must be mathematics")
    return errors


def _validate_physicist(parsed):
    errors = _expert_fields(parsed, ("state_variables", "interaction_or_feedback"))
    if not errors and parsed.get("domain") != "physics":
        errors.append("output.domain must be physics")
    if not errors:
        if not _is_str_list(parsed["state_variables"]):
            errors.append("output.state_variables must be a list of strings")
        if not _is_str(parsed["interaction_or_feedback"]):
            errors.append("output.interaction_or_feedback must be a non-empty string")
    return errors


def _validate_chemist(parsed):
    errors = _expert_fields(parsed, ("structural_features_needed",))
    if not errors and parsed.get("domain") != "chemistry":
        errors.append("output.domain must be chemistry")
    if not errors and not _is_str_list(parsed["structural_features_needed"]):
        errors.append("output.structural_features_needed must be a list of strings")
    return errors


def _validate_validator(parsed):
    errors = _check_object(parsed, ("reviews",))
    if errors:
        return errors
    reviews = parsed["reviews"]
    if not isinstance(reviews, list):
        return ["output.reviews must be a list"]
    for i, review in enumerate(reviews):
        path = "output.reviews[%d]" % i
        errors.extend(_check_object(review, ("domain", "biological_fit", "missing_biological_context",
                                             "confounders", "assay_or_dataset_limitations", "required_controls",
                                             "falsification_test", "verdict", "reason"), path))
        if isinstance(review, dict):
            if review.get("domain") not in DOMAINS:
                errors.append("%s.domain must be one of %s" % (path, ", ".join(DOMAINS)))
            if review.get("biological_fit") not in FIT_LEVELS:
                errors.append("%s.biological_fit must be one of %s" % (path, ", ".join(FIT_LEVELS)))
            for key in ("missing_biological_context", "confounders", "assay_or_dataset_limitations", "required_controls"):
                if key in review and not _is_str_list(review[key]):
                    errors.append("%s.%s must be a list of strings" % (path, key))
            if review.get("verdict") not in VALIDATOR_VERDICTS:
                errors.append("%s.verdict must be one of %s" % (path, ", ".join(VALIDATOR_VERDICTS)))
            for key in ("falsification_test", "reason"):
                if key in review and not _is_str(review[key]):
                    errors.append("%s.%s must be a non-empty string" % (path, key))
    return errors


def _validate_judge(parsed):
    """Validate a Transfer Judge output (v1 fields + v2 fields, union).

    Required: the 12 v1 fields plus selected_method, method_parameters,
    and required_data_fields (15 fields, exact set — unknown fields
    are still rejected). The v2 checks mirror the decision compiler's
    rules so a pipeline-valid Judge output is compiler-valid for the
    7 shared fields (selected_domain, selected_principle,
    selected_method, method_parameters, prediction, falsifier,
    required_data_fields). Known asymmetry, kept deliberately: the
    pipeline requires selected_principle to be empty when the domain
    is none, while the compiler only requires it to be a string.
    """
    required = ("candidate_scores", "selected_domain", "selected_principle", "biological_hypothesis",
                "mechanism", "prediction", "falsifier", "minimum_experiment", "fair_baselines",
                "required_tools", "rejected_candidates", "missing_capability",
                "selected_method", "method_parameters", "required_data_fields")
    errors = _check_object(parsed, required)
    if errors:
        return errors
    # --- v2 decision-compiler fields (mirror the compiler's rules) ---
    method = parsed["selected_method"]
    if method not in JUDGE_METHODS:
        errors.append("output.selected_method must be one of %s" % (", ".join(JUDGE_METHODS)))
    if parsed["selected_domain"] == "none" and method != "none":
        errors.append("output.selected_domain none requires output.selected_method none")
    if parsed["selected_domain"] != "none" and method == "none":
        errors.append("output.selected domain requires a real output.selected_method")
    params = parsed["method_parameters"]
    if not isinstance(params, dict):
        errors.append("output.method_parameters must be an object")
    elif method == "exact_pair_mean":
        for key in params:
            if key != "min_pair_items":
                errors.append("output.method_parameters.%s is not an allowed parameter" % key)
        if "min_pair_items" in params:
            value = params["min_pair_items"]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                errors.append("output.method_parameters.min_pair_items must be an integer >= 1")
    elif method in ("drug_mean", "pair_frequency"):
        if params:
            errors.append("output.method_parameters: %s takes no parameters" % method)
    # Method "none": parameters are accepted and dropped by the compiler
    # (known nuance, unchanged); the pipeline mirrors the compiler here.
    fields = parsed["required_data_fields"]
    if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
        errors.append("output.required_data_fields must be a list of strings")
    else:
        for field in fields:
            if field not in JUDGE_DATA_FIELDS:
                errors.append("output.required_data_fields: unknown field %r" % field)
    scores = parsed["candidate_scores"]
    if not isinstance(scores, list):
        errors.append("output.candidate_scores must be a list")
    else:
        for i, item in enumerate(scores):
            path = "output.candidate_scores[%d]" % i
            errors.extend(_check_object(item, ("domain", "structural_fit", "mechanism_quality", "testability",
                                               "data_availability", "feasibility", "misleading_risk", "score_explanation"), path))
            if isinstance(item, dict):
                if item.get("domain") not in DOMAINS:
                    errors.append("%s.domain must be one of %s" % (path, ", ".join(DOMAINS)))
                for key in ("structural_fit", "mechanism_quality", "testability", "data_availability", "feasibility", "misleading_risk"):
                    if key in item:
                        _score(item[key], "%s.%s" % (path, key), errors)
                if "score_explanation" in item and not _is_str(item["score_explanation"]):
                    errors.append("%s.score_explanation must be a non-empty string" % path)
    if parsed["selected_domain"] not in SELECTED_DOMAINS:
        errors.append("output.selected_domain must be one of %s" % (", ".join(SELECTED_DOMAINS)))
    if parsed["selected_domain"] == "none":
        if parsed["selected_principle"] not in ("", None):
            errors.append("output.selected_principle must be empty when selected_domain is none")
    else:
        for key in ("selected_principle", "biological_hypothesis", "mechanism", "prediction", "falsifier", "minimum_experiment"):
            if not _is_str(parsed[key]):
                errors.append("output.%s must be a non-empty string" % key)
    if not _is_str_list(parsed["fair_baselines"]):
        errors.append("output.fair_baselines must be a list of strings")
    if not _is_str_list(parsed["required_tools"]):
        errors.append("output.required_tools must be a list of strings")
    rejected = parsed["rejected_candidates"]
    if not isinstance(rejected, list):
        errors.append("output.rejected_candidates must be a list")
    else:
        for i, item in enumerate(rejected):
            path = "output.rejected_candidates[%d]" % i
            errors.extend(_check_object(item, ("domain", "reason"), path))
            if isinstance(item, dict):
                if item.get("domain") not in DOMAINS:
                    errors.append("%s.domain must be one of %s" % (path, ", ".join(DOMAINS)))
                if "reason" in item and not _is_str(item["reason"]):
                    errors.append("%s.reason must be a non-empty string" % path)
    missing = parsed["missing_capability"]
    if missing is not None and not _is_str(missing):
        errors.append("output.missing_capability must be null or a non-empty string")
    return errors


def _validate_critic(parsed):
    required = ("classification", "failure_type", "evidence", "validity_conditions", "contradictory_evidence",
                "next_action", "next_action_rationale", "requested_calls", "unresolved_question")
    errors = _check_object(parsed, required)
    if errors:
        return errors
    if parsed["classification"] not in CLASSIFICATIONS:
        errors.append("output.classification must be one of %s" % (", ".join(CLASSIFICATIONS)))
    if parsed["failure_type"] not in FAILURE_TYPES:
        errors.append("output.failure_type must be one of %s" % (", ".join(FAILURE_TYPES)))
    evidence = parsed["evidence"]
    if not isinstance(evidence, list):
        errors.append("output.evidence must be a list")
    else:
        for i, item in enumerate(evidence):
            path = "output.evidence[%d]" % i
            errors.extend(_check_object(item, ("claim", "ledger_entry", "value"), path))
            if isinstance(item, dict):
                if "claim" in item and not _is_str(item["claim"]):
                    errors.append("%s.claim must be a non-empty string" % path)
                if "ledger_entry" in item and not (isinstance(item["ledger_entry"], str) and _ENTRY_ID_RE.match(item["ledger_entry"])):
                    errors.append("%s.ledger_entry must be like E0001" % path)
                if "value" in item:
                    value = item["value"]
                    if isinstance(value, bool) or (isinstance(value, (int, float)) and not math.isfinite(value)):
                        errors.append("%s.value must be finite if numeric" % path)
                    elif isinstance(value, str) and _NUMERIC_STRING_RE.match(value.strip()):
                        # Confirmed rule: a number hidden as a string to bypass
                        # auditing is rejected. Numeric evidence must be numeric.
                        errors.append("%s.value must not hide a number as a string" % path)
                    elif not isinstance(value, (str, int, float)):
                        errors.append("%s.value must be a string or a number" % path)
    if not _is_str(parsed["validity_conditions"]):
        errors.append("output.validity_conditions must be a non-empty string")
    if not _is_str_list(parsed["contradictory_evidence"]):
        errors.append("output.contradictory_evidence must be a list of strings")
    if parsed["next_action"] not in NEXT_ACTIONS:
        errors.append("output.next_action must be one of %s" % (", ".join(NEXT_ACTIONS)))
    for key in ("next_action_rationale", "unresolved_question"):
        if not _is_str(parsed[key]):
            errors.append("output.%s must be a non-empty string" % key)
    requested = parsed["requested_calls"]
    if not isinstance(requested, list):
        errors.append("output.requested_calls must be a list")
    else:
        for i, item in enumerate(requested):
            path = "output.requested_calls[%d]" % i
            errors.extend(_check_object(item, ("tool", "arguments"), path))
            if isinstance(item, dict) and "arguments" in item and not isinstance(item["arguments"], dict):
                errors.append("%s.arguments must be an object" % path)
    return errors


def _validate_reporter(parsed):
    required = ("summary", "what_we_tested", "what_happened", "evidence_level", "limits",
                "most_important_unresolved_question", "recommended_next_action", "claims")
    errors = _check_object(parsed, required)
    if errors:
        return errors
    for key in ("summary", "what_we_tested", "what_happened", "evidence_level",
                "most_important_unresolved_question", "recommended_next_action"):
        if not _is_str(parsed[key]):
            errors.append("output.%s must be a non-empty string" % key)
    if not _is_str_list(parsed["limits"]):
        errors.append("output.limits must be a list of strings")
    claims = parsed["claims"]
    if not isinstance(claims, list):
        errors.append("output.claims must be a list")
    else:
        for i, claim in enumerate(claims):
            path = "output.claims[%d]" % i
            errors.extend(_check_object(claim, ("text", "label", "ledger_entries"), path))
            if isinstance(claim, dict):
                if "text" in claim and not _is_str(claim["text"]):
                    errors.append("%s.text must be a non-empty string" % path)
                if "label" in claim and claim["label"] not in REPORTER_LABELS:
                    errors.append("%s.label must be one of %s" % (path, ", ".join(REPORTER_LABELS)))
                entries = claim.get("ledger_entries")
                if entries is not None and (not isinstance(entries, list) or not all(isinstance(e, str) and _ENTRY_ID_RE.match(e) for e in entries)):
                    errors.append("%s.ledger_entries must be a list of ledger entry ids like E0001" % path)
    return errors


_VALIDATORS = {
    "problem_structurer": _validate_problem_structurer,
    "mathematician": _validate_mathematician,
    "physicist": _validate_physicist,
    "chemist": _validate_chemist,
    "biology_validator": _validate_validator,
    "transfer_judge": _validate_judge,
    "critic": _validate_critic,
    "reporter": _validate_reporter,
}

ROLES = tuple(_VALIDATORS.keys())


def validate_role_output(role: str, raw):
    validator = _VALIDATORS.get(role)
    if validator is None:
        return False, None, ["unknown role: %s" % role]
    if isinstance(raw, str):
        try:
            parsed = parse_json_strict(raw)
        except ValueError as exc:
            return False, None, ["invalid JSON: %s" % exc]
    elif isinstance(raw, dict):
        try:
            _check_finite(raw, "output")
        except ValueError as exc:
            return False, None, [str(exc)]
        parsed = raw
    else:
        return False, None, ["role output must be JSON text or an object"]
    errors = validator(parsed)
    if errors:
        return False, None, errors
    return True, parsed, []


def validate_tool_call(call):
    if not isinstance(call, dict):
        return "tool_call must be an object"
    call_id = call.get("id")
    if not isinstance(call_id, str) or not call_id:
        return "tool_call.id must be a non-empty string"
    function = call.get("function")
    if not isinstance(function, dict):
        return "tool_call.function must be an object"
    if not isinstance(function.get("name"), str) or not function.get("name"):
        return "tool_call.function.name must be a non-empty string"
    arguments = function.get("arguments")
    if not isinstance(arguments, (str, dict)):
        return "tool_call.function.arguments must be JSON text or an object"
    if isinstance(arguments, str):
        try:
            parse_json_strict(arguments)
        except ValueError as exc:
            return "tool_call arguments are not valid JSON: %s" % exc
    return None


def validate_sealed_plan(plan) -> list:
    """Validate the single sealed-plan schema. Returns a list of errors."""
    required = ("dataset_hash", "split", "seed", "budget", "budget_count", "budget_fraction",
                "metric", "baseline", "confidence", "minimum_effect",
                "selected_domain", "selected_hypothesis", "prediction", "falsifier")
    errors = _check_object(plan, required, "sealed_plan")
    if errors:
        return errors
    if not _is_str(plan["dataset_hash"]):
        errors.append("sealed_plan.dataset_hash must be a non-empty string")
    split = plan["split"]
    if not isinstance(split, dict):
        errors.append("sealed_plan.split must be an object")
    else:
        split_required = ("train_ids", "hidden_ids", "start_set_ids", "train_ids_hash", "hidden_ids_hash",
                          "start_set_ids_hash", "split_seed", "split_method", "dataset_hash")
        errors.extend(_check_object(split, split_required, "sealed_plan.split"))
        for key in ("train_ids", "hidden_ids", "start_set_ids"):
            if key in split and not isinstance(split[key], list):
                errors.append("sealed_plan.split.%s must be a list" % key)
        for key in ("train_ids_hash", "hidden_ids_hash", "start_set_ids_hash", "split_method", "dataset_hash"):
            if key in split and not _is_str(split[key]):
                errors.append("sealed_plan.split.%s must be a non-empty string" % key)
        if "split_seed" in split and (not isinstance(split["split_seed"], int) or isinstance(split["split_seed"], bool)):
            errors.append("sealed_plan.split.split_seed must be an integer")
        if "dataset_hash" in split and split["dataset_hash"] != plan["dataset_hash"]:
            errors.append("sealed_plan.split.dataset_hash must match sealed_plan.dataset_hash")
    if not isinstance(plan["seed"], int) or isinstance(plan["seed"], bool):
        errors.append("sealed_plan.seed must be an integer")
    if not isinstance(plan["budget"], int) or isinstance(plan["budget"], bool) or plan["budget"] < 0:
        errors.append("sealed_plan.budget must be an integer >= 0")
    if not isinstance(plan["budget_count"], int) or isinstance(plan["budget_count"], bool) or plan["budget_count"] < 0:
        errors.append("sealed_plan.budget_count must be an integer >= 0")
    if plan["budget"] != plan["budget_count"]:
        errors.append("sealed_plan.budget must equal budget_count")
    fraction = plan["budget_fraction"]
    if fraction is not None and (not isinstance(fraction, (int, float)) or isinstance(fraction, bool) or not math.isfinite(fraction) or not 0 <= fraction <= 1):
        errors.append("sealed_plan.budget_fraction must be null or a finite number in [0, 1]")
    for key in ("metric", "baseline"):
        if not _is_str(plan[key]):
            errors.append("sealed_plan.%s must be a non-empty string" % key)
    confidence = plan["confidence"]
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not math.isfinite(confidence) or not 0 < confidence < 1:
        errors.append("sealed_plan.confidence must be a number in (0, 1)")
    minimum_effect = plan["minimum_effect"]
    if not isinstance(minimum_effect, (int, float)) or isinstance(minimum_effect, bool) or not math.isfinite(minimum_effect):
        errors.append("sealed_plan.minimum_effect must be a finite number")
    if plan["selected_domain"] not in SELECTED_DOMAINS:
        errors.append("sealed_plan.selected_domain must be one of %s" % (", ".join(SELECTED_DOMAINS)))
    if plan["selected_domain"] == "none":
        errors.append("sealed_plan.selected_domain must not be none for an execution seal")
    else:
        for key in ("selected_hypothesis", "prediction", "falsifier"):
            if not _is_str(plan[key]):
                errors.append("sealed_plan.%s must be a non-empty string" % key)
    return errors


def validate_dataset_manifest(manifest) -> list:
    if not isinstance(manifest, dict):
        return ["dataset manifest must be an object"]
    errors = []
    for key in ("dataset_id", "molecular_structure_available", "structure_source", "structure_field", "structure_file_hash"):
        if key not in manifest:
            errors.append("manifest.%s is required" % key)
    if "dataset_id" in manifest and not _is_str(manifest["dataset_id"]):
        errors.append("manifest.dataset_id must be a non-empty string")
    if "molecular_structure_available" in manifest and not isinstance(manifest["molecular_structure_available"], bool):
        errors.append("manifest.molecular_structure_available must be a boolean")
    if manifest.get("molecular_structure_available") is True:
        if manifest.get("structure_source") is None and manifest.get("structure_field") is None:
            errors.append("manifest must name a structure source or field when molecular_structure_available is true")
    return errors
