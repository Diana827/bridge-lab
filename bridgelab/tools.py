"""Tool registry. Muse may call only tools registered here; arguments are validated before execution.

A tool is a name, a description, a JSON-schema subset (type, required, properties with type) and a function.
run() never raises: it returns {"status": "ok"|"error", ...} so the outcome can be written to the ledger as is.
"""
from __future__ import annotations
import math, statistics
from . import compute as _compute

REGISTRY = {}
_TYPES = {"number": (int, float), "string": (str,), "array": (list,), "integer": (int,), "object": (dict,), "boolean": (bool,)}


def tool(name: str, description: str, parameters: dict):
    def deco(fn):
        REGISTRY[name] = {"name": name, "description": description, "parameters": parameters, "fn": fn}
        return fn
    return deco


def specs():
    """Tool definitions in the Chat Completions `tools` format."""
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
            for t in REGISTRY.values()]


def _check(schema: dict, value, path="args"):
    want = schema.get("type")
    if want:
        ok = isinstance(value, _TYPES[want]) and not (want in ("number", "integer") and isinstance(value, bool))
        if not ok:
            return "%s must be %s" % (path, want)
    if want in ("number", "integer") and isinstance(value, (int, float)):
        if not math.isfinite(value):
            return "%s must be finite" % path
    if want == "object":
        if not isinstance(value, dict):
            return "%s must be object" % path
        for k in schema.get("required", []):
            if k not in value:
                return "%s.%s is required" % (path, k)
        allowed = set(schema.get("properties", {}).keys())
        for k in value:
            if k not in allowed:
                return "%s.%s is not a registered argument" % (path, k)
        for k, sub in schema.get("properties", {}).items():
            if k in value:
                err = _check(sub, value[k], path + "." + k)
                if err:
                    return err
    if want == "array" and "items" in schema:
        for i, v in enumerate(value):
            err = _check(schema["items"], v, "%s[%d]" % (path, i))
            if err:
                return err
    return None


TOOLS = REGISTRY


def validate_args(name: str, args):
    """Return an error string if name/args are not valid, else None."""
    if name not in REGISTRY:
        return "not_registered: %s" % name
    if not isinstance(args, dict):
        return "args must be object"
    return _check(REGISTRY[name]["parameters"], args)


def run(name: str, args) -> dict:
    # Safeguard 2 and 10: only a registered tool, with validated arguments, is
    # ever executed. Every other case is returned as an error result, never raised.
    if not isinstance(name, str) or not name:
        return {"status": "error", "error": "invalid_tool_name"}
    t = REGISTRY.get(name)
    if t is None:
        return {"status": "error", "error": "not_registered: %s" % name}
    if not isinstance(args, dict):
        return {"status": "error", "error": "invalid_arguments: args must be object"}
    err = _check(t["parameters"], args)
    if err:
        return {"status": "error", "error": "invalid_arguments: " + err}
    try:
        return {"status": "ok", "output": t["fn"](**args)}
    except Exception as ex:  # a tool failure is a result, not a crash
        return {"status": "error", "error": "%s: %s" % (type(ex).__name__, ex)}


@tool("summarize_numbers", "Compute count, mean, standard deviation, min and max of a list of numbers.",
      {"type": "object", "required": ["values"],
       "properties": {"values": {"type": "array", "items": {"type": "number"}},
                      "budget_count": {"type": "integer"}}})
def summarize_numbers(values, budget_count=None):
    if not values:
        raise ValueError("values is empty")
    result = {"count": len(values), "mean": statistics.fmean(values), "stdev": statistics.pstdev(values),
              "min": min(values), "max": max(values)}
    if budget_count is not None:
        result["budget_count"] = budget_count
    return result


# --- BRIDGE LAB computation layer: registered wrappers around bridgelab/compute.py ---
# Every wrapper returns the compute result directly; run() adds
# {"status": "ok", "output": ...} or an error result. Compute results
# themselves always carry status, seed, dataset_hash, and metadata.
_ITEM_SCHEMA = {"type": "object", "required": ["id", "drug_a", "drug_b", "condition", "observed_score"],
                "properties": {"id": {"type": "string"}, "drug_a": {"type": "string"}, "drug_b": {"type": "string"},
                               "condition": {"type": "string"}, "observed_score": {"type": "number"},
                               "provenance": {"type": "string"}}}
_ITEMS = {"type": "array", "items": _ITEM_SCHEMA}


def _with_hash(result, dataset_hash):
    # A caller-supplied dataset_hash, if given, is the hash of record;
    # otherwise the compute result's own hash stands.
    if dataset_hash is not None:
        result = dict(result)
        result["dataset_hash"] = dataset_hash
    return result


@tool("load_dataset", "Validate a CSV or single-file zip dataset and return metadata only (row count, columns, file hash). Rows are not returned.",
      {"type": "object", "required": ["file_path"],
       "properties": {"file_path": {"type": "string"},
                      "required_columns": {"type": "array", "items": {"type": "string"}},
                      "dataset_id": {"type": "string"}}})
def load_dataset(file_path, required_columns=None, dataset_id=None):
    return _compute.load_dataset(file_path, required_columns=required_columns, dataset_id=dataset_id)


@tool("describe_data", "Describe a list of canonical items: count, distinct drugs/conditions/pairs, and observed-score mean/min/max/stdev. Python computes every number.",
      {"type": "object", "required": ["items"],
       "properties": {"items": _ITEMS, "seed": {"type": "integer"}, "dataset_hash": {"type": "string"}}})
def describe_data(items, seed=None, dataset_hash=None):
    return _with_hash(_compute.describe_data(items, seed=seed, dataset_hash=dataset_hash), dataset_hash)


