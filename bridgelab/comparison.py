"""Sealed-comparison execution layer (comparison-exec v1).

New module only: it wires existing, already-tested pieces together and
adds the comparison-specific runners, scorer, bootstrap, and gate that
did not exist. It modifies no existing module, no sealed packet file,
and no seal. It never opens a dataset file, a packet file, or a hidden
label: every function takes its inputs as arguments, so hidden labels
can only arrive through the HiddenLabelGate after both arms finish.

Frozen rules implemented here (from comparison-prereg, 2026-10-27):
  - Budgets: 5% = 3,125; 10% = 6,251 (primary); 20% = 12,502.
  - HIT iff observed_score > 0 (strict: a score of exactly 0 is not a
    hit). BORDERLINE iff |observed_score| <= 1, reported separately;
    borderline scores in (0, 1] count as hits under the primary rule.
  - Primary metric: precision at the 10% budget under intention-to-
    treat: hits / budget_count. A failed run selects nothing and
    counts as 0 hits, so its precision is 0.0 (never undefined).
  - Duplicate selections: deduplicated and flagged; the budget is
    filled from the remaining ranked list when one exists, otherwise
    the selection is left short and flagged. Short selections still
    divide by the full budget_count (ITT denominator), never by the
    number actually selected.
  - Empty or declined selection: none, 0 hits, NOT a failure.
  - Invalid JSON: at most 2 retries (3 attempts total); still invalid
    after that, the run fails. An API/model failure is a failed run
    immediately (its retry is a new run index, not a JSON retry).
  - Invalid selection (unknown ids, non-list): failed run, 0 hits.
    Unknown ids invalidate the whole selection; there is no partial
    scoring of the known prefix.
  - Superiority claim gate: paired-by-pair bootstrap 95% CI on the
    precision difference (harness - baseline) excludes zero AND is
    positive at the primary budget, wins at >= 2 of the 3 budgets,
    all three budgets reported. Wins mean strictly greater precision.
  - Hidden labels: opened only after both arms completed or were
    recorded as failed. Enforced by HiddenLabelGate in code.

Deliberately NOT used: bridgelab.compute.evaluate / tools.evaluate.
Its semantics differ from the sealed rules (hit is >= threshold, so a
score of exactly 0 would count; empty, duplicate, or wrong-length
selections raise instead of none/dedupe/truncate; it reports a
recall-like proportion, not precision; no borderline, ITT, or
bootstrap handling). The scorer below implements the sealed rules
directly; see score_selection.

Deliberately NOT decided here: who produces the Harness ranked list.
adapt_harness requires the caller to supply an explicit ranked list
produced by the Transfer Judge or the selected method. Domain-only,
plan-only, or verdict-only harness output is rejected. If producing
that list requires changing the sealed harness prompts, that change
requires a NEW SEAL, not an edit to this module.
"""
from __future__ import annotations
import random
import statistics

from .ledger import canon, sha256
from .muse_runtime import run_model_call, validate_selection
from .validation import parse_json_strict

VERSION = "comparison-exec v1"

FROZEN_BUDGET_COUNTS = (3125, 6251, 12502)
PRIMARY_BUDGET = 6251
MAX_JSON_ATTEMPTS = 3
RUN_STATUSES = ("ok", "none", "failed")


# ---------------------------------------------------------------- validation helpers

def is_frozen_budget(budget_count) -> bool:
    """True iff budget_count is one of the three sealed budgets."""
    return budget_count in FROZEN_BUDGET_COUNTS


def _check_candidates(candidate_ids):
    if not isinstance(candidate_ids, list) or not candidate_ids:
        raise ValueError("candidate_ids must be a non-empty list")
    if not all(isinstance(i, str) for i in candidate_ids):
        raise ValueError("candidate_ids must all be strings")
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("candidate_ids must not contain duplicates")


def _check_budget(budget_count):
    if isinstance(budget_count, bool) or not isinstance(budget_count, int) or budget_count < 1:
        raise ValueError("budget_count must be a positive integer")


