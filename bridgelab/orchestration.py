"""Multi-agent orchestration contract (confirmed by the user).

Order, exactly:
    problem_structurer -> mathematician, physicist, chemist independently
    -> biology_validator -> transfer_judge -> Python execution
    -> critic -> reporter

Confirmed rules implemented here:
- Judge scores are finite numbers in [0, 1], every criterion required.
- Sealed-plan split is train_ids / hidden_ids / start_set_ids plus their
  sha256 hashes, split_seed, split_method, and dataset_hash. ID lists
  themselves are sealed in the payload; only their hashes are also stored
  separately so a reader can check them without re-reading every ID.
- budget is a count of items or pairs (integer >= 0). A fraction, when
  given, is converted by Python to budget_count and both are recorded;
  success criteria and execution tools use budget_count.
- Judge none: no execution seal, no execution, critic returns
  not_executed, and a judge_decision verdict entry (provenance model)
  with classification not_executed and reason "no testable candidate"
  is recorded. Not-executed work is never dressed up as a sealed experiment.
- structurer clarification is deferred; only a post-debate Judge decision can
  stop the pipeline with blocked_on_clarification
  note entry.
- molecular_structure_available is computed by the application from the
  dataset manifest, never by a model. When false, chemistry is untestable
  and cannot be the selected execution hypothesis.
- Critic evidence: numeric values must match an executed entry; string
  values are explanation only; a number hidden as a string is rejected
  by validation.
- Reporter claims are explicit JSON: {text, label, ledger_entries}, with
  labels user_provided | retrieved | model_generated_hypothesis |
  executed_result. Only executed_result claims are audited as executed numbers.
- Critic requested_calls are never auto-executed. The application checks
  (1) next_action is not stop, (2) rerun budget remains, (3) every tool is
  allowlisted, (4) arguments are valid, (5) a new seal can be made.
  A blocked request is recorded as blocked_requested_call.
- Influence is computed in Python from downstream artifacts, never from
  model self-report: {selected_candidate, rejected_candidate,
  selected_plan_field, caused_tool_call, changed_verdict,
  caused_verdict_ceiling, ledger_citations_in_final_report}.
- Only deterministic fake clients are used in tests. No live Muse call
  and no UI is created by this module; the client is injected.

Seal and reveal are engine operations here, never tools.
Python computes every number; the model never does.
"""
from __future__ import annotations
import json, math, os
from . import tools
from .agent import run_role
from .audit import audit_claims, audit_reporter_claims, entry_numbers
from .ledger import Ledger, canon, sha256
from .rerun import CriticRerunBudget
from .seal import seal as seal_plan
from .validation import (validate_dataset_manifest, validate_sealed_plan)
from .verdict import compute_ceiling, enforce_verdict

ROLE_TO_PROMPT_FILE = {
    "problem_structurer": "problem_structurer.md",
    "mathematician": "mathematician.md",
    "physicist": "physicist.md",
    "chemist": "chemist.md",
    "biology_validator": "biology_validator.md",
    "transfer_judge": "transfer_judge_v2.md",
    "critic": "critic_v2.md",
    "reporter": "reporter_v2.md",
}
EXPERT_ROLES = ("mathematician", "physicist", "chemist")
ROLE_ORDER = ("problem_structurer",) + EXPERT_ROLES + ("biology_validator", "transfer_judge", "critic", "reporter")


def load_role_prompts(prompts_dir: str) -> dict:
    prompts = {}
    for role, filename in ROLE_TO_PROMPT_FILE.items():
        path = os.path.join(prompts_dir, filename)
        with open(path, encoding="utf-8") as f:
            prompts[role] = f.read()
    return prompts