@tool("make_split", "Deterministically split items into train and hidden ids by hash(seed:id). Same seed and same items give the same split.",
      {"type": "object", "required": ["items", "seed", "train_fraction"],
       "properties": {"items": _ITEMS, "seed": {"type": "integer"}, "train_fraction": {"type": "number"},
                      "dataset_hash": {"type": "string"}}})
def make_split(items, seed, train_fraction, dataset_hash=None):
    return _with_hash(_compute.make_split(items, seed, train_fraction, dataset_hash=dataset_hash), dataset_hash)


@tool("random_selection_baseline", "Random baseline: hash-ordered shuffle by seed, take budget items from hidden. Negative control.",
      {"type": "object", "required": ["hidden_items", "budget", "seed"],
       "properties": {"hidden_items": _ITEMS, "budget": {"type": "integer"}, "seed": {"type": "integer"},
                      "dataset_hash": {"type": "string"}}})
def random_selection_baseline(hidden_items, budget, seed, dataset_hash=None):
    return _with_hash(_compute.random_selection_baseline(hidden_items, budget, seed, dataset_hash=dataset_hash), dataset_hash)


@tool("simple_frequency_or_mean_baseline", "Simple baseline: score hidden items by train mean observed score of their drugs, frequency as tie-break, id as final tie-break.",
      {"type": "object", "required": ["train_items", "hidden_items", "budget"],
       "properties": {"train_items": _ITEMS, "hidden_items": _ITEMS, "budget": {"type": "integer"},
                      "seed": {"type": "integer"}, "dataset_hash": {"type": "string"}}})
def simple_frequency_or_mean_baseline(train_items, hidden_items, budget, seed=None, dataset_hash=None):
    return _with_hash(_compute.simple_frequency_or_mean_baseline(train_items, hidden_items, budget, dataset_hash=dataset_hash, seed=seed), dataset_hash)


@tool("selected_method", "Pipeline test method: exact-pair train mean, fallback drug mean, fallback global mean. This is a computation-layer test method, not a discovered cross-domain principle.",
      {"type": "object", "required": ["train_items", "hidden_items", "budget"],
       "properties": {"train_items": _ITEMS, "hidden_items": _ITEMS, "budget": {"type": "integer"},
                      "seed": {"type": "integer"}, "dataset_hash": {"type": "string"}}})
def selected_method(train_items, hidden_items, budget, seed=None, dataset_hash=None):
    return _with_hash(_compute.selected_method(train_items, hidden_items, budget, dataset_hash=dataset_hash, seed=seed), dataset_hash)


@tool("evaluate", "Evaluate a selection against hidden items: high-found count and proportion for a caller-supplied threshold. High means observed_score >= threshold.",
      {"type": "object", "required": ["selected_ids", "hidden_items", "budget", "threshold"],
       "properties": {"selected_ids": {"type": "array", "items": {"type": "string"}}, "hidden_items": _ITEMS,
                      "budget": {"type": "integer"}, "threshold": {"type": "number"},
                      "seed": {"type": "integer"}, "dataset_hash": {"type": "string"}}})
def evaluate(selected_ids, hidden_items, budget, threshold, seed=None, dataset_hash=None):
    return _with_hash(_compute.evaluate(selected_ids, hidden_items, budget, threshold, dataset_hash=dataset_hash, seed=seed), dataset_hash)


@tool("confidence_interval", "Python confidence interval: bootstrap percentile for numbers (seed required) or Wilson for binary 0/1 proportions at 0.95.",
      {"type": "object", "required": ["values"],
       "properties": {"values": {"type": "array", "items": {"type": "number"}}, "confidence_level": {"type": "number"},
                      "seed": {"type": "integer"}, "method": {"type": "string"}, "n_bootstrap": {"type": "integer"}}})
def confidence_interval(values, confidence_level=0.95, seed=None, method="bootstrap", n_bootstrap=1000):
    return _compute.confidence_interval(values, confidence_level=confidence_level, seed=seed, method=method, n_bootstrap=n_bootstrap)


@tool("dataset_hash", "Hash a dataset: either a file's bytes (file_path) or a canonical item list (items). Exactly one must be given.",
      {"type": "object", "required": [],
       "properties": {"file_path": {"type": "string"}, "items": _ITEMS}})
def dataset_hash(file_path=None, items=None):
    if (file_path is None) == (items is None):
        raise ValueError("dataset_hash requires exactly one of file_path or items")
    if file_path is not None:
        return _compute.dataset_hash_for_file(file_path)
    return {"status": "ok", "seed": None, "dataset_hash": _compute.dataset_hash_for_items(items),
            "metadata": {"method": "sha256_canonical_items", "row_count": len(items)}}


@tool("permutation_check", "Negative control: shuffle hidden observed scores with a seeded RNG, recompute the evaluate metric, and report a permutation p-value.",
      {"type": "object", "required": ["selected_ids", "hidden_items", "budget", "threshold", "n_permutations", "seed"],
       "properties": {"selected_ids": {"type": "array", "items": {"type": "string"}}, "hidden_items": _ITEMS,
                      "budget": {"type": "integer"}, "threshold": {"type": "number"}, "n_permutations": {"type": "integer"},
                      "seed": {"type": "integer"}, "dataset_hash": {"type": "string"}}})
def permutation_check(selected_ids, hidden_items, budget, threshold, n_permutations, seed, dataset_hash=None):
    return _with_hash(_compute.permutation_check(selected_ids, hidden_items, budget, threshold, n_permutations, seed, dataset_hash=dataset_hash), dataset_hash)