def _selection_sha256(ids) -> str:
    return sha256(canon(ids))


def validate_arm_selection(selected_ids, candidate_ids, budget_count):
    """Validate one arm's selection with the frozen rules.

    Delegates to muse_runtime.validate_selection (unchanged): exact
    item_id match against the candidate vocabulary, over-budget
    truncated in selection order and flagged, duplicates deduplicated
    and flagged, empty selection -> none, unknown ids or a non-list ->
    invalid. No hidden column is inspected: only id membership.
    """
    _check_candidates(candidate_ids)
    _check_budget(budget_count)
    result = validate_selection(selected_ids, candidate_ids, budget_count)
    return {"status": result["status"], "ids": result["ids"], "flags": result["flags"]}


# ---------------------------------------------------------------- baseline runner

def _extract_text(call_result):
    """Extract model text from a run_model_call success, or None."""
    try:
        resp = call_result["response"]
        msg = resp["choices"][0]["message"]
        text = msg.get("content")
        return text if isinstance(text, str) else None
    except (KeyError, TypeError, IndexError):
        return None


def run_baseline(client, ledger, prompt_template, candidate_ids, budget_count, run_index=0):
    """Run one Baseline arm attempt sequence. Returns a run record.

    Sends the frozen baseline prompt (template with the {budget_count}
    placeholder replaced by str.replace, never str.format, because the
    prompt contains a JSON example whose braces must survive) to the
    model via muse_runtime.run_model_call, which records latency,
    verifies the model id, and extracts usage or MISSING. Only the
    selected_item_ids field is ever read: any other model content,
    including claimed numbers, is ignored and never scored.

    Outcomes: ok (validated selection, possibly flagged), none (empty
    or declined selection, valid, 0 hits), failed (invalid JSON after
    3 attempts, invalid selection, or a model/API failure). A failed
    run selects nothing, so under ITT it counts as 0 hits.
    """
    _check_candidates(candidate_ids)
    _check_budget(budget_count)
    if "{budget_count}" not in prompt_template:
        raise ValueError("baseline prompt template is missing the {budget_count} placeholder")
    prompt = prompt_template.replace("{budget_count}", str(budget_count))
    messages = [{"role": "user", "content": prompt}]
    attempts = 0
    last_error = None
    while attempts < MAX_JSON_ATTEMPTS:
        attempts += 1
        call = run_model_call(client, ledger, messages,
                              label="baseline_run_%d_attempt_%d" % (run_index, attempts))
        if call["status"] != "ok":
            # API/model failure: failed run immediately. Its retry is a
            # new run index under the frozen rules, not a JSON retry.
            record = {"arm": "baseline", "run_index": run_index, "status": "failed",
                      "error": call["error"], "ids": [], "flags": [],
                      "budget_count": budget_count, "attempts": attempts}
            ledger.append("note", "executed", {"arm": "baseline", "run_index": run_index,
                          "status": "failed", "error": call["error"],
                          "budget_count": budget_count, "attempts": attempts,
                          "selection_sha256": _selection_sha256([])})
            return record
        text = _extract_text(call)
        parsed = None
        parse_error = None
        if text is None:
            parse_error = "missing_model_text"
        else:
            try:
                parsed = parse_json_strict(text)
            except ValueError as exc:
                parse_error = "invalid_json: %s" % exc
        if parse_error is not None or not isinstance(parsed, dict) \
                or not isinstance(parsed.get("selected_item_ids"), list) \
                or not all(isinstance(i, str) for i in parsed["selected_item_ids"]):
            last_error = parse_error or "selected_item_ids_missing_or_not_a_string_list"
            continue
        validated = validate_arm_selection(parsed["selected_item_ids"], candidate_ids, budget_count)
        if validated["status"] == "invalid":
            record = {"arm": "baseline", "run_index": run_index, "status": "failed",
                      "error": "invalid_selection", "ids": [], "flags": validated["flags"],
                      "budget_count": budget_count, "attempts": attempts}
            ledger.append("note", "executed", {"arm": "baseline", "run_index": run_index,
                          "status": "failed", "error": "invalid_selection",
                          "budget_count": budget_count, "attempts": attempts,
                          "selection_sha256": _selection_sha256([])})
            return record
        record = {"arm": "baseline", "run_index": run_index, "status": validated["status"],
                  "ids": validated["ids"], "flags": validated["flags"],
                  "budget_count": budget_count, "attempts": attempts}
        ledger.append("note", "executed", {"arm": "baseline", "run_index": run_index,
                      "status": validated["status"], "budget_count": budget_count,
                      "selected_count": len(validated["ids"]), "flags": validated["flags"],
                      "attempts": attempts, "selection_sha256": _selection_sha256(validated["ids"])})
        return record
    record = {"arm": "baseline", "run_index": run_index, "status": "failed",
              "error": "invalid_json_after_retries", "ids": [], "flags": [],
              "budget_count": budget_count, "attempts": attempts,
              "last_error": last_error}
    ledger.append("note", "executed", {"arm": "baseline", "run_index": run_index,
                  "status": "failed", "error": "invalid_json_after_retries",
                  "budget_count": budget_count, "attempts": attempts,
                  "selection_sha256": _selection_sha256([])})
    return record