def convert_budget(fraction, count, total_items):
    """Convert a budget fraction to a count in Python, or check a count.

    Returns (budget_fraction, budget_count). When both are given they
    must agree with round(fraction * total_items); when only one is
    given the other is derived when total_items is known.
    """
    if fraction is None and count is None:
        raise ValueError("budget fraction or budget count is required")
    if fraction is not None:
        if not isinstance(fraction, (int, float)) or isinstance(fraction, bool) or not math.isfinite(fraction) or not 0 <= fraction <= 1:
            raise ValueError("budget fraction must be a finite number in [0, 1]")
    if count is not None:
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("budget count must be an integer >= 0")
    if fraction is not None and total_items is not None:
        computed = int(round(fraction * total_items))
        if count is not None and count != computed:
            raise ValueError("budget count %d does not match fraction %s of %d items" % (count, fraction, total_items))
        return fraction, computed
    if fraction is not None and count is not None:
        return fraction, count
    if fraction is not None:
        return fraction, None
    return None, count


SPLIT_REQUIRED_KEYS = ("train_ids", "hidden_ids", "start_set_ids",
                       "split_seed", "split_method")


def validate_split_ids(split_ids):
    """Validate a split_ids object (the packet's split_ids.json shape).

    Returns split_ids unchanged. Raises ValueError with a specific
    message on any violation. Lists are never accepted: a list cannot
    carry the seed, the method, or the three id sets, so a list input
    is a format error, not a split. This is the single validator used
    by the live caller, the pipeline (via build_split), and the
    comparison driver, so all three stages enforce the same contract.
    """
    if not isinstance(split_ids, dict):
        raise ValueError(
            "split_ids must be a JSON object with keys %s; got %s "
            "(lists are not accepted)"
            % (", ".join(SPLIT_REQUIRED_KEYS), type(split_ids).__name__))
    missing = [k for k in SPLIT_REQUIRED_KEYS if k not in split_ids]
    if missing:
        raise ValueError("split_ids is missing required keys: %s"
                         % ", ".join(missing))
    for key in ("train_ids", "hidden_ids", "start_set_ids"):
        ids = split_ids[key]
        if not isinstance(ids, list):
            raise ValueError("split_ids[%r] must be a list of strings; got %s"
                             % (key, type(ids).__name__))
        for i, item in enumerate(ids):
            if not isinstance(item, str):
                raise ValueError("split_ids[%r][%d] must be a string; got %s"
                                 % (key, i, type(item).__name__))
    seed = split_ids["split_seed"]
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("split_ids['split_seed'] must be an integer; got %r"
                         % (seed,))
    method = split_ids["split_method"]
    if not isinstance(method, str) or not method:
        raise ValueError("split_ids['split_method'] must be a non-empty string")
    overlap = set(split_ids["train_ids"]) & set(split_ids["hidden_ids"])
    if overlap:
        raise ValueError("split_ids train_ids and hidden_ids overlap "
                         "(%d shared ids)" % len(overlap))
    return split_ids


def load_split_ids_file(path):
    """Read a split_ids JSON file and validate it (dict shape only)."""
    with open(path, encoding="utf-8") as f:
        return validate_split_ids(json.load(f))


def build_split(split_ids, dataset_hash):
    """Build the sealed split object, including per-list hashes."""
    validate_split_ids(split_ids)
    train_ids = list(split_ids.get("train_ids", []))
    hidden_ids = list(split_ids.get("hidden_ids", []))
    start_set_ids = list(split_ids.get("start_set_ids", []))
    return {
        "train_ids": train_ids,
        "hidden_ids": hidden_ids,
        "start_set_ids": start_set_ids,
        "train_ids_hash": sha256(canon(train_ids)),
        "hidden_ids_hash": sha256(canon(hidden_ids)),
        "start_set_ids_hash": sha256(canon(start_set_ids)),
        "split_seed": split_ids.get("split_seed"),
        "split_method": split_ids.get("split_method", "fixed_random_split"),
        "dataset_hash": dataset_hash,
    }


def build_sealed_plan(judge, dataset_hash, split, seed, budget_fraction, budget_count,
                      metric, baseline, confidence, minimum_effect):
    return {
        "dataset_hash": dataset_hash,
        "split": split,
        "seed": seed,
        "budget": budget_count,
        "budget_count": budget_count,
        "budget_fraction": budget_fraction,
        "metric": metric,
        "baseline": baseline,
        "confidence": confidence,
        "minimum_effect": minimum_effect,
        "selected_domain": judge["selected_domain"],
        "selected_hypothesis": judge["selected_principle"],
        "prediction": judge["prediction"],
        "falsifier": judge["falsifier"],
    }


