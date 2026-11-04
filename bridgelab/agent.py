"""Tool-calling loop and strict role-output loop. Every model call and every
tool execution is written to the ledger.

Safeguards enforced here:
- 2: tool names and arguments are validated before execution; an
  unregistered tool, a malformed call, or invalid arguments are recorded
  as an error tool_call result, never executed and never a crash.
- 3: maximum total model steps. max_steps must be in [1, MAX_MODEL_STEPS];
  the loop never makes more model calls than max_steps.
- 9: at most two retries for invalid role JSON (run_role only).
- 10: failed tools are recorded as tool_call results in the ledger.

The final text is model output (provenance 'model'); tool results are
'executed'. Python computes every number; the model never does.
"""
from __future__ import annotations
import json
import time
from . import tools
from .clients import provenance_of
from .ledger import Ledger, canon, sha256
from .validation import validate_role_output, validate_tool_call, parse_json_strict

# Default only for direct run_loop/run_role calls without an explicit system
# prompt. The orchestration path always passes the role's own prompt file
# from prompts/, so this default must not name any single role. The old
# "Researcher" default from the pre-9-role design was removed 2026-10-27.
SYSTEM = ("You are a BRIDGE LAB role agent. Return strict JSON when the role requires it. "
          "Use the registered tools for any computation; "
          "never state a number you did not get from a tool result.")

MAX_MODEL_STEPS = 20
MAX_JSON_RETRIES = 2
EXPECTED_MODEL = "muse-spark-1.3"


def _validate_max_steps(max_steps: int) -> int:
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 1 or max_steps > MAX_MODEL_STEPS:
        raise ValueError("max_steps must be an integer in [1, %d]" % MAX_MODEL_STEPS)
    return max_steps


def _record_tool_failure(ledger, call, error):
    """Record a failed tool call as a tool_call result. Returns the entry."""
    if isinstance(call, dict):
        call_id = call.get("id") if isinstance(call.get("id"), str) and call.get("id") else "missing"
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = function.get("name") if isinstance(function.get("name"), str) and function.get("name") else "invalid"
    else:
        call_id, name = "missing", "invalid"
    result = {"status": "error", "error": error}
    return ledger.append("tool_call", "executed", {"call_id": call_id, "tool": name, "arguments": None, "result": result})


def run_loop(client, ledger: Ledger, user_prompt: str, max_steps: int = 4, system: str = SYSTEM) -> dict:
    max_steps = _validate_max_steps(max_steps)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user_prompt}]
    ledger.append("user_input", "user", {"prompt": user_prompt})
    for step in range(max_steps):
        call_start = time.monotonic()
        try:
            resp = client.chat(messages, tools.specs())
        except Exception as exc:  # noqa: BLE001 - API failures must be ledgered, never crash
            ledger.append("note", "executed", {"step": step, "error": "model_call_failed",
                          "error_type": type(exc).__name__, "error_message": str(exc)[:200],
                          "latency_ms": round((time.monotonic() - call_start) * 1000, 3)})
            return {"text": "", "steps": step + 1, "stopped": "error", "error": "model_call_failed",
                    "error_type": type(exc).__name__}
        if not isinstance(resp, dict) or not resp.get("choices") or not isinstance(resp["choices"][0].get("message"), dict):
            ledger.append("note", "executed", {"step": step, "error": "malformed model response"})
            return {"text": "", "steps": step + 1, "stopped": "error", "error": "malformed model response"}
        msg = resp["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not isinstance(calls, list):
            ledger.append("note", "executed", {"step": step, "error": "tool_calls must be a list"})
            calls = []
        ledger.append("model_call", provenance_of(client), {"model": resp.get("model", getattr(client, "model", "")), "step": step,
                      "request_sha256": sha256(canon(messages)), "text": msg.get("content") or "",
                      "tool_calls": [{"id": c.get("id"), "name": (c.get("function") or {}).get("name"), "arguments": (c.get("function") or {}).get("arguments")} for c in calls if isinstance(c, dict)],
                      "usage": resp.get("usage"),
                      "latency_ms": round((time.monotonic() - call_start) * 1000, 3),
                      "cost": "MISSING"})
        messages.append(msg)
        if not calls:
            return {"text": msg.get("content") or "", "steps": step + 1, "stopped": "final"}
        for c in calls:
            call_error = validate_tool_call(c)
            if call_error is not None:
                entry = _record_tool_failure(ledger, c, call_error)
                result = {"status": "error", "error": call_error}
                call_id = entry["data"]["call_id"]
                messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps({"ledger_entry": entry["id"], **result})})
                continue
            name, raw = c["function"]["name"], c["function"]["arguments"]
            try:
                args = parse_json_strict(raw) if isinstance(raw, str) else raw
            except ValueError as exc:
                result = {"status": "error", "error": "arguments are not valid JSON: %s" % exc}
                args = None
            else:
                result = tools.run(name, args)
            e = ledger.append("tool_call", "executed", {"call_id": c["id"], "tool": name, "arguments": args, "result": result})
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps({"ledger_entry": e["id"], **result})})
    return {"text": "", "steps": max_steps, "stopped": "max_steps"}