# ---------------------------------------------------------------- harness adapter

def adapt_harness(harness_result, ranked_item_ids, candidate_ids, budget_count, ledger=None):
    """Convert a Harness pipeline result into a validated selection.

    harness_result is the dict returned by orchestration.run_pipeline.
    ranked_item_ids must be an explicit ranked list produced by the
    Transfer Judge or the selected method; it is NOT derived here from
    a domain, a plan, or a verdict. Domain-only, plan-only, or
    verdict-only output is rejected as failed.

    Judge none (selected_domain == "none") is preserved as a valid
    none selection even if a ranked list was supplied. A selected
    domain with an empty or missing ranked list is failed, not none:
    none belongs to Judge none only.
    """
    _check_candidates(candidate_ids)
    _check_budget(budget_count)
    if not isinstance(harness_result, dict) or harness_result.get("status") != "ok":
        return {"arm": "harness", "status": "failed", "error": "harness_not_ok",
                "ids": [], "flags": [], "budget_count": budget_count}
    domain = harness_result.get("selected_domain")
    if domain == "none":
        record = {"arm": "harness", "status": "none", "ids": [], "flags": [],
                  "budget_count": budget_count, "selected_domain": "none"}
        if ledger is not None:
            ledger.append("note", "executed", {"arm": "harness", "status": "none",
                          "selected_domain": "none", "budget_count": budget_count,
                          "selected_count": 0, "selection_sha256": _selection_sha256([])})
        return record
    if not isinstance(domain, str) or not domain:
        return {"arm": "harness", "status": "failed", "error": "missing_selected_domain",
                "ids": [], "flags": [], "budget_count": budget_count}
    if ranked_item_ids is None:
        return {"arm": "harness", "status": "failed", "error": "missing_ranked_list",
                "ids": [], "flags": [], "budget_count": budget_count}
    if not isinstance(ranked_item_ids, list) or not ranked_item_ids:
        return {"arm": "harness", "status": "failed", "error": "empty_or_invalid_ranked_list",
                "ids": [], "flags": [], "budget_count": budget_count}
    validated = validate_arm_selection(ranked_item_ids, candidate_ids, budget_count)
    if validated["status"] == "invalid":
        return {"arm": "harness", "status": "failed", "error": "invalid_selection",
                "ids": [], "flags": validated["flags"], "budget_count": budget_count,
                "selected_domain": domain}
    record = {"arm": "harness", "status": validated["status"], "ids": validated["ids"],
              "flags": validated["flags"], "budget_count": budget_count,
              "selected_domain": domain}
    if ledger is not None:
        ledger.append("note", "executed", {"arm": "harness", "status": validated["status"],
                      "selected_domain": domain, "budget_count": budget_count,
                      "selected_count": len(validated["ids"]), "flags": validated["flags"],
                      "selection_sha256": _selection_sha256(validated["ids"])})
    return record


