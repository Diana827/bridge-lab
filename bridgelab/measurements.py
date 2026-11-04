"""One measurement shape for every model call, fake or live.

Every logical call is measured on the same eight metrics the user
asked for:

    calls            total API attempts (sum of attempts)
    prompt_tokens    sum, or None when any counted call lacks it
    completion_tokens  sum, or None when any counted call lacks it
    total_tokens     sum, or None when any counted call lacks it
    latency_ms       summed wall-clock latency of the attempts
    retries          sum of (attempts - 1): extra attempts after the first
    cost             computed from tokens and a documented price, or
                     "MISSING" — never estimated. With no official price
                     on file (the current state), cost is always
                     "MISSING"; the token totals are recorded so the cost
                     becomes computable the moment a price is documented.
    timeout_s        the configured per-call timeout (a single value
                     when uniform across calls, else the list of values)
    timeout_occurred whether any attempt ended in a timeout

A single call measurement is built with measurement_from_call().
aggregate() totals a list of them. from_ledger_entries() rebuilds an
aggregate from model_call ledger entries (best effort: ledger entries
do not record the configured timeout, so timeout_s is None there and
retries per logical call are the max retry_count within its
role/label group).
"""
from __future__ import annotations

from .pricing import cost_from_usage_summary

MISSING = "MISSING"

MEASUREMENT_FIELDS = ("calls", "prompt_tokens", "completion_tokens",
                      "total_tokens", "latency_ms", "retries", "cost",
                      "timeout_s", "timeout_occurred")


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def measurement_from_call(attempts, latency_ms, usage, cost=MISSING,
                          timeout_s=None, timeout_occurred=False,
                          provenance=None):
    """Build one logical call's measurement.

    attempts: number of API attempts for this logical call (>= 1).
    usage: a dict with prompt_tokens/completion_tokens/total_tokens
    (ints), or MISSING/None when unknown. cost: a number or MISSING.
    """
    record = {"attempts": attempts, "latency_ms": latency_ms,
              "timeout_s": timeout_s, "timeout_occurred": bool(timeout_occurred)}
    if provenance is not None:
        record["provenance"] = provenance
    if isinstance(usage, dict):
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            record[key] = usage.get(key) if _is_int(usage.get(key)) else None
    else:
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            record[key] = None
    record["cost"] = cost if isinstance(cost, (int, float)) else MISSING
    return record


def aggregate(measurements, prices=None, model=None):
    """Total a list of call measurements into the eight metrics."""
    calls = 0
    retries = 0
    latency_ms = 0.0
    token_sums = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    token_complete = {"prompt_tokens": True, "completion_tokens": True, "total_tokens": True}
    timeouts = []
    timeout_occurred = False
    for record in measurements:
        attempts = record.get("attempts", 1)
        calls += attempts
        retries += max(0, attempts - 1)
        latency_ms += record.get("latency_ms") or 0.0
        for key in token_sums:
            value = record.get(key)
            if _is_int(value):
                token_sums[key] += value
            else:
                token_complete[key] = False
        timeouts.append(record.get("timeout_s"))
        timeout_occurred = timeout_occurred or bool(record.get("timeout_occurred"))
    tokens = {key: (token_sums[key] if token_complete[key] else None)
              for key in token_sums}
    if prices is not None and model is not None:
        cost = cost_from_usage_summary(tokens, model, prices)
    else:
        cost = MISSING
    unique_timeouts = []
    for value in timeouts:
        if value not in unique_timeouts:
            unique_timeouts.append(value)
    timeout_s = unique_timeouts[0] if len(unique_timeouts) == 1 else unique_timeouts
    return {"calls": calls,
            "prompt_tokens": tokens["prompt_tokens"],
            "completion_tokens": tokens["completion_tokens"],
            "total_tokens": tokens["total_tokens"],
            "latency_ms": round(latency_ms, 3),
            "retries": retries,
            "cost": cost,
            "timeout_s": timeout_s,
            "timeout_occurred": timeout_occurred}


def from_ledger_entries(entries):
    """Rebuild an aggregate from model_call ledger entries.

    Retries per logical call are the max retry_count within its
    role/label group (run_role records retry_count per attempt, so a
    3-attempt call contributes entries with retry_count 0, 1, 2 and
    2 retries). Ledger entries do not record the configured timeout,
    so timeout_s is None here; use call measurements for timeout_s.
    """
    measurements = []
    groups = {}
    for entry in entries:
        if entry.get("kind") != "model_call":
            continue
        data = entry.get("data", {})
        key = data.get("role") or data.get("label") or "model_call"
        groups.setdefault(key, []).append((entry.get("provenance"), data))
    for key, pairs in groups.items():
        rows = [data for _, data in pairs]
        provenance = pairs[-1][0]
        attempts = max([row.get("retry_count", 0) for row in rows]) + 1
        latency = sum(row.get("latency_ms") or 0.0 for row in rows)
        # Token totals across attempts in a group cannot be summed from
        # a single trailing usage object, so per-group usage is taken
        # from the sum of each attempt's usage when every attempt has
        # complete integer usage; otherwise the group contributes None.
        summed = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        complete = True
        for row in rows:
            row_usage = row.get("usage")
            if not isinstance(row_usage, dict):
                complete = False
                break
            for token_key in summed:
                if _is_int(row_usage.get(token_key)):
                    summed[token_key] += row_usage[token_key]
                else:
                    complete = False
        measurements.append(measurement_from_call(
            attempts, latency, summed if complete else None,
            cost=MISSING, timeout_s=None,
            timeout_occurred=any("timeout" in str(row.get("error_type", "")).lower()
                                 for row in rows),
            provenance=provenance))
    result = aggregate(measurements)
    result["timeout_s"] = None
    result["timeout_note"] = ("Ledger entries do not record the configured "
                              "timeout; use call measurements for timeout_s.")
    return result
