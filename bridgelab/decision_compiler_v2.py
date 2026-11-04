"""Decision compiler v2 (decision-compiler v2), written for the E0004 design.

New module only. It modifies no existing module, so the execution code
hash bound by seal E0003 stays verifiable. v1 stays the compiler for
every E0003 run.

What v2 adds: two allowlisted methods and a public ranking entry point
shared by both comparison arms.

Why. On a pair-disjoint holdout every candidate pair is unseen in train, so
v1's exact_pair_mean falls back to drug_mean for all of them and the two rank
identically; pair_frequency has no signal; and no v1 method uses the cell
line or any molecular structure.

  drug_cell_mean: key = mean over the candidate's two drugs of that drug's
    train mean in the candidate's own cell line. A drug with no train rows
    in that cell line falls back to its drug_mean, then to the global train
    mean. Parameters: {}.

  structure_cell_knn: key = drug_cell_mean + 0.5 x the similarity-weighted
    mean residual of the 30 train pairs most similar to the candidate pair,
    in the candidate's own cell line. A train pair's residual is its observed
    score minus its drug_cell_mean. Pair similarity = max(s(a,a')s(b,b'),
    s(a,b')s(b,a')) squared, where s is the Tanimoto similarity of Morgan
    fingerprints (radius 2, 2048 bits, largest fragment, RDKit) built from
    comparison-prereg-v4/inputs/nsc_smiles.csv. A drug with no structure is
    similar only to itself. Parameters: {}. Constants are fixed here; they
    are not parameters.

Ties break by item_id ascending, like every v1 method. Candidate rows
carrying any outcome column are rejected, exactly as in v1. Train rows only.
"""
from __future__ import annotations

import csv
import os

from . import decision_compiler as v1
from .ledger import canon, sha256
from .muse_runtime import validate_selection

VERSION = "decision-compiler v2"
NEW_METHODS = ("drug_cell_mean", "structure_cell_knn")
METHODS_V2 = ("exact_pair_mean", "drug_mean", "pair_frequency") + NEW_METHODS

KNN_K = 30
KNN_ALPHA = 0.5
KNN_POWER = 2
SMILES_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "comparison-prereg-v4", "inputs", "nsc_smiles.csv")

_FP_CACHE = {}


def validate_method_choice(method, params):
    """Return a list of contract errors for a (method, params) choice."""
    if method not in METHODS_V2:
        return ["selected_method: must be one of %s" % ", ".join(METHODS_V2)]
    if not isinstance(params, dict):
        return ["method_parameters: must be an object"]
    if method in NEW_METHODS:
        return ["method_parameters: %s takes no parameters" % method] if params else []
    return v1._validate_parameters(method, params)


def _mean(values):
    return sum(values) / len(values)


def _drug_cell_stats(train_rows):
    sums, counts = {}, {}
    for row in train_rows:
        for nsc in (str(row["nsc1"]), str(row["nsc2"])):
            key = (nsc, row["cellname"])
            sums[key] = sums.get(key, 0.0) + row["observed_score"]
            counts[key] = counts.get(key, 0) + 1
    return {k: sums[k] / counts[k] for k in sums}


def _dcm_key(row, stats, drug_cell):
    total = 0.0
    for nsc in (str(row["nsc1"]), str(row["nsc2"])):
        fallback = stats["drug_mean"].get(nsc, stats["global_mean"])
        total += drug_cell.get((nsc, row["cellname"]), fallback)
    return total / 2.0


def load_fingerprints(path=SMILES_PATH):
    """Morgan fingerprints keyed by NSC string; drugs RDKit cannot parse are absent."""
    if path in _FP_CACHE:
        return _FP_CACHE[path]
    from rdkit import Chem, RDLogger
    from rdkit.Chem import rdFingerprintGenerator
    RDLogger.DisableLog("rdApp.*")
    gen = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
    fps = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            mol = Chem.MolFromSmiles(r["smiles"])
            if mol is None:
                continue
            frags = Chem.GetMolFrags(mol, asMols=True)
            fps[str(r["nsc"])] = gen.GetFingerprint(max(frags, key=lambda m: m.GetNumHeavyAtoms()))
    _FP_CACHE[path] = fps
    return fps