ROLE_TO_DOMAIN = {"mathematician": "mathematics", "physicist": "physics", "chemist": "chemistry"}


def _influence(role, state):
    """Python-computed influence for one role, using the confirmed shape."""
    selected = state.get("selected_domain")
    rejected = [item["domain"] for item in state.get("rejected_candidates", [])]
    is_expert = role in EXPERT_ROLES
    domain = ROLE_TO_DOMAIN.get(role)
    selected_candidate = bool(is_expert and selected is not None and domain == selected)
    rejected_candidate = bool(is_expert and domain in rejected)
    if is_expert and selected_candidate:
        selected_plan_field = "selected_hypothesis"
    elif role == "transfer_judge" and selected is not None and selected != "none":
        selected_plan_field = "selected_domain"
    elif role == "biology_validator" and any(d in rejected for d in ("mathematics", "physics", "chemistry")):
        selected_plan_field = "rejected_candidates"
    else:
        selected_plan_field = ""
    caused_tool_call = list(state.get("executed_tools", [])) if role == "transfer_judge" else (
        list(state.get("approved_rerun_tools", [])) if role == "critic" else [])
    changed_verdict = bool(role == "critic" and state.get("critic_requested") is not None
                           and state.get("final_verdict") is not None
                           and state["critic_requested"] != state["final_verdict"])
    caused_ceiling = bool(role == "critic" and changed_verdict)
    citations = 0
    if role == "reporter":
        citations = sum(len(claim.get("ledger_entries", [])) for claim in state.get("reporter_claims", []))
    return {
        "selected_candidate": selected_candidate,
        "rejected_candidate": rejected_candidate,
        "selected_plan_field": selected_plan_field,
        "caused_tool_call": caused_tool_call,
        "changed_verdict": changed_verdict,
        "caused_verdict_ceiling": caused_ceiling,
        "ledger_citations_in_final_report": citations,
    }


def _record_influences(ledger, calls, state):
    entries = []
    for role, call in calls.items():
        if call is None:
            continue
        entries.append(ledger.append("influence", "executed", {
            "role": role,
            "model_call_entry": call.get("model_call_entry"),
            "influence": _influence(role, state),
        }))
    return entries


def run_execution_placeholder(ledger, sealed_plan, judge_required_tools, execution_calls):
    """Stage 2 Python execution placeholder.

    This is deliberately NOT a Case A data tool: it does not load a
    dataset, simulate screening, or evaluate pairs. It runs only the
    tools the Transfer Judge planned, only if they are registered,
    and only if a budget_count argument matches the sealed budget.
    Returns (status, executed_entries, executed_tools, error, tool).
    Every attempted call, successful or failed, is recorded as a
    tool_call ledger entry.
    """
    budget_count = sealed_plan["budget_count"]
    executed_entries = []
    executed_tools = []
    for call in execution_calls or []:
        tool_name = call.get("tool")
        arguments = call.get("arguments", {})
        if tool_name not in tools.TOOLS:
            ledger.append("tool_call", "executed", {"tool": tool_name, "status": "error",
                                                   "result": {"status": "error", "error": "unregistered tool: %s" % tool_name}})
            return "failed", executed_entries, executed_tools, "unregistered_tool", tool_name
        if tool_name not in judge_required_tools:
            ledger.append("tool_call", "executed", {"tool": tool_name, "status": "error",
                                                   "result": {"status": "error", "error": "tool not in judge plan: %s" % tool_name}})
            return "failed", executed_entries, executed_tools, "tool_not_in_plan", tool_name
        if "budget_count" in arguments and arguments["budget_count"] != budget_count:
            ledger.append("tool_call", "executed", {"tool": tool_name, "status": "error",
                                                   "result": {"status": "error", "error": "budget_count mismatch"}})
            return "failed", executed_entries, executed_tools, "budget_count_mismatch", tool_name
        result_obj = tools.run(tool_name, arguments)
        if result_obj.get("status") != "ok":
            ledger.append("tool_call", "executed", {"tool": tool_name, "status": "error",
                                                   "result": result_obj})
            return "failed", executed_entries, executed_tools, "execution_tool_failed", tool_name
        entry = ledger.append("tool_call", "executed", {"tool": tool_name, "status": "ok",
                                                        "result": result_obj})
        executed_entries.append(entry)
        executed_tools.append(tool_name)
    return "ok", executed_entries, executed_tools, None, None


