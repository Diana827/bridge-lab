"""Python computation layer for BRIDGE LAB. Stdlib only.

This is the only place where numbers are computed. Muse never computes
metrics; it proposes and critiques, and Python computes here.

Data model (canonical, from VALIDATION_PLAN.md):
  item = {
    "id": str,            # unique, non-empty
    "drug_a": str,        # non-empty identifier, e.g. NSC1 as supplied
    "drug_b": str,        # non-empty identifier for a pair item; a
                          # single-agent row has no drug_b and is not a
                          # pair item (see validate_items)
    "condition": str,     # e.g. CELLNAME as supplied
    "observed_score": float,  # finite number; meaning is fixed by the
                          # caller/sealed rule, never inferred here
    "provenance": str,    # optional; "synthetic" marks demo data
  }

Every result dict returned here contains:
  status, seed, dataset_hash, metadata.
seed is the seed actually used, or None if the computation is
seed-free. dataset_hash is the hash actually used, or None if the
caller did not supply one (the caller must supply it for any sealed
run; a None hash in a result is a warning, not a seal).
metadata always states the method, the budget/metric/threshold used,
and whether any item was synthetic.

Rules enforced here, as errors (ValueError), never as silent fixes:
- items must be a non-empty list of dicts with unique, non-empty ids;
- drug_a/drug_b/condition must be non-empty strings for pair items;
- observed_score must be a finite number (bool, None, empty string,
  NaN, inf are all invalid);
- mismatched arrays (selected ids not in hidden, budget out of range,
  train/hidden overlap, empty train/hidden) are errors;
- missing values are never imputed.

Split: deterministic by hash, not by input order. An item's split
key is sha256(f"{seed}:{id}"). Sorting by that key and taking the
first train_fraction gives the same split for the same seed and the
same item set, regardless of input order. The split unit is the
item, not the pair or condition; a future sealed experiment must
record its split unit explicitly and must not assume pair-disjoint
splitting from this function alone.

Baselines and selected method (all use the same split, seed, budget,
and metric when called with the same arguments; the caller must
pass the same arguments, this module does not remember a run):
- random_selection_baseline: hash-ordered shuffle by seed, take budget.
- simple_frequency_or_mean_baseline: score a hidden item by the
  mean train observed_score of its drugs, with train frequency as
  tie-break and id as final tie-break. Unseen drug -> global train
  mean. This is a mean baseline; frequency is only a tie-break.
- selected_method: a transparent, documented pipeline method for
  testing the computation layer, NOT a discovered cross-domain
  principle and NOT a scientific claim. It scores a hidden item by
  the mean train observed_score of the exact pair (drug_a, drug_b),
  falling back to the drug-mean score above, then to the global
  train mean. Tie-break by id. If a future Judge selects a real
  method, that method must be implemented as a new, recorded
  function; it must not be smuggled in by renaming this one.

Evaluate: metric is "high_found_count" and "high_found_proportion"
for a caller-supplied threshold. High = observed_score >= threshold.
Threshold is a parameter, never a default scientific claim; callers
in tests pass it explicitly, and a sealed run must seal it.
If there are no high items in hidden, proportion is None and
metadata records "no_high_in_hidden".

Confidence interval: bootstrap percentile for a list of numbers
(method="bootstrap", seed required for reproducibility), or Wilson
for a proportion (method="wilson", successes/total). Both are
Python computations; neither is a Muse computation.

Permutation check (negative control): shuffle observed_scores
among hidden items with a seeded RNG, recompute the evaluate
metric, repeat n_permutations times, and report a permutation
p-value as (count >= observed + 1) / (n_permutations + 1).
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import random
import statistics
import zipfile
from pathlib import Path

REQUIRED_FIELDS = ("id", "drug_a", "drug_b", "condition", "observed_score")


def _is_finite_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def dataset_hash_for_items(items) -> str:
    """Canonical hash of items: sorted by id, fixed field order."""
    validate_items(items)
    canon = [
        {
            "id": it["id"],
            "drug_a": it["drug_a"],
            "drug_b": it["drug_b"],
            "condition": it["condition"],
            "observed_score": it["observed_score"],
        }
        for it in sorted(items, key=lambda x: x["id"])
    ]
    return _hash_text(json.dumps(canon, sort_keys=True, separators=(",", ":")))


def dataset_hash_for_file(path) -> dict:
    """Hash a file's bytes. Does not load or interpret its rows."""
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"dataset file not found: {path}")
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return {"status": "ok", "seed": None, "dataset_hash": h.hexdigest(),
            "metadata": {"file": str(p), "size_bytes": p.stat().st_size, "method": "sha256_file_bytes"}}