def _similarity_matrix(drugs, fps):
    import numpy as np
    from rdkit import DataStructs
    n = len(drugs)
    sim = np.eye(n)
    have = [i for i, d in enumerate(drugs) if d in fps]
    for i in have:
        row = DataStructs.BulkTanimotoSimilarity(fps[drugs[i]], [fps[drugs[j]] for j in have])
        for j, value in zip(have, row):
            if i != j:
                sim[i, j] = value
    return sim


def score_candidates_v2(method, params, train_rows, candidate_rows, similarity=None):
    """[(item_id, score)] for drug_cell_mean / structure_cell_knn, unsorted.
    similarity, for tests only: callable(list_of_drug_ids) -> square matrix."""
    stats = v1._train_stats(train_rows)
    drug_cell = _drug_cell_stats(train_rows)
    base = {row["item_id"]: _dcm_key(row, stats, drug_cell) for row in candidate_rows}
    if method == "drug_cell_mean":
        return list(base.items())
    if method != "structure_cell_knn":
        raise ValueError("not a v2 scoring method: %s" % method)
    import numpy as np
    pair_key = v1._pair_key
    train_pairs = sorted({pair_key(r["nsc1"], r["nsc2"]) for r in train_rows})
    drugs = sorted({d for p in train_pairs for d in p}
                   | {str(r[k]) for r in candidate_rows for k in ("nsc1", "nsc2")})
    index = {d: i for i, d in enumerate(drugs)}
    sim = similarity(drugs) if similarity is not None else _similarity_matrix(drugs, load_fingerprints())
    cells = sorted({r["cellname"] for r in train_rows} | {r["cellname"] for r in candidate_rows})
    cell_index = {c: i for i, c in enumerate(cells)}
    pair_row = {p: i for i, p in enumerate(train_pairs)}
    resid = np.full((len(train_pairs), len(cells)), np.nan)
    for r in train_rows:
        resid[pair_row[pair_key(r["nsc1"], r["nsc2"])], cell_index[r["cellname"]]] = \
            r["observed_score"] - _dcm_key(r, stats, drug_cell)
    a_idx = np.array([index[p[0]] for p in train_pairs])
    b_idx = np.array([index[p[1]] for p in train_pairs])
    neighbours = {}
    out = []
    for row in candidate_rows:
        pair = pair_key(row["nsc1"], row["nsc2"])
        if pair not in neighbours:
            a, b = index[pair[0]], index[pair[1]]
            w = np.maximum(sim[a, a_idx] * sim[b, b_idx], sim[a, b_idx] * sim[b, a_idx]) ** KNN_POWER
            top = np.argsort(-w, kind="stable")[:KNN_K]
            neighbours[pair] = (top, w[top])
        top, w = neighbours[pair]
        values = resid[top, cell_index[row["cellname"]]]
        ok = ~np.isnan(values)
        adjust = float((w[ok] * values[ok]).sum() / w[ok].sum()) if ok.any() and w[ok].sum() > 0 else 0.0
        out.append((row["item_id"], base[row["item_id"]] + KNN_ALPHA * adjust))
    return out


def rank_candidates_v2(method, params, train_rows, candidate_rows, similarity=None):
    """Ordered candidate item_ids, best first. Deterministic, train-only."""
    if method not in NEW_METHODS:
        return v1._rank_candidates(method, v1._normalized_params(method, params),
                                   train_rows, candidate_rows)
    scored = score_candidates_v2(method, params, train_rows, candidate_rows, similarity=similarity)
    scored.sort(key=lambda item: (-item[1], item[0]))
    return [item_id for item_id, _ in scored]