# ---------------------------------------------------------------- primary scorer

def score_selection(selection, labels, budget_count):
    """Score one validated selection against hidden labels (dict).

    labels maps item_id -> observed_score (float). Labels must come
    from the HiddenLabelGate after both arms finish; this function
    never opens a file. HIT iff score > 0 (strict). BORDERLINE iff
    |score| <= 1, reported separately; borderline scores in (0, 1]
    are hits under the primary rule and are counted in hits as well
    as in borderline_hits. Precision uses the ITT denominator
    budget_count, so a failed/none/short selection scores 0.0 rather
    than dividing by the number selected (or dividing by zero).
    Secondary metrics (recall, mean) are returned under "secondary"
    and are never the primary metric.
    """
    _check_budget(budget_count)
    if not isinstance(selection, dict) or selection.get("status") not in RUN_STATUSES:
        raise ValueError("selection must be a run record with status ok/none/failed")
    if not isinstance(labels, dict):
        raise ValueError("labels must be a dict of item_id -> observed_score")
    status = selection["status"]
    if status in ("failed", "none"):
        return {"status": status, "budget_count": budget_count, "selected_count": 0,
                "hits": 0, "precision": 0.0, "borderline_count": 0, "borderline_hits": 0,
                "mean_score": None,
                "secondary": {"recall": None, "mean_score": None, "hits": 0,
                              "borderline_count": 0, "borderline_hits": 0}}
    ids = selection["ids"]
    unknown = [i for i in ids if i not in labels]
    if unknown:
        raise ValueError("selected ids missing from labels: %s" % unknown[:5])
    scores = [labels[i] for i in ids]
    hits = sum(1 for s in scores if s > 0)
    borderline = sum(1 for s in scores if abs(s) <= 1)
    borderline_hits = sum(1 for s in scores if 0 < s <= 1)
    total_hidden_hits = sum(1 for v in labels.values() if v > 0)
    recall = (hits / total_hidden_hits) if total_hidden_hits else None
    mean_score = statistics.fmean(scores) if scores else None
    return {"status": status, "budget_count": budget_count, "selected_count": len(ids),
            "hits": hits, "precision": hits / budget_count,
            "borderline_count": borderline, "borderline_hits": borderline_hits,
            "mean_score": mean_score,
            "secondary": {"recall": recall, "mean_score": mean_score, "hits": hits,
                          "borderline_count": borderline, "borderline_hits": borderline_hits}}


def score_budgets(selections_by_budget, labels):
    """Score one arm at every budget in selections_by_budget.

    Returns per-budget scores plus the primary metric: precision at
    the 10% budget (6,251) under ITT. If the 10% selection is absent,
    primary_precision is None (the arm cannot be claimed on).
    """
    budgets = {}
    for budget_count, selection in selections_by_budget.items():
        budgets[budget_count] = score_selection(selection, labels, budget_count)
    primary = budgets.get(PRIMARY_BUDGET)
    return {"budgets": budgets,
            "primary_budget": PRIMARY_BUDGET,
            "primary_precision": primary["precision"] if primary is not None else None}


# ---------------------------------------------------------------- bootstrap + claim gate