def run_role(client, ledger: Ledger, role: str, user_prompt: str, max_retries: int = MAX_JSON_RETRIES, system: str = SYSTEM) -> dict:
    """Run one role until its output passes strict JSON validation.

    Safeguard 9: at most two retries. Total attempts = 1 + max_retries, and
    max_retries itself may not exceed 2. After the last attempt the failure
    is recorded in the ledger and returned, never retried again.
    """
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0 or max_retries > MAX_JSON_RETRIES:
        raise ValueError("max_retries must be an integer in [0, %d]" % MAX_JSON_RETRIES)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user_prompt}]
    ledger.append("user_input", "user", {"prompt": user_prompt, "role": role})
    errors = []
    last_entry = None
    for attempt in range(max_retries + 1):
        # Role calls do not offer tools: a role returns JSON only. The input
        # hash is over exactly the messages sent, so a fake client test can
        # recompute it from the messages it captured.
        input_hash = sha256(canon(messages))
        call_start = time.monotonic()
        try:
            try:
                resp = client.chat(messages)
            except TypeError:
                resp = client.chat(messages, tools.specs())
        except Exception as exc:  # noqa: BLE001 - API failures (e.g. MuseError) must be ledgered, never crash
            ledger.append("note", "executed", {"role": role, "error": "model_call_failed",
                          "error_type": type(exc).__name__, "error_message": str(exc)[:200],
                          "attempt": attempt,
                          "latency_ms": round((time.monotonic() - call_start) * 1000, 3)})
            return {"status": "failed", "role": role, "error": "model_call_failed",
                    "error_type": type(exc).__name__, "attempts": attempt + 1,
                    "retry_count": attempt, "model_call_entry": last_entry["id"] if last_entry else None}
        if not isinstance(resp, dict) or resp.get("model") != EXPECTED_MODEL:
            actual_model = resp.get("model") if isinstance(resp, dict) else None
            error = "model_id_missing" if actual_model is None else "model_id_mismatch"
            last_entry = ledger.append("model_call", provenance_of(client), {
                "model": actual_model or "MISSING", "step": attempt, "role": role,
                "text": "", "validation_errors": [error], "input_hash": input_hash,
                "retry_count": attempt,
                "latency_ms": round((time.monotonic() - call_start) * 1000, 3),
                "usage": resp.get("usage") if isinstance(resp, dict) else None,
                "cost": "MISSING"})
            return {"status": "failed", "role": role, "error": error,
                    "attempts": attempt + 1, "retry_count": attempt,
                    "model_call_entry": last_entry["id"]}
        if not isinstance(resp, dict) or not resp.get("choices") or not isinstance(resp["choices"][0].get("message"), dict):
            errors = ["malformed model response"]
            last_entry = ledger.append("model_call", provenance_of(client), {"model": resp.get("model", getattr(client, "model", "")) if isinstance(resp, dict) else "",
                          "step": attempt, "role": role, "text": "", "validation_errors": errors,
                          "input_hash": input_hash, "retry_count": attempt,
                          "latency_ms": round((time.monotonic() - call_start) * 1000, 3),
                          "usage": resp.get("usage") if isinstance(resp, dict) else None,
                          "cost": "MISSING"})
            break
        msg = resp["choices"][0]["message"]
        text = msg.get("content") or ""
        ok, parsed, errors = validate_role_output(role, text)
        last_entry = ledger.append("model_call", provenance_of(client), {"model": resp.get("model", getattr(client, "model", "")), "step": attempt,
                      "role": role, "text": text, "validation_errors": errors,
                      "input_hash": input_hash, "retry_count": attempt,
                      "latency_ms": round((time.monotonic() - call_start) * 1000, 3),
                      "usage": resp.get("usage"), "cost": "MISSING"})
        if ok:
            return {"status": "ok", "role": role, "parsed": parsed, "attempts": attempt + 1,
                    "retry_count": attempt, "model_call_entry": last_entry["id"], "input_hash": input_hash}
        if attempt < max_retries:
            messages.append(msg)
            messages.append({"role": "user", "content": "The previous output was not valid JSON for role %s: %s. Reply again with strict JSON only, using exactly the allowed fields." % (role, "; ".join(errors))})
    ledger.append("note", "executed", {"role": role, "error": "invalid_json_after_retries", "attempts": max_retries + 1, "validation_errors": errors})
    return {"status": "failed", "role": role, "error": "invalid_json_after_retries", "attempts": max_retries + 1,
            "retry_count": max_retries, "model_call_entry": last_entry["id"] if last_entry else None,
            "validation_errors": errors}