def _fail(ledger, calls, state, error, result_extra=None):
    if error == "max_steps_exceeded":
        # Record the limit stop itself in the ledger, before influences,
        # so the chain shows why the pipeline stopped.
        ledger.append("note", "executed", {
            "status": "max_steps_exceeded",
            "model_calls_used": state.get("model_calls_used"),
            "max_total_model_steps": state.get("max_total_model_steps")})
    _record_influences(ledger, calls, state)
    result = {"status": "failed", "error": error, "state": state}
    if result_extra:
        result.update(result_extra)
    return result


def run_pipeline(client, ledger, user_prompt, role_prompts, dataset_manifest, split_ids,
                 metric, baseline, confidence, minimum_effect, budget_fraction=None, budget_count=None,
                 total_items=None, execution_calls=None, verdict_input=None, rerun_budget=None,
                 max_total_model_steps=20, review_callback=None, execution_callback=None):
    """Run the full orchestration order. Returns a result dict.

    execution_calls: list of {"tool": name, "arguments": {...}} supplied
    by the caller (the execution engine's plan). Each tool must be
    registered and each argument must be valid; a tool's budget_count
    argument, when present, must equal the sealed budget_count.
    verdict_input: Python-computed primary metric result
    {value, ci_low, ci_high, baseline_value}, or None. When None, the
    pipeline looks for those fields in an executed tool output.
    """
    manifest_errors = validate_dataset_manifest(dataset_manifest)
    if manifest_errors:
        return {"status": "failed", "error": "invalid_dataset_manifest", "validation_errors": manifest_errors}
    dataset_hash = dataset_manifest["dataset_id"]
    molecular = bool(dataset_manifest["molecular_structure_available"])
    try:
        budget_fraction, budget_count = convert_budget(budget_fraction, budget_count, total_items)
    except ValueError as exc:
        return {"status": "failed", "error": "invalid_budget", "validation_errors": [str(exc)]}
    if budget_count is None:
        return {"status": "failed", "error": "invalid_budget",
                "validation_errors": ["budget count cannot be derived without total_items"]}
    calls = {}
    state = {"molecular_structure_available": molecular, "budget_count": budget_count,
             "budget_fraction": budget_fraction, "executed_tools": []}
    # Safeguard 7, global accounting (Stage 1 verification fix):
    # max_total_model_steps is a per-ledger global budget, not a
    # per-invocation reset. Every model call counts toward it: every
    # expert call, every retry, every Critic call, every Reporter call,
    # and every follow-up model call made on the same ledger. The
    # count is the absolute number of model_call entries in the ledger,
    # so a follow-up run on a reused ledger does not get a fresh budget.
    # There is no separate retry path: run_role is called with
    # max_retries capped by the remaining budget, so retries can reach
    # the limit exactly but can never exceed it. Reaching the limit on
    # a non-final role stops the pipeline immediately with
    # max_steps_exceeded; reaching it on the final Reporter role is a
    # successful completion, because the total never exceeded the limit.
    # The default 20 matches agent.MAX_MODEL_STEPS; a fresh 8-role run
    # with no retries uses 8 calls.
    if isinstance(max_total_model_steps, bool) or not isinstance(max_total_model_steps, int) or max_total_model_steps < 1:
        raise ValueError("max_total_model_steps must be a positive integer")
    state["max_total_model_steps"] = max_total_model_steps

    def model_calls_used():
        return len([e for e in ledger.entries() if e["kind"] == "model_call"])

    def call_role(role, user_text):
        remaining = max_total_model_steps - model_calls_used()
        if remaining <= 0:
            state["model_calls_used"] = model_calls_used()
            return {"status": "failed", "error": "max_steps_exceeded", "role": role}, True
        # Cap retries by the remaining global budget. This is the fix
        # for the retry-path ambiguity: a role that needs retries gets
        # at most (remaining - 1) retries, so its attempts can reach
        # the limit but never pass it.
        capped_retries = min(2, remaining - 1)
        result = run_role(client, ledger, role, user_text,
                          max_retries=capped_retries, system=role_prompts[role])
        calls[role] = result
        state["model_calls_used"] = model_calls_used()
        if model_calls_used() > max_total_model_steps:
            # Safety net only; the capped retry count above must make
            # this unreachable. If it ever fires, stop immediately.
            return result, True
        if model_calls_used() >= max_total_model_steps and role != "reporter":
            return result, True
        return result, False

    # 1. Problem Structurer.
    result, exceeded = call_role("problem_structurer", user_prompt)
    if exceeded:
        return _fail(ledger, calls, state, "max_steps_exceeded", {"role": "problem_structurer"})
    if result["status"] != "ok":
        return _fail(ledger, calls, state, result["error"], {"role": "problem_structurer"})
    structured = result["parsed"]
    # A structurer clarification is a hypothesis about missing information,
    # not a user gate. Let the expert roles debate under explicit assumptions;
    # only the post-debate Judge may conclude that clarification is necessary.
    if structured.get("clarification") is not None:
        state["clarification_deferred"] = structured["clarification"]
        ledger.append("note", "model", {
            "status": "clarification_deferred",
            "question": structured["clarification"],
            "reason": "experts must first attempt a testable plan"})
        structured["clarification"] = None

    # Give the Judge an explicit execution contract.  Without this, the
    # model sees only expert prose and can reasonably conclude that budget,
    # candidate pool, and executable methods are missing even though Python
    # has them locally.
    execution_contract = {
        "budget_fraction": budget_fraction,
        "budget_count": budget_count,
        "total_items": total_items,
        "metric": metric,
        "baseline": baseline,
        "train_only": True,
        "hidden_labels_available_to_model": False,
        "candidate_selection_is_python_only": True,
        "allowed_methods": ["exact_pair_mean", "drug_mean", "pair_frequency"],
        "available_fields": ["item_id", "nsc1", "nsc2", "cellname", "observed_score"],
        "judge_instruction": (
            "Treat this as an executable train-only screening-prioritization "
            "experiment. If the contract is valid, choose one allowlisted "
            "method; do not require clinical endpoints, per-combination costs, "
            "or molecular structures. Report those as limitations, not blockers. "
            "Return none only if the contract is invalid or no allowlisted "
            "method can run on the listed fields."
        ),
    }
    problem_contract = {"problem": structured, "dataset": dataset_manifest,
                        "execution_contract": execution_contract,
                        "molecular_structure_available": molecular}
    problem_hash = sha256(canon(problem_contract))

    # 2. Three domain experts, independently, with the same problem contract.
    experts = {}
    for role in EXPERT_ROLES:
        result, exceeded = call_role(role, canon(problem_contract))
        if exceeded:
            return _fail(ledger, calls, state, "max_steps_exceeded", {"role": role})
        if result["status"] != "ok":
            return _fail(ledger, calls, state, result["error"], {"role": role})
        experts[role] = result["parsed"]
        state.setdefault("expert_problem_hashes", {})[role] = problem_hash
    user_direction = None
    if review_callback is not None:
        ledger.append("note", "executed", {"status": "review_ready",
                     "roles": list(EXPERT_ROLES)})
        review = review_callback({"problem": structured, "proposals": experts})
        if not isinstance(review, dict) or review.get("action") not in ("continue", "end"):
            review = {"action": "end"}
        if review["action"] == "end":
            ledger.append("note", "user-provided", {"status": "review_ended"})
            _record_influences(ledger, calls, state)
            return {"status": "review_ended", "state": state}
        user_direction = (review.get("direction") or "").strip() or None
        if user_direction:
            ledger.append("user_input", "user-provided", {
                "role": "review_gate", "direction": user_direction})
    if not molecular:
        ledger.append("note", "executed", {"domain": "chemistry", "testable": False,
                                           "reason": "molecular_structure_available is false"})
        state["chemistry_untestable"] = True
    else:
        state["chemistry_untestable"] = False

    # 3. Biology Validator reviews all three proposals.
    validator_input = {"problem_contract": problem_contract,
                       "proposals": {role: experts[role] for role in EXPERT_ROLES},
                       "chemistry_untestable": state["chemistry_untestable"]}
    if user_direction:
        validator_input["user_direction"] = user_direction
    result, exceeded = call_role("biology_validator", canon(validator_input))
    if exceeded:
        return _fail(ledger, calls, state, "max_steps_exceeded", {"role": "biology_validator"})
    if result["status"] != "ok":
        return _fail(ledger, calls, state, result["error"], {"role": "biology_validator"})
    validator = result["parsed"]
    reviews = validator["reviews"]
    # Validator review domains use the prompt's domain names
    # (mathematics/physics/chemistry), not the role names.
    if sorted(review["domain"] for review in reviews) != ["chemistry", "mathematics", "physics"]:
        return _fail(ledger, calls, state, "validator_must_review_all_three",
                     {"role": "biology_validator"})

    # 4. Transfer Judge selects one proposal or none.
    judge_input = {"problem_contract": problem_contract,
                   "proposals": {role: experts[role] for role in EXPERT_ROLES},
                   "validator_reviews": reviews,
                   "chemistry_untestable": state["chemistry_untestable"],
                   "execution_contract": execution_contract}
    if user_direction:
        judge_input["user_direction"] = user_direction
    result, exceeded = call_role("transfer_judge", canon(judge_input))
    if exceeded:
        return _fail(ledger, calls, state, "max_steps_exceeded", {"role": "transfer_judge"})
    if result["status"] != "ok":
        return _fail(ledger, calls, state, result["error"], {"role": "transfer_judge"})
    judge = result["parsed"]
    state["selected_domain"] = judge["selected_domain"]
    state["rejected_candidates"] = judge["rejected_candidates"]
    state["judge"] = judge

    # 5a. Judge none: no seal, no execution, critic not_executed.
    if judge["selected_domain"] == "none":
        if judge.get("missing_capability"):
            question = judge["missing_capability"]
            ledger.append("note", "model", {
                "status": "blocked_on_clarification", "question": question,
                "reason": "experts and judge found no testable plan"})
            _record_influences(ledger, calls, state)
            return {"status": "blocked_on_clarification", "question": question, "state": state}
        ledger.append("verdict", "model", {"classification": "not_executed", "reason": "no testable candidate"})
        state["final_verdict"] = "not_executed"
        state["critic_requested"] = None
        critic_input = {"sealed_plan": None, "executed_results": []}
        result, exceeded = call_role("critic", canon(critic_input))
        if exceeded:
            return _fail(ledger, calls, state, "max_steps_exceeded", {"role": "critic"})
        if result["status"] != "ok":
            return _fail(ledger, calls, state, result["error"], {"role": "critic"})
        critic = result["parsed"]
        if critic["classification"] != "not_executed":
            return _fail(ledger, calls, state, "critic_must_return_not_executed_when_none",
                         {"role": "critic"})
        state["critic_requested"] = "not_executed"
        reporter_input = {"final_verdict": "not_executed", "executed_results": [], "sealed_plan": None}
        result, exceeded = call_role("reporter", canon(reporter_input))
        if exceeded:
            return _fail(ledger, calls, state, "max_steps_exceeded", {"role": "reporter"})
        if result["status"] != "ok":
            return _fail(ledger, calls, state, result["error"], {"role": "reporter"})
        reporter = result["parsed"]
        if reporter["evidence_level"] != "not_executed":
            return _fail(ledger, calls, state, "reporter_evidence_level_mismatch", {"role": "reporter"})
        state["reporter_claims"] = reporter["claims"]
        _record_influences(ledger, calls, state)
        return {"status": "ok", "selected_domain": "none", "final_verdict": "not_executed",
                "critic": critic, "reporter": reporter, "state": state}

    # 5b. Chemistry untestable must never be selected.
    if state["chemistry_untestable"] and judge["selected_domain"] == "chemistry":
        return _fail(ledger, calls, state, "chemistry_untestable_selected", {"role": "transfer_judge"})

    # 5c. Required tools must be registered; unregistered tools stop the run.
    for tool_name in judge["required_tools"]:
        if tool_name not in tools.TOOLS:
            return _fail(ledger, calls, state, "unregistered_tool", {"role": "transfer_judge", "tool": tool_name})

    # 6. Seal the plan (engine operation, not a tool).
    split = build_split(split_ids, dataset_hash)
    sealed_plan = build_sealed_plan(judge, dataset_hash, split, split_ids.get("split_seed", 0),
                                    budget_fraction, budget_count, metric, baseline,
                                    confidence, minimum_effect)
    plan_errors = validate_sealed_plan(sealed_plan)
    if plan_errors:
        return _fail(ledger, calls, state, "invalid_sealed_plan", {"validation_errors": plan_errors})
    secret = seal_plan(ledger, sealed_plan)
    state["sealed_plan"] = sealed_plan
    state["seal_entry"] = secret["seal_entry"]
    state["seal_secret"] = secret

    # 7. Execute the sealed method. Live callers can provide the actual
    # deterministic runner; the placeholder remains available for demos.
    if execution_callback is not None:
        exec_status, executed_entries, executed_tools, exec_error, exec_tool = execution_callback(
            judge, sealed_plan, ledger)
    else:
        exec_status, executed_entries, executed_tools, exec_error, exec_tool = run_execution_placeholder(
            ledger, sealed_plan, judge["required_tools"], execution_calls)
    if exec_status != "ok":
        return _fail(ledger, calls, state, exec_error, {"role": "execution", "tool": exec_tool})
    state["executed_tools"] = executed_tools
    state["executed_entries"] = [entry["id"] for entry in executed_entries]

    # 8. Critic evaluates only sealed plan + executed results. Executed
    # results carry their ledger entry ids so the critic can cite them.
    executed_results = [{"entry_id": entry["id"], "tool": entry["data"]["tool"],
                         "result": entry["data"]["result"]} for entry in executed_entries]
    state["execution_results"] = executed_results
    critic_input = {"sealed_plan": sealed_plan, "executed_results": executed_results}
    result, exceeded = call_role("critic", canon(critic_input))
    if exceeded:
        return _fail(ledger, calls, state, "max_steps_exceeded", {"role": "critic"})
    if result["status"] != "ok":
        return _fail(ledger, calls, state, result["error"], {"role": "critic"})
    critic = result["parsed"]
    state["critic_requested"] = critic["classification"]
    # Audit numeric evidence against executed entries.
    numeric_claims = [{"value": item["value"], "ledger_entry": item["ledger_entry"]}
                      for item in critic["evidence"] if isinstance(item["value"], (int, float)) and not isinstance(item["value"], bool)]
    evidence_audit = audit_claims(numeric_claims, ledger)
    if not evidence_audit["ok"]:
        return _fail(ledger, calls, state, "critic_evidence_audit_failed",
                     {"role": "critic", "audit_errors": evidence_audit["errors"]})
    # Verdict ceiling, enforced by code.
    if verdict_input is None:
        for entry in executed_entries:
            output = entry["data"].get("result", {})
            if isinstance(output, dict) and "output" in output and isinstance(output["output"], dict):
                candidate = output["output"]
                if all(key in candidate for key in ("value", "ci_low", "ci_high", "baseline_value")):
                    verdict_input = candidate
                    break
    if verdict_input is not None:
        ceiling = compute_ceiling({"minimum_effect": minimum_effect}, verdict_input)
        verdict_result = enforce_verdict(critic["classification"], ceiling, ledger,
                                         evidence_ids=[item["ledger_entry"] for item in critic["evidence"]])
        final_verdict = verdict_result["final"]
    else:
        final_verdict = "not_executed" if critic["classification"] == "not_executed" else "inconclusive"
        ledger.append("verdict", "executed", {"requested": critic["classification"], "ceiling": final_verdict,
                                              "final": final_verdict, "lowered": final_verdict != critic["classification"]})
    state["final_verdict"] = final_verdict

    # 9. Reporter, with evidence_level copied from the final verdict.
    reporter_input = {"final_verdict": final_verdict, "executed_results": executed_results,
                      "sealed_plan": sealed_plan}
    result, exceeded = call_role("reporter", canon(reporter_input))
    if exceeded:
        return _fail(ledger, calls, state, "max_steps_exceeded", {"role": "reporter"})
    if result["status"] != "ok":
        return _fail(ledger, calls, state, result["error"], {"role": "reporter"})
    reporter = result["parsed"]
    if reporter["evidence_level"] != final_verdict:
        return _fail(ledger, calls, state, "reporter_evidence_level_mismatch", {"role": "reporter"})
    report_words = sum(len(reporter[key].split()) for key in ("summary", "what_we_tested", "what_happened"))
    if report_words >= 250:
        return _fail(ledger, calls, state, "reporter_too_long", {"role": "reporter"})
    report_audit = audit_reporter_claims(reporter["claims"], ledger)
    if not report_audit["ok"]:
        return _fail(ledger, calls, state, "reporter_audit_failed",
                     {"role": "reporter", "audit_errors": report_audit["errors"]})
    state["reporter_claims"] = reporter["claims"]
    _record_influences(ledger, calls, state)

    # 10. Rerun proposal: never auto-executed. See approve_rerun().
    rerun_proposal = check_rerun_proposal(critic, rerun_budget)
    return {"status": "ok", "selected_domain": judge["selected_domain"], "final_verdict": final_verdict,
            "sealed_plan": sealed_plan, "critic": critic, "reporter": reporter,
            "rerun_proposal": rerun_proposal, "state": state}