def validate_items(items) -> list:
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    seen = set()
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise ValueError(f"items[{i}] must be an object")
        for f in REQUIRED_FIELDS:
            if f not in it:
                raise ValueError(f"items[{i}] is missing field: {f}")
        if not isinstance(it["id"], str) or not it["id"].strip():
            raise ValueError(f"items[{i}] id must be a non-empty string")
        if it["id"] in seen:
            raise ValueError(f"duplicate item id: {it['id']}")
        seen.add(it["id"])
        for f in ("drug_a", "drug_b", "condition"):
            if not isinstance(it[f], str) or not it[f].strip():
                raise ValueError(f"items[{i}] {f} must be a non-empty string")
        if not _is_finite_number(it["observed_score"]):
            raise ValueError(f"items[{i}] observed_score must be a finite number")
    return items


def _synthetic_flag(items) -> bool:
    return any(isinstance(it, dict) and it.get("provenance") == "synthetic" for it in items)


def _base(seed, dataset_hash, metadata: dict) -> dict:
    meta = {"synthetic_data": False}
    meta.update(metadata)
    return {"status": "ok", "seed": seed, "dataset_hash": dataset_hash, "metadata": meta}


def load_dataset(path, required_columns=None, dataset_id=None) -> dict:
    """Validate a CSV dataset file and return metadata only.

    Returns row_count, columns, and the file hash. It does NOT return
    rows: a tool-loop ledger must not carry a whole dataset. Python
    code that needs rows must use load_dataset_rows() directly.
    required_columns, if given, must all be present in the header.
    """
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"dataset file not found: {path}")
    if p.suffix.lower() == ".zip":
        with zipfile.ZipFile(p) as z:
            names = z.namelist()
            if len(names) != 1:
                raise ValueError(f"zip dataset must contain exactly 1 file, got {names}")
            inner = names[0]
            with z.open(inner) as fh:
                import io

                text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="")
                reader = csv.reader(text)
                try:
                    header = next(reader)
                except StopIteration:
                    raise ValueError("dataset csv is empty")
                row_count = sum(1 for _ in reader)
    elif p.suffix.lower() == ".csv":
        with p.open(newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.reader(f)
            try:
                header = next(reader)
            except StopIteration:
                raise ValueError("dataset csv is empty")
            row_count = sum(1 for _ in reader)
    else:
        raise ValueError("load_dataset supports .csv and single-file .zip only")
    if required_columns:
        missing = [c for c in required_columns if c not in header]
        if missing:
            raise ValueError(f"dataset is missing required columns: {missing}")
    h = dataset_hash_for_file(path)["dataset_hash"]
    return _base(None, h, {"method": "load_dataset", "dataset_id": dataset_id,
                           "file": str(p), "inner_file": inner if p.suffix.lower() == ".zip" else None,
                           "columns": header, "row_count": row_count})


def load_dataset_rows(path, required_columns=None):
    """Python-only row loader (not a registered tool). Returns list of dicts."""
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"dataset file not found: {path}")
    rows = []
    if p.suffix.lower() == ".zip":
        with zipfile.ZipFile(p) as z:
            if len(z.namelist()) != 1:
                raise ValueError("zip dataset must contain exactly 1 file")
            with z.open(z.namelist()[0]) as fh:
                import io

                text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="")
                reader = csv.DictReader(text)
                if required_columns:
                    missing = [c for c in required_columns if c not in (reader.fieldnames or [])]
                    if missing:
                        raise ValueError(f"dataset is missing required columns: {missing}")
                rows = list(reader)
    else:
        with p.open(newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            if required_columns:
                missing = [c for c in required_columns if c not in (reader.fieldnames or [])]
                if missing:
                    raise ValueError(f"dataset is missing required columns: {missing}")
            rows = list(reader)
    return rows


def describe_data(items, seed=None, dataset_hash=None) -> dict:
    validate_items(items)
    scores = [it["observed_score"] for it in items]
    missing = {}
    # describe_data works on validated items, so missing here means
    # the optional provenance field only; required-field missing is
    # an error in validate_items, not a statistic.
    out = _base(seed, dataset_hash if dataset_hash is not None else dataset_hash_for_items(items),
                {"method": "describe_data", "synthetic_data": _synthetic_flag(items),
                 "count": len(items), "missing": missing,
                 "distinct_drug_a": len({it["drug_a"] for it in items}),
                 "distinct_drug_b": len({it["drug_b"] for it in items}),
                 "distinct_condition": len({it["condition"] for it in items}),
                 "distinct_pair": len({(it["drug_a"], it["drug_b"]) for it in items})})
    out["count"] = len(items)
    out["mean_observed_score"] = statistics.fmean(scores)
    out["min_observed_score"] = min(scores)
    out["max_observed_score"] = max(scores)
    out["stdev_observed_score"] = statistics.pstdev(scores) if len(scores) > 1 else 0.0
    return out


def make_split(items, seed, train_fraction, dataset_hash=None) -> dict:
    validate_items(items)
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError("seed must be an integer")
    if not _is_finite_number(train_fraction) or not (0 < train_fraction < 1):
        raise ValueError("train_fraction must be strictly between 0 and 1")
    keyed = sorted(items, key=lambda it: _hash_text(f"{seed}:{it['id']}"))
    n_train = int(len(keyed) * train_fraction)
    if n_train < 1 or n_train >= len(keyed):
        raise ValueError("train_fraction yields an empty train or hidden set for this item count")
    train, hidden = keyed[:n_train], keyed[n_train:]
    return _base(seed, dataset_hash if dataset_hash is not None else dataset_hash_for_items(items),
                 {"method": "make_split", "synthetic_data": _synthetic_flag(items),
                  "train_fraction": train_fraction, "split_unit": "item",
                  "train_count": len(train), "hidden_count": len(hidden)} ) | {
        "train_ids": [it["id"] for it in train],
        "hidden_ids": [it["id"] for it in hidden],
    }


def _split_or_error(train_items, hidden_items, seed, dataset_hash):
    validate_items(train_items)
    validate_items(hidden_items)
    train_ids = {it["id"] for it in train_items}
    hidden_ids = {it["id"] for it in hidden_items}
    if train_ids & hidden_ids:
        raise ValueError("train and hidden overlap")
    return train_ids, hidden_ids


def _check_budget(budget, hidden_count):
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1 or budget > hidden_count:
        raise ValueError("budget must be an integer between 1 and the hidden count")


def random_selection_baseline(hidden_items, budget, seed, dataset_hash=None) -> dict:
    validate_items(hidden_items)
    _check_budget(budget, len(hidden_items))
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError("seed must be an integer")
    ordered = sorted(hidden_items, key=lambda it: _hash_text(f"{seed}:{it['id']}"))
    selected = ordered[:budget]
    return _base(seed, dataset_hash if dataset_hash is not None else dataset_hash_for_items(hidden_items),
                 {"method": "random_selection_baseline", "synthetic_data": _synthetic_flag(hidden_items),
                  "budget": budget}) | {"selected_ids": [it["id"] for it in selected]}


def _drug_mean_scores(train_items):
    sums: dict[str, list] = {}
    for it in train_items:
        for d in (it["drug_a"], it["drug_b"]):
            sums.setdefault(d, []).append(it["observed_score"])
    return {d: statistics.fmean(v) for d, v in sums.items()}, statistics.fmean([it["observed_score"] for it in train_items])


def simple_frequency_or_mean_baseline(train_items, hidden_items, budget, dataset_hash=None, seed=None) -> dict:
    _split_or_error(train_items, hidden_items, seed, dataset_hash)
    _check_budget(budget, len(hidden_items))
    drug_mean, global_mean = _drug_mean_scores(train_items)
    freq: dict[str, int] = {}
    for it in train_items:
        for d in (it["drug_a"], it["drug_b"]):
            freq[d] = freq.get(d, 0) + 1

    def score(it):
        m = (drug_mean.get(it["drug_a"], global_mean) + drug_mean.get(it["drug_b"], global_mean)) / 2
        f = freq.get(it["drug_a"], 0) + freq.get(it["drug_b"], 0)
        return (-m, -f, it["id"])

    ordered = sorted(hidden_items, key=score)
    selected = ordered[:budget]
    return _base(seed, dataset_hash if dataset_hash is not None else dataset_hash_for_items(train_items + hidden_items),
                 {"method": "simple_frequency_or_mean_baseline", "synthetic_data": _synthetic_flag(train_items + hidden_items),
                  "budget": budget}) | {"selected_ids": [it["id"] for it in selected]}


def selected_method(train_items, hidden_items, budget, dataset_hash=None, seed=None) -> dict:
    """Transparent pipeline method for testing the computation layer.

    Scores by exact-pair train mean, falling back to drug mean, then
    global mean. See module docstring: this is NOT a discovered
    cross-domain principle.
    """
    _split_or_error(train_items, hidden_items, seed, dataset_hash)
    _check_budget(budget, len(hidden_items))
    pair_sums: dict[tuple, list] = {}
    for it in train_items:
        pair_sums.setdefault((it["drug_a"], it["drug_b"]), []).append(it["observed_score"])
    pair_mean = {k: statistics.fmean(v) for k, v in pair_sums.items()}
    drug_mean, global_mean = _drug_mean_scores(train_items)

    def score(it):
        pair = pair_mean.get((it["drug_a"], it["drug_b"]))
        if pair is not None:
            return (-pair, it["id"])
        m = (drug_mean.get(it["drug_a"], global_mean) + drug_mean.get(it["drug_b"], global_mean)) / 2
        return (-m, it["id"])

    ordered = sorted(hidden_items, key=score)
    selected = ordered[:budget]
    return _base(seed, dataset_hash if dataset_hash is not None else dataset_hash_for_items(train_items + hidden_items),
                 {"method": "selected_method", "synthetic_data": _synthetic_flag(train_items + hidden_items),
                  "budget": budget,
                  "method_note": "exact-pair train mean, fallback drug mean, fallback global mean; pipeline test method only"}) | {
        "selected_ids": [it["id"] for it in selected]}


def evaluate(selected_ids, hidden_items, budget, threshold, dataset_hash=None, seed=None) -> dict:
    validate_items(hidden_items)
    if not isinstance(selected_ids, list) or not selected_ids:
        raise ValueError("selected_ids must be a non-empty list")
    if len(selected_ids) != len(set(selected_ids)):
        raise ValueError("selected_ids contains duplicates")
    _check_budget(budget, len(hidden_items))
    if len(selected_ids) != budget:
        raise ValueError("selected_ids length must equal budget")
    if not _is_finite_number(threshold):
        raise ValueError("threshold must be a finite number")
    by_id = {it["id"]: it for it in hidden_items}
    unknown = [s for s in selected_ids if s not in by_id]
    if unknown:
        raise ValueError(f"selected_ids not in hidden: {unknown}")
    high_hidden = [it for it in hidden_items if it["observed_score"] >= threshold]
    found = [s for s in selected_ids if by_id[s]["observed_score"] >= threshold]
    proportion = (len(found) / len(high_hidden)) if high_hidden else None
    return _base(seed, dataset_hash if dataset_hash is not None else dataset_hash_for_items(hidden_items),
                 {"method": "evaluate", "synthetic_data": _synthetic_flag(hidden_items),
                  "budget": budget, "threshold": threshold, "metric": "high_found_count/high_found_proportion",
                  "no_high_in_hidden": not high_hidden}) | {
        "selected_count": len(selected_ids), "high_found_count": len(found),
        "high_in_hidden_count": len(high_hidden), "high_found_proportion": proportion,
        "found_ids": found}


def confidence_interval(values, confidence_level=0.95, seed=None, method="bootstrap", n_bootstrap=1000) -> dict:
    if not isinstance(values, list) or not values:
        raise ValueError("values must be a non-empty list")
    if not all(_is_finite_number(v) for v in values):
        raise ValueError("values must all be finite numbers")
    if not _is_finite_number(confidence_level) or not (0 < confidence_level < 1):
        raise ValueError("confidence_level must be strictly between 0 and 1")
    mean = statistics.fmean(values)
    alpha = 1 - confidence_level
    if method == "wilson":
        # values must be 0/1 for a proportion
        if not all(v in (0, 1, 0.0, 1.0) for v in values):
            raise ValueError("wilson method requires binary 0/1 values")
        n = len(values)
        p = mean
        z = 1.96 if confidence_level == 0.95 else None
        if z is None:
            raise ValueError("wilson method in this layer supports confidence_level 0.95 only")
        denom = 1 + z * z / n
        centre = (p + z * z / (2 * n)) / denom
        half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
        lower, upper = centre - half, centre + half
    elif method == "bootstrap":
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise ValueError("bootstrap confidence_interval requires an integer seed")
        if not isinstance(n_bootstrap, int) or n_bootstrap < 100:
            raise ValueError("n_bootstrap must be an integer >= 100")
        rng = random.Random(seed)
        means = []
        for _ in range(n_bootstrap):
            sample = [rng.choice(values) for _ in values]
            means.append(statistics.fmean(sample))
        means.sort()
        lo_i = int((alpha / 2) * len(means))
        hi_i = min(len(means) - 1, int((1 - alpha / 2) * len(means)))
        lower, upper = means[lo_i], means[hi_i]
    else:
        raise ValueError("method must be 'bootstrap' or 'wilson'")
    return _base(seed, None, {"method": f"confidence_interval_{method}",
                              "confidence_level": confidence_level, "n": len(values)}) | {
        "mean": mean, "lower": lower, "upper": upper}


def permutation_check(selected_ids, hidden_items, budget, threshold, n_permutations, seed, dataset_hash=None) -> dict:
    observed = evaluate(selected_ids, hidden_items, budget, threshold, dataset_hash=dataset_hash, seed=seed)
    if observed["high_in_hidden_count"] == 0:
        raise ValueError("permutation check requires at least one high item in hidden")
    if not isinstance(n_permutations, int) or n_permutations < 10:
        raise ValueError("n_permutations must be an integer >= 10")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError("seed must be an integer")
    rng = random.Random(seed)
    scores = [it["observed_score"] for it in hidden_items]
    obs_prop = observed["high_found_proportion"]
    ge = 0
    permuted = []
    for _ in range(n_permutations):
        shuffled = scores[:]
        rng.shuffle(shuffled)
        # assign shuffled scores to the selected positions in hidden order
        by_pos = {it["id"]: s for it, s in zip(hidden_items, shuffled)}
        found = sum(1 for s in selected_ids if by_pos[s] >= threshold)
        prop = found / observed["high_in_hidden_count"]
        permuted.append(prop)
        if prop >= obs_prop:
            ge += 1
    p_value = (ge + 1) / (n_permutations + 1)
    return _base(seed, dataset_hash if dataset_hash is not None else dataset_hash_for_items(hidden_items),
                 {"method": "permutation_check", "synthetic_data": _synthetic_flag(hidden_items),
                  "budget": budget, "threshold": threshold, "n_permutations": n_permutations,
                  "observed_proportion": obs_prop}) | {
        "observed_high_found_proportion": obs_prop, "permutation_p_value": p_value,
        "permuted_proportion_mean": statistics.fmean(permuted)}
