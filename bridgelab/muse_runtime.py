"""Live-model execution bookkeeping for BRIDGE LAB (no dataset access).

This module is the ONLY place where a live Muse call is wrapped for
execution bookkeeping. It takes a client, a ledger, and messages — never
a dataset, a candidate file, or a hidden label — so by construction it
cannot read hidden labels.

For every call it records:
  - latency_ms (wall clock around the call, monotonic clock);
  - model verification: the response's "model" field must equal
    EXPECTED_MODEL ("muse-spark-1.3"). A missing field is
    "model_id_missing"; a different value is "model_id_mismatch".
    Either is a failure: the result is never used.
  - usage: the response's "usage" object when it is a dict (integer
    fields preserved, missing fields recorded as "MISSING"); otherwise
    the whole usage is the string "MISSING". Usage is never estimated
    from text length.
  - cost: "MISSING" unless the caller supplies a documented price and
    total_tokens is an integer; then cost = total_tokens * price.
    Cost is never estimated from an undocumented price.
  - failures: MuseError, HTTP/network errors, and any other exception
    from client.chat are caught, ledger-recorded as a "note" entry
    (kind "note", provenance "executed"), and returned as a failure
    result — never raised further, never retried here. Retrying a
    failed call is a new run index under the frozen failure rules.

Selection validation (validate_selection) implements the frozen
failure-handling rules for a returned selection, purely on ids:
  - empty selection -> {"status": "none"} (valid zero-hit result);
  - unknown ids -> {"status": "invalid"} (no scoring);
  - duplicates -> deduplicated, flagged;
  - over budget -> truncated in selection order, flagged.
It never interprets what an id means and never touches a label.
"""
from __future__ import annotations
import time

from .clients import provenance_of

EXPECTED_MODEL = "muse-spark-1.3"
MISSING = "MISSING"


def _usage_of(resp):
    usage = resp.get("usage") if isinstance(resp, dict) else None
    if not isinstance(usage, dict):
        return MISSING
    out = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        out[key] = value if isinstance(value, int) and not isinstance(value, bool) else MISSING
    return out


def _cost_of(usage, price_per_token):
    if price_per_token is None:
        return MISSING
    if isinstance(usage, dict) and isinstance(usage.get("total_tokens"), int):
        return usage["total_tokens"] * price_per_token
    return MISSING


def run_model_call(client, ledger, messages, tools=None, label="model_call",
                   price_per_token=None):
    """Call client.chat once with full bookkeeping. Never raises for an
    API failure; failures are ledger-recorded and returned."""
    start = time.monotonic()
    try:
        if tools is None:
            resp = client.chat(messages)
        else:
            resp = client.chat(messages, tools)
    except Exception as exc:  # noqa: BLE001 - every client failure must be ledgered, not crash
        latency_ms = round((time.monotonic() - start) * 1000, 3)
        ledger.append("note", "executed", {
            "label": label, "error": "model_call_failed",
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:200], "latency_ms": latency_ms})
        return {"status": "failed", "error": "model_call_failed",
                "error_type": type(exc).__name__, "latency_ms": latency_ms,
                "model": getattr(client, "model", ""), "usage": MISSING,
                "cost": MISSING}
    latency_ms = round((time.monotonic() - start) * 1000, 3)
    model = resp.get("model") if isinstance(resp, dict) else None
    if model is None:
        ledger.append("note", "executed", {
            "label": label, "error": "model_id_missing",
            "expected_model": EXPECTED_MODEL, "latency_ms": latency_ms})
        return {"status": "failed", "error": "model_id_missing",
                "latency_ms": latency_ms, "model": MISSING,
                "usage": MISSING, "cost": MISSING}
    if model != EXPECTED_MODEL:
        ledger.append("note", "executed", {
            "label": label, "error": "model_id_mismatch",
            "expected_model": EXPECTED_MODEL, "actual_model": model,
            "latency_ms": latency_ms})
        return {"status": "failed", "error": "model_id_mismatch",
                "latency_ms": latency_ms, "model": model,
                "usage": MISSING, "cost": MISSING}
    usage = _usage_of(resp)
    cost = _cost_of(usage, price_per_token)
    ledger.append("model_call", provenance_of(client), {
        "model": model, "label": label, "latency_ms": latency_ms,
        "usage": usage, "cost": cost})
    return {"status": "ok", "response": resp, "model": model,
            "latency_ms": latency_ms, "usage": usage, "cost": cost}


def validate_selection(selected_ids, candidate_ids, budget_count):
    """Validate a returned selection against the frozen failure rules.

    Returns a dict with status "ok" | "none" | "invalid", the ids to
    score (empty for none/invalid), and flags naming every adjustment.
    Unknown ids make the whole selection invalid (no partial scoring);
    duplicates and over-budget selections are adjusted and flagged.
    """
    if not isinstance(selected_ids, list):
        return {"status": "invalid", "ids": [], "flags": ["selection_not_a_list"]}
    candidates = set(candidate_ids)
    unknown = [i for i in selected_ids if i not in candidates]
    if unknown:
        return {"status": "invalid", "ids": [], "flags": ["unknown_item_ids"],
                "unknown": unknown}
    if not selected_ids:
        return {"status": "none", "ids": [], "flags": []}
    flags = []
    seen = []
    for item_id in selected_ids:
        if item_id in seen:
            if "duplicates_removed" not in flags:
                flags.append("duplicates_removed")
            continue
        seen.append(item_id)
    if len(seen) > budget_count:
        flags.append("truncated_to_budget")
        seen = seen[:budget_count]
    return {"status": "ok", "ids": seen, "flags": flags}