def check_rerun_proposal(critic, rerun_budget=None):
    """Check a critic rerun proposal without executing it.

    Returns {"approved": bool, "reason": str, "requested_calls": [...]}.
    A blocked request is not executed; the caller records it as
    blocked_requested_call if it chooses to record it.
    """
    if critic is None:
        return {"approved": False, "reason": "no_critic", "requested_calls": []}
    requested = critic.get("requested_calls", [])
    if critic.get("next_action") == "stop":
        return {"approved": False, "reason": "next_action_stop", "requested_calls": requested}
    if not requested:
        return {"approved": False, "reason": "no_requested_calls", "requested_calls": []}
    for call in requested:
        if call.get("tool") not in tools.TOOLS:
            return {"approved": False, "reason": "blocked_requested_call", "requested_calls": requested}
        error = tools.validate_args(call["tool"], call.get("arguments", {}))
        if error is not None:
            return {"approved": False, "reason": "blocked_requested_call", "requested_calls": requested}
    if rerun_budget is not None and rerun_budget.remaining <= 0:
        return {"approved": False, "reason": "rerun_budget_exhausted", "requested_calls": requested}
    return {"approved": True, "reason": "approved", "requested_calls": requested}


def approve_rerun(ledger, rerun_budget, previous_secret, new_payload):
    """Approve and create a follow-up experiment with a new seal.

    previous_secret is the dict returned by seal.seal() for the previous
    experiment (or None for a first follow-up with no previous seal on
    record). Returns {"status": "ok", "secret": ..., "entry": ...} or
    {"status": "blocked", "reason": ...}. The caller runs the new
    experiment; this function only creates the new seal and consumes budget,
    and records a note linking the previous and new seal entries.
    """
    if rerun_budget is not None and rerun_budget.remaining <= 0:
        ledger.append("note", "executed", {"status": "blocked_requested_call", "reason": "rerun_budget_exhausted"})
        return {"status": "blocked", "reason": "rerun_budget_exhausted"}
    if rerun_budget is not None:
        rerun_budget.consume()
    previous_entry = previous_secret.get("seal_entry") if isinstance(previous_secret, dict) else None
    secret = seal_plan(ledger, new_payload)
    if previous_entry is not None and secret["seal_entry"] == previous_entry:
        return {"status": "blocked", "reason": "rerun requires a new seal"}
    ledger.append("note", "executed", {"previous_seal_entry": previous_entry,
                                       "new_seal_entry": secret["seal_entry"]})
    return {"status": "ok", "secret": secret, "entry": secret["seal_entry"]}
