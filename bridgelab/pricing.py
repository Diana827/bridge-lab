"""Model price configuration and cost calculation (pricing v1).

Prices live ONLY in a separate JSON config file (bridgelab/model_prices.json
by default) so that no price is ever hard-coded into execution code.
A price is recorded only from an official provider source, together
with its version and source; the config's version/source fields are
what the run manifest cites. Nothing here estimates a price: when no
official price exists for a model, or when usage is incomplete, the
cost is the string "MISSING" — never an invented number.

Config shape:
  {"version": <str>, "source": <str>, "currency": <str>,
   "models": {<model_id>: {"price_per_prompt_token": <number>,
                           "price_per_completion_token": <number>}}}
Either per-token price may be absent; a model entry with neither
price, or a model absent from the config, yields MISSING.
"""
from __future__ import annotations

import json

MISSING = "MISSING"


def load_prices(path):
    """Load a price config file. Returns {"version", "source", "currency", "models"}.

    Raises FileNotFoundError when the file does not exist and
    ValueError when its shape is invalid. Callers that must run
    without a config pass path=None and get an empty config (all
    costs MISSING) instead of an error."""
    if path is None:
        return {"version": None, "source": None, "currency": None, "models": {}}
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict) or not isinstance(raw.get("models", {}), dict):
        raise ValueError("price config must be an object with a models object")
    models = {}
    for model_id, entry in raw["models"].items():
        if not isinstance(entry, dict):
            raise ValueError("price config model entry must be an object")
        clean = {}
        for key in ("price_per_prompt_token", "price_per_completion_token", "price_per_token"):
            value = entry.get(key)
            if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)):
                raise ValueError("price config price must be a number")
            if value is not None:
                clean[key] = value
        models[model_id] = clean
    return {"version": raw.get("version"), "source": raw.get("source"),
            "currency": raw.get("currency"), "models": models}


def compute_cost(prompt_tokens, completion_tokens, model, prices):
    """Cost for one usage total, or MISSING.

    Prefers separate prompt/completion prices when both are present;
    falls back to a single price_per_token applied to the token sum.
    Any missing input (non-integer tokens, unknown model, no usable
    price) yields MISSING, never an estimate."""
    if (isinstance(prompt_tokens, bool) or not isinstance(prompt_tokens, int)
            or isinstance(completion_tokens, bool) or not isinstance(completion_tokens, int)):
        return MISSING
    entry = (prices or {}).get("models", {}).get(model)
    if not isinstance(entry, dict):
        return MISSING
    pp = entry.get("price_per_prompt_token")
    pc = entry.get("price_per_completion_token")
    if isinstance(pp, (int, float)) and isinstance(pc, (int, float)):
        return round(prompt_tokens * pp + completion_tokens * pc, 6)
    single = entry.get("price_per_token")
    if isinstance(single, (int, float)):
        return round((prompt_tokens + completion_tokens) * single, 6)
    return MISSING


def cost_from_usage_summary(usage, model, prices):
    """Cost for a driver usage summary (aggregate_usage output), or MISSING."""
    if not isinstance(usage, dict):
        return MISSING
    return compute_cost(usage.get("prompt_tokens"), usage.get("completion_tokens"),
                        model, prices)