def paired_bootstrap_ci(pair_rows, n_bootstrap=1000, seed=42, budget_count=None):
    """Paired-by-pair bootstrap 95% CI on (harness - baseline) precision.

    PRIMARY estimand (fixed): the ITT precision difference,
    (harness_hits - baseline_hits) / budget_count, where budget_count
    is the frozen budget the selections were scored against (the same
    ITT denominator score_selection uses). budget_count is required;
    without it the primary CI cannot be defined, so it raises instead
    of silently falling back to another estimand.

    SECONDARY estimand (reported only, never gating): the
    per-protocol difference, (h_hits / h_sel) - (b_hits / b_sel),
    under the key "per_protocol". The claim gate uses the ITT CI only.

    pair_rows is a list of per-item dicts: {"pair": <opaque pair key>,
    "b_sel": 0/1, "b_hit": 0/1, "h_sel": 0/1, "h_hit": 0/1}, where a hit
    flag is 1 iff the arm selected the item AND its observed_score > 0.
    Pairs are resampled with replacement (never items): each resampled
    pair contributes its items again. Both differences are computed
    from the SAME resampled replicates, so the primary and secondary
    CIs share one effective replicate set. Replicates with a zero
    selected count for either arm (per-protocol undefined) are skipped
    and counted; if fewer than 100 effective replicates remain, both
    CIs are None and the gate must fail. The observed differences use
    all pairs without resampling. Deterministic for a fixed seed; the
    seed and n_bootstrap are recorded in output.
    """
    if not isinstance(pair_rows, list) or not pair_rows:
        raise ValueError("pair_rows must be a non-empty list")
    for row in pair_rows:
        for key in ("pair", "b_sel", "b_hit", "h_sel", "h_hit"):
            if key not in row:
                raise ValueError("pair_rows entries require %s" % key)
        for key in ("b_sel", "b_hit", "h_sel", "h_hit"):
            if row[key] not in (0, 1):
                raise ValueError("pair_rows flags must be 0/1")
    if isinstance(n_bootstrap, bool) or not isinstance(n_bootstrap, int) or n_bootstrap < 100:
        raise ValueError("n_bootstrap must be an integer >= 100")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    _check_budget(budget_count)
    by_pair = {}
    for row in pair_rows:
        by_pair.setdefault(row["pair"], []).append(row)

    def diffs_for(rows):
        b_sel = sum(r["b_sel"] for r in rows)
        h_sel = sum(r["h_sel"] for r in rows)
        if b_sel == 0 or h_sel == 0:
            return None
        b_hit = sum(r["b_hit"] for r in rows)
        h_hit = sum(r["h_hit"] for r in rows)
        itt = (h_hit - b_hit) / budget_count
        pp = (h_hit / h_sel) - (b_hit / b_sel)
        return (itt, pp)

    observed = diffs_for(pair_rows)
    pairs = list(by_pair.values())
    rng = random.Random(seed)
    itt_diffs = []
    pp_diffs = []
    skipped = 0
    for _ in range(n_bootstrap):
        sampled = []
        for chosen in rng.choices(pairs, k=len(pairs)):
            sampled.extend(chosen)
        diff = diffs_for(sampled)
        if diff is None:
            skipped += 1
        else:
            itt_diffs.append(diff[0])
            pp_diffs.append(diff[1])

    def percentile(values):
        ordered = sorted(values)
        return (ordered[int(0.025 * (len(ordered) - 1))],
                ordered[int(0.975 * (len(ordered) - 1))])

    if len(itt_diffs) < 100 or observed is None:
        return {"observed_difference": observed[0] if observed else None,
                "ci_low": None, "ci_high": None,
                "n_bootstrap": n_bootstrap, "n_effective": len(itt_diffs), "n_skipped": skipped,
                "seed": seed, "method": "paired_by_pair_bootstrap_95pct_itt",
                "per_protocol": {"observed_difference": observed[1] if observed else None,
                                 "ci_low": None, "ci_high": None}}
    itt_lo, itt_hi = percentile(itt_diffs)
    pp_lo, pp_hi = percentile(pp_diffs)
    return {"observed_difference": observed[0], "ci_low": itt_lo, "ci_high": itt_hi,
            "n_bootstrap": n_bootstrap, "n_effective": len(itt_diffs), "n_skipped": skipped,
            "seed": seed, "method": "paired_by_pair_bootstrap_95pct_itt",
            "per_protocol": {"observed_difference": observed[1],
                             "ci_low": pp_lo, "ci_high": pp_hi}}


