"""Freeze the smallest-fair-comparison pre-registration packet (no seal, no arms).

Reads data/raw/ComboDrugGrowth_Nov2017.zip read-only, twice, with strict
separation between train and hidden handling:

  Pass 1: group structure only (row counts, scored counts, concentration
          sets, unordered pair set). No outcome is aggregated for any group.
  Split: unordered drug pairs, seed 42, train_fraction 0.8, hash-ordered
         by sha256("42:<pair_id>"), pair_id = two NSC strings sorted
         lexicographically joined by ":". Train = first 80% in hash order.
  Pass 2: TRAIN groups only: accumulate per-point differences
          (EXPECTEDGROWTH - PERCENTGROWTH) and write train_items.csv.
          HIDDEN groups: only row counts are re-checked; their
          PERCENTGROWTH / EXPECTEDGROWTH / SCORE values are never parsed,
          aggregated, or written. Hidden candidates carry identities and
          concentration grids only. No hidden label is computed, inspected,
          or stored anywhere in this script's outputs.

Outputs (comparison-prereg/):
  train_items.csv, hidden_candidates.csv, packet_manifest.json
Also asserts the previously recorded pool_hash, split_hash, and counts;
any mismatch fails loudly instead of freezing a different packet.
"""
import csv
import hashlib
import io
import json
import math
import sys
import zipfile
from collections import defaultdict

RAW_ZIP = "data/raw/ComboDrugGrowth_Nov2017.zip"
OUT_DIR = "comparison-prereg"
SEED = 42
TRAIN_FRACTION = 0.8

EXPECTED_POOL_HASH = "374d58fac23b50d13eef167b8c9437b40fce22cc0916d152babd579c1aeda01c"
EXPECTED_SPLIT_HASH = "3383719d39d237463f15eec7866aad7cd5c31a5808a47995d260d204e75e57e5"
EXPECTED_RAW_HASH = "69367a8b654908ebf70a752548c8d240fda2b751336eff0241a5986628696100"


def empty(v):
    return v is None or v.strip() == ""


