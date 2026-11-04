"""Number-to-ledger auditing, computed in Python only.

Safeguard 7: every number shown as a result must come from an executed
tool_call ledger entry. Numbers from model text, user input, or a missing
entry do not pass. A claim is (value, ledger_entry_id); the entry must exist,
be a tool_call with provenance 'executed', and its tool output must contain
that value (exact match, with a 1e-9 relative tolerance for floats).
"""
from __future__ import annotations
import math, re

_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9_])[-+]?\d+(?:\.\d+)?(?![A-Za-z0-9_])")


def collect_numbers(obj):
    numbers = []
    if isinstance(obj, bool):
        return numbers
    if isinstance(obj, (int, float)):
        if math.isfinite(obj):
            numbers.append(obj)
        return numbers
    if isinstance(obj, dict):
        for value in obj.values():
            numbers.extend(collect_numbers(value))
        return numbers
    if isinstance(obj, list):
        for value in obj:
            numbers.extend(collect_numbers(value))
    return numbers


def entry_numbers(entry: dict):
    if not isinstance(entry, dict):
        return []
    data = entry.get("data", {})
    result = data.get("result", {})
    if isinstance(result, dict) and "output" in result:
        return collect_numbers(result["output"])
    return collect_numbers(result)


def _matches(value, candidates):
    for candidate in candidates:
        if isinstance(value, int) and isinstance(candidate, int) and not isinstance(candidate, bool):
            if value == candidate:
                return True
        elif isinstance(value, (int, float)) and isinstance(candidate, (int, float)):
            if math.isclose(float(value), float(candidate), rel_tol=1e-9, abs_tol=1e-12):
                return True
    return False


def audit_claims(claims, ledger) -> dict:
    """Audit structured claims. claims: iterable of (value, entry_id) or dicts."""
    errors = []
    checked = 0
    for claim in claims:
        if isinstance(claim, dict):
            value, entry_id = claim.get("value"), claim.get("ledger_entry")
        else:
            value, entry_id = claim
        checked += 1
        entry = ledger.get(entry_id) if isinstance(entry_id, str) else None
        if entry is None:
            errors.append("number %r has no ledger entry %r" % (value, entry_id))
            continue
        if entry.get("kind") != "tool_call" or entry.get("provenance") != "executed":
            errors.append("entry %s is not an executed tool_call" % entry_id)
            continue
        if not _matches(value, entry_numbers(entry)):
            errors.append("number %r is not in executed entry %s" % (value, entry_id))
    return {"ok": not errors, "checked": checked, "errors": errors}


def audit_reporter_claims(claims, ledger) -> dict:
    """Audit reporter claims.

    Only claims labelled executed_result are audited as executed numbers:
    every number in their text must appear in an executed tool_call entry
    listed in that claim's ledger_entries. Other labels (user_provided,
    retrieved, model_generated_hypothesis) describe provenance, not executed
    results, so their numbers are not required to be tool outputs.
    """
    errors = []
    checked = 0
    for claim in claims:
        if not isinstance(claim, dict):
            errors.append("claim must be an object")
            continue
        if claim.get("label") != "executed_result":
            continue
        for entry_id in claim.get("ledger_entries", []):
            entry = ledger.get(entry_id) if isinstance(entry_id, str) else None
            if entry is None:
                errors.append("claim cites missing ledger entry %r" % (entry_id,))
            elif entry.get("kind") != "tool_call" or entry.get("provenance") != "executed":
                errors.append("claim cites non-executed entry %s" % entry_id)
        numbers = numbers_in_text(claim.get("text", ""))
        executed = []
        for entry_id in claim.get("ledger_entries", []):
            entry = ledger.get(entry_id) if isinstance(entry_id, str) else None
            if entry is not None:
                executed.extend(entry_numbers(entry))
        for value in numbers:
            checked += 1
            if not _matches(value, executed):
                errors.append("number %r in reporter claim is not in its cited executed entries" % (value,))
    return {"ok": not errors, "checked": checked, "errors": errors}


def numbers_in_text(text: str):
    """Extract plain numbers from text, excluding ledger ids like E0001."""
    if not isinstance(text, str):
        return []
    cleaned = re.sub(r"E\d{4}", " ", text)
    return [float(m) if "." in m else int(m) for m in _NUMBER_RE.findall(cleaned)]


def audit_text_numbers(text: str, ledger) -> dict:
    """Audit every number in text against executed tool_call entries.

    This is strict by design: a report text must contain only numbers that
    come from executed results. Years, counts, or other prose numbers that
    are not tool outputs will fail, which is the intended safeguard.
    """
    numbers = numbers_in_text(text)
    executed = []
    for entry in ledger.entries():
        if entry.get("kind") == "tool_call" and entry.get("provenance") == "executed":
            executed.extend(entry_numbers(entry))
    errors = []
    for value in numbers:
        if not _matches(value, executed):
            errors.append("number %r in text is not in any executed tool_call entry" % (value,))
    return {"ok": not errors, "checked": len(numbers), "errors": errors}
