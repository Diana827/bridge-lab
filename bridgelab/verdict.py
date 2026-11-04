"""Verdict ceiling, computed in Python only.

Safeguard 6: a Critic may not claim more than the sealed rule and the
executed numbers allow. The ceiling is computed from a sealed rule and a
Python result, never from model prose.

Claim strength, weakest to strongest:
  not_executed < inconclusive < not_supported < partially_supported < supported
A requested label is kept only if it is no stronger than the ceiling;
otherwise it is lowered to the ceiling and `lowered` is True.

Ceiling rules, for a result with value, ci_low, ci_high and baseline_value:
  - no result, or a failed result: not_executed
  - ci_low - baseline_value >= minimum_effect: supported
  - ci_low > baseline_value: partially_supported
  - ci_high <= baseline_value: not_supported
  - otherwise (the interval overlaps the baseline): inconclusive
"""
from __future__ import annotations
import math

ORDER = {"not_executed": 0, "inconclusive": 1, "not_supported": 2, "partially_supported": 3, "supported": 4}


def _finite_number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("%s must be a finite number" % name)
    return float(value)


def compute_ceiling(sealed_rule: dict, result) -> str:
    if not isinstance(sealed_rule, dict) or "minimum_effect" not in sealed_rule:
        raise ValueError("sealed_rule must contain minimum_effect")
    minimum_effect = _finite_number(sealed_rule["minimum_effect"], "minimum_effect")
    if result is None:
        return "not_executed"
    if isinstance(result, dict) and result.get("status") == "error":
        return "not_executed"
    if not isinstance(result, dict):
        raise ValueError("result must be an object or None")
    output = result.get("output", result)
    if not isinstance(output, dict):
        raise ValueError("result output must be an object")
    for key in ("value", "ci_low", "ci_high", "baseline_value"):
        if key not in output:
            raise ValueError("result output is missing %s" % key)
    value = _finite_number(output["value"], "value")
    ci_low = _finite_number(output["ci_low"], "ci_low")
    ci_high = _finite_number(output["ci_high"], "ci_high")
    baseline = _finite_number(output["baseline_value"], "baseline_value")
    if ci_low > ci_high:
        raise ValueError("ci_low must not exceed ci_high")
    if ci_low - baseline >= minimum_effect:
        return "supported"
    if ci_low > baseline:
        return "partially_supported"
    if ci_high <= baseline:
        return "not_supported"
    return "inconclusive"


def enforce_verdict(requested: str, ceiling: str, ledger=None, evidence_ids=None) -> dict:
    """Return the final verdict, lowering the request to the ceiling if needed.

    If a ledger is given, the decision is written as a verdict entry, so the
    lowering itself is auditable.
    """
    if requested not in ORDER:
        raise ValueError("unknown requested verdict: %s" % requested)
    if ceiling not in ORDER:
        raise ValueError("unknown ceiling verdict: %s" % ceiling)
    final = requested if ORDER[requested] <= ORDER[ceiling] else ceiling
    lowered = final != requested
    outcome = {"requested": requested, "ceiling": ceiling, "final": final, "lowered": lowered,
               "evidence_ids": list(evidence_ids or [])}
    if ledger is not None:
        ledger.append("verdict", "executed", outcome)
    return outcome