def evaluate_claim_gate(budget_evaluations):
    """Apply the sealed superiority claim gate.

    budget_evaluations maps budget_count -> {"baseline_precision": float,
    "harness_precision": float, "ci_low": float|None, "ci_high": float|None}.
    Precisions and CIs here are ITT (hits / budget_count); the
    per-protocol CI is secondary-only and is never passed to the gate.
    Claim (primary interpretation): all three budgets reported, the
    primary (10%) CI excludes zero and favors harness (ci_low > 0),
    and harness wins (strictly greater precision) at >= 2 budgets.
    The stricter all-CIs-exclude-zero reading is also reported, so it
    can be applied from the same output without new code.
    """
    per_budget = {}
    wins = 0
    for budget_count in FROZEN_BUDGET_COUNTS:
        ev = budget_evaluations.get(budget_count)
        if ev is None:
            per_budget[budget_count] = {"reported": False}
            continue
        b_prec = ev["baseline_precision"]
        h_prec = ev["harness_precision"]
        diff = h_prec - b_prec
        win = h_prec > b_prec
        wins += 1 if win else 0
        ci_low, ci_high = ev.get("ci_low"), ev.get("ci_high")
        excludes_zero = ci_low is not None and ci_high is not None and (ci_high < 0 or ci_low > 0)
        favors_harness = ci_low is not None and ci_low > 0
        per_budget[budget_count] = {"reported": True, "baseline_precision": b_prec,
                                    "harness_precision": h_prec, "difference": diff,
                                    "win": win, "ci_low": ci_low, "ci_high": ci_high,
                                    "ci_excludes_zero": excludes_zero,
                                    "ci_favors_harness": favors_harness}
    all_reported = all(per_budget[b]["reported"] for b in FROZEN_BUDGET_COUNTS)
    primary = per_budget[PRIMARY_BUDGET]
    primary_favors = primary.get("ci_favors_harness", False)
    all_ci_favor = all_reported and all(per_budget[b]["ci_favors_harness"] for b in FROZEN_BUDGET_COUNTS)
    claim = bool(all_reported and primary_favors and wins >= 2)
    return {"per_budget": per_budget, "all_budgets_reported": all_reported,
            "win_count": wins, "wins_required": 2,
            "primary_ci_excludes_zero": primary.get("ci_excludes_zero", False),
            "primary_ci_favors_harness": primary_favors,
            "all_cis_favor_harness": all_ci_favor,
            "claim_superiority": claim}


# ---------------------------------------------------------------- hidden-label gate

class HiddenLabelGate:
    """Code-enforced hidden-label boundary.

    Arms are recorded done with mark_baseline_done / mark_harness_done;
    any of ok, none, or failed counts as done (the sealed rule is
    'completed or recorded as failed'). open_labels(loader) raises
    until both arms are done and never calls loader early (tests
    assert the loader is not invoked). After opening, the same labels
    object is returned on repeat calls and the loader runs once.
    Arm runners and the adapter take no labels argument at all, so
    labels cannot leak into arm execution through this module.
    """

    def __init__(self):
        self.baseline_status = None
        self.harness_status = None
        self._labels = None
        self._opened = False
        self._loader_calls = 0

    def mark_baseline_done(self, status):
        if status not in RUN_STATUSES:
            raise ValueError("baseline status must be ok, none, or failed")
        self.baseline_status = status

    def mark_harness_done(self, status):
        if status not in RUN_STATUSES:
            raise ValueError("harness status must be ok, none, or failed")
        self.harness_status = status

    def both_done(self):
        return self.baseline_status is not None and self.harness_status is not None

    def open_labels(self, loader):
        if not self.both_done():
            raise ValueError(
                "hidden labels may be opened only after both Baseline and Harness "
                "runs have completed or been recorded as failed "
                "(baseline=%s, harness=%s)" % (self.baseline_status, self.harness_status))
        if not self._opened:
            self._labels = loader()
            self._loader_calls += 1
            self._opened = True
        return self._labels