def compile_selection_v2(method, params, train_rows, candidate_rows, budget_count, ledger=None):
    """Validate a method choice, rank with train rows only, apply the frozen
    selection rules. Returns {status, ids, flags, budget_count,
    selected_method, method_parameters} plus error fields on failure.
    A failed compile is a failed run (0 hits under ITT), never a partial one."""
    if isinstance(budget_count, bool) or not isinstance(budget_count, int) or budget_count < 1:
        raise ValueError("budget_count must be a positive integer")
    v1._check_rows(train_rows, candidate_rows)
    errors = validate_method_choice(method, params)
    if errors:
        return {"status": "failed", "error": "invalid_method_choice",
                "validation_errors": errors, "ids": [], "flags": [],
                "budget_count": budget_count, "selected_method": method if isinstance(method, str) else None}
    norm = v1._normalized_params(method, params) if method == "exact_pair_mean" else {}
    ranked = rank_candidates_v2(method, norm, train_rows, candidate_rows)
    candidate_ids = [row["item_id"] for row in candidate_rows]
    validated = validate_selection(ranked, candidate_ids, budget_count)
    base = {"selected_method": method, "method_parameters": norm, "budget_count": budget_count}
    if validated["status"] == "invalid":
        return {"status": "failed", "error": "invalid_selection", "ids": [],
                "flags": validated["flags"], **base}
    record = {"status": validated["status"], "ids": validated["ids"],
              "flags": validated["flags"], **base}
    if ledger is not None:
        ledger.append("note", "executed", {"stage": "decision_compiler_v2",
                      "status": validated["status"], "selected_method": method,
                      "method_parameters": norm, "budget_count": budget_count,
                      "selected_count": len(validated["ids"]), "flags": validated["flags"],
                      "selection_sha256": sha256(canon(validated["ids"]))})
    return record


def compile_decision_v2(judge_output, train_rows, candidate_rows, budget_count, ledger=None):
    """Compile a Judge output that may select a v2 method.

    Judge outputs that select any v1 method (or none) go through
    decision_compiler.compile_decision unchanged, so the E0004 Harness
    ranks exactly as E0003 did for those methods. A v2-method output is
    checked against the same contract as a drug_mean output (all take the
    same, empty, parameters), then ranked by rank_candidates_v2.
    Returns the same record shape as compile_decision.
    """
    method = judge_output.get("selected_method") if isinstance(judge_output, dict) else None
    if method not in NEW_METHODS:
        return v1.compile_decision(judge_output, train_rows, candidate_rows, budget_count, ledger=ledger)
    if isinstance(budget_count, bool) or not isinstance(budget_count, int) or budget_count < 1:
        raise ValueError("budget_count must be a positive integer")
    v1._check_rows(train_rows, candidate_rows)
    proxy = dict(judge_output, selected_method="drug_mean")
    errors = v1.validate_judge_output(proxy)
    if judge_output.get("method_parameters"):
        errors = list(errors) + ["method_parameters: %s takes no parameters" % method]
    if errors:
        return {"status": "failed", "error": "invalid_judge_output", "validation_errors": errors,
                "ids": [], "flags": [], "budget_count": budget_count,
                "selected_domain": judge_output.get("selected_domain"),
                "selected_method": method}
    base = {"selected_domain": judge_output["selected_domain"],
            "selected_principle": judge_output["selected_principle"],
            "selected_method": method, "method_parameters": {},
            "prediction": judge_output["prediction"], "falsifier": judge_output["falsifier"],
            "required_data_fields": judge_output["required_data_fields"],
            "budget_count": budget_count}
    ranked = rank_candidates_v2(method, {}, train_rows, candidate_rows)
    validated = validate_selection(ranked, [r["item_id"] for r in candidate_rows], budget_count)
    if validated["status"] == "invalid":
        return {"status": "failed", "error": "invalid_selection", "ids": [],
                "flags": validated["flags"], **base}
    record = {"status": validated["status"], "ids": validated["ids"],
              "flags": validated["flags"], **base}
    if ledger is not None:
        ledger.append("note", "executed", {"stage": "decision_compiler_v2",
                      "status": validated["status"], "selected_domain": base["selected_domain"],
                      "selected_method": method, "method_parameters": {},
                      "budget_count": budget_count, "selected_count": len(validated["ids"]),
                      "flags": validated["flags"],
                      "selection_sha256": sha256(canon(validated["ids"]))})
    return record