def num(v):
    try:
        return float(v) if not empty(v) else None
    except ValueError:
        return None


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    import os
    os.makedirs(OUT_DIR, exist_ok=True)
    z = zipfile.ZipFile(RAW_ZIP)

    # Pass 1: structure only.
    groups = {}  # (nsc1, nsc2, cell) -> [rows, scored, conc1 set, conc2 set]
    unordered_pairs = set()
    with io.TextIOWrapper(z.open(z.namelist()[0]), newline="") as f:
        for row in csv.DictReader(f):
            if empty(row["NSC2"]):
                continue  # single-agent rows are never items
            g = (row["NSC1"], row["NSC2"], row["CELLNAME"])
            entry = groups.get(g)
            if entry is None:
                entry = groups[g] = [0, 0, set(), set()]
            entry[0] += 1
            if not empty(row["EXPECTEDGROWTH"]):
                entry[1] += 1
            entry[2].add(row["CONC1"])
            entry[3].add(row["CONC2"])
            a, b = sorted((row["NSC1"], row["NSC2"]))
            unordered_pairs.add(f"{a}:{b}")

    eligible = {g: e for g, e in groups.items()
                if e[1] == e[0] and e[0] in (9, 15)}
    assert len(eligible) == 311466, f"eligible groups {len(eligible)}"
    pool_keys = sorted(f"{a}|{b}|{c}|{e[0]}" for (a, b, c), e in eligible.items())
    pool_hash = hashlib.sha256("\n".join(pool_keys).encode()).hexdigest()
    assert pool_hash == EXPECTED_POOL_HASH, f"pool_hash {pool_hash}"

    def hkey(pid):
        return hashlib.sha256(f"{SEED}:{pid}".encode()).hexdigest()

    ordered_pairs = sorted(unordered_pairs, key=hkey)
    n_train = int(TRAIN_FRACTION * len(ordered_pairs))
    train_pairs = set(ordered_pairs[:n_train])
    hidden_pairs = set(ordered_pairs[n_train:])
    assert (len(train_pairs), len(hidden_pairs)) == (4193, 1049)
    split_rec = {"seed": SEED, "train_fraction": TRAIN_FRACTION,
                 "unit": "unordered_pair",
                 "train_pairs": ordered_pairs[:n_train],
                 "hidden_pairs": ordered_pairs[n_train:]}
    split_hash = hashlib.sha256(
        json.dumps(split_rec, sort_keys=True).encode()).hexdigest()
    assert split_hash == EXPECTED_SPLIT_HASH, f"split_hash {split_hash}"

    def side(g):
        a, b = sorted((g[0], g[1]))
        return "train" if f"{a}:{b}" in train_pairs else "hidden"

    train_groups = {g for g in eligible if side(g) == "train"}
    hidden_groups = {g for g in eligible if side(g) == "hidden"}
    assert (len(train_groups), len(hidden_groups)) == (248956, 62510), (
        len(train_groups), len(hidden_groups))

    # Pass 2: train scores only; hidden rows contribute counts only.
    train_diffs = defaultdict(list)
    hidden_rows_seen = defaultdict(int)
    with io.TextIOWrapper(z.open(z.namelist()[0]), newline="") as f:
        for row in csv.DictReader(f):
            if empty(row["NSC2"]):
                continue
            g = (row["NSC1"], row["NSC2"], row["CELLNAME"])
            if g not in eligible:
                continue
            if g in train_groups:
                expg = num(row["EXPECTEDGROWTH"])
                pg = num(row["PERCENTGROWTH"])
                assert expg is not None and pg is not None
                train_diffs[g].append(expg - pg)
            else:
                # Hidden: deliberately do NOT parse outcome columns.
                hidden_rows_seen[g] += 1
    for g, e in eligible.items():
        if g in hidden_groups:
            assert hidden_rows_seen[g] == e[0], f"hidden row count {g}"

    def item_id(g):
        return f"{g[0]}:{g[1]}:{g[2]}"

    train_path = f"{OUT_DIR}/train_items.csv"
    with open(train_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["item_id", "nsc1", "nsc2", "cellname", "grid_n",
                    "observed_score", "per_point_differences"])
        for g in sorted(train_groups, key=item_id):
            diffs = train_diffs[g]
            assert len(diffs) == groups[g][0]
            score = round(sum(diffs) / len(diffs), 6)
            w.writerow([item_id(g), g[0], g[1], g[2], len(diffs), f"{score:.6f}",
                        ";".join(f"{d:.3f}" for d in diffs)])

    def conc_sorted(values):
        return ";".join(v for v in sorted(values, key=lambda x: float(x)))

    hidden_path = f"{OUT_DIR}/hidden_candidates.csv"
    with open(hidden_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["item_id", "nsc1", "nsc2", "cellname", "grid_n",
                    "conc1_values", "conc2_values"])
        for g in sorted(hidden_groups, key=item_id):
            e = groups[g]
            w.writerow([item_id(g), g[0], g[1], g[2], e[0],
                        conc_sorted(e[2]), conc_sorted(e[3])])

    budgets = {}
    for frac in (0.05, 0.10, 0.20):
        budgets[str(frac)] = math.floor(frac * len(hidden_groups))
    assert budgets == {"0.05": 3125, "0.1": 6251, "0.2": 12502}, budgets

    manifest = {
        "raw_zip": "data/raw/ComboDrugGrowth_Nov2017.zip",
        "raw_sha256": EXPECTED_RAW_HASH,
        "pool_definition": ("eligible item = ordered (NSC1, NSC2, CELLNAME) "
                            "group, complete grid (9 or 15 rows), all rows scored"),
        "eligible_groups": len(eligible),
        "split": {"seed": SEED, "train_fraction": TRAIN_FRACTION,
                  "unit": "unordered_pair",
                  "pair_id": "two NSC strings sorted lexicographically, joined by ':'",
                  "ordering": "sha256('42:<pair_id>'), train = first 80% in hash order",
                  "train_pairs": len(train_pairs),
                  "hidden_pairs": len(hidden_pairs)},
        "train_items": len(train_groups),
        "hidden_candidates": len(hidden_groups),
        "budgets_floor": budgets,
        "pool_hash": pool_hash,
        "split_hash": split_hash,
        "train_items_sha256": sha256_file(train_path),
        "hidden_candidates_sha256": sha256_file(hidden_path),
        "hidden_label_note": ("No hidden label was computed, inspected, or stored. "
                             "Hidden candidates carry identities and concentration "
                             "grids only; no PERCENTGROWTH, EXPECTEDGROWTH, or SCORE "
                             "column exists in hidden_candidates.csv."),
        "seal_status": "NOT_CREATED",
        "arms_run": False,
    }
    with open(f"{OUT_DIR}/packet_manifest.json", "w") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
    print(json.dumps(manifest, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
