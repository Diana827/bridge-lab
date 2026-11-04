"""Execute one LIVE EXPLORATION computational test for a finished run.

Flow (Andrew's specification, 2026-10-29):
  Judge's selected method -> Python computation on TRAIN data only ->
  comparison against the random baseline at the fixed budget ->
  Critic judges the executed result (its classification is capped by
  the Python-computed verdict ceiling) -> Reporter writes the summary
  and the next research question.

Hidden labels are never read here: the computation is a pair-disjoint
holdout inside train_items.csv (see compute_test.py). This is NOT the
sealed comparison; it never touches the E0002 packet or its caller.

Everything is recorded in the test's own ledger
(server/tests/<test_id>.jsonl): a note with the plan, one tool_call
with the computed result, the critic/reporter model calls, and the
verdict entry written by enforce_verdict.
"""
import json
import os

from bridgelab.agent import run_role
from local_client import make_live_client
from bridgelab.ledger import Ledger, canon
from bridgelab.harness_v2 import load_role_prompts
from bridgelab.verdict import compute_ceiling, enforce_verdict

from compute_test_v2 import compute_method_test_v2 as compute_method_test

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS_DIR = os.path.join(ROOT, "server", "runs")
TESTS_DIR = os.path.join(ROOT, "server", "tests")
TRAIN_ITEMS = os.path.join(ROOT, "comparison-prereg-v4", "train_items.csv")
PROMPTS_DIR = os.path.join(ROOT, "prompts")


def _judge_plan(run_id):
    path = os.path.join(RUNS_DIR, run_id + ".jsonl")
    if not os.path.exists(path):
        return None
    plan = None
    for entry in Ledger(path).entries():
        data = entry.get("data") or {}
        if entry.get("kind") == "model_call" and data.get("role") == "transfer_judge":
            try:
                obj = json.loads(data.get("text") or "")
            except ValueError:
                continue
            plan = {"selected_method": obj.get("selected_method"),
                    "selected_domain": obj.get("selected_domain"),
                    "prediction": obj.get("prediction"),
                    "falsifier": obj.get("falsifier")}
    return plan


def execute_test(rec, run_id):
    test_id = rec["id"]
    try:
        plan = _judge_plan(run_id)
        if not plan or not plan.get("selected_method"):
            rec["status"] = "failed"
            rec["result"] = {"status": "failed", "error": "no_judge_selection",
                             "failed_role": "transfer_judge"}
            return
        os.makedirs(TESTS_DIR, exist_ok=True)
        ledger = Ledger(os.path.join(TESTS_DIR, test_id + ".jsonl"))
        ledger.append("note", "executed",
                      {"mode": "LIVE EXPLORATION", "source_run": run_id,
                       "plan": plan, "hidden_labels_read": False})
        method = plan["selected_method"]
        if method not in {"drug_mean", "exact_pair_mean", "pair_frequency", "drug_cell_mean", "structure_cell_knn"}:
            rec["status"] = "failed"
            rec["result"] = {"status": "failed",
                             "error": "unsupported_exploration_method",
                             "failed_role": "execution", "plan": plan}
            return

        result = compute_method_test(TRAIN_ITEMS, method)
        tool_entry = ledger.append(
            "tool_call", "executed",
            {"tool": "train_holdout_evaluation", "status": "ok",
             "result": {"status": "ok", "output": result}})
        ceiling = compute_ceiling(
            {"minimum_effect": 0.0},
            {"value": result["difference"],
             "ci_low": result["difference_ci95"][0],
             "ci_high": result["difference_ci95"][1],
             "baseline_value": 0.0})

        client = make_live_client(timeout=180)
        prompts = load_role_prompts(PROMPTS_DIR)

        critic_input = {
            "plan": plan,
            "executed_results": [{"ledger_entry": tool_entry, "tool": "train_holdout_evaluation",
                                  "output": result}],
            "instruction": ("Judge only the executed result above. Cite its numbers "
                            "with the ledger entry id. The falsifier in the plan is "
                            "the success criterion."),
        }
        critic = run_role(client, ledger, "critic", canon(critic_input),
                          system=prompts["critic"])
        if critic.get("status") != "ok":
            rec["status"] = "failed"
            rec["result"] = {"status": "failed", "error": critic.get("error"),
                             "failed_role": "critic", "plan": plan,
                             "computation": result, "ceiling": ceiling}
            return
        classification = critic["parsed"]["classification"]
        outcome = enforce_verdict(classification, ceiling, ledger,
                                  evidence_ids=[tool_entry])

        reporter_input = {
            "final_verdict": outcome["final"],
            "evidence_level": outcome["final"],
            "executed_results": [{"ledger_entry": tool_entry, "output": result}],
            "sealed_plan": plan,
            "instruction": ("Write the report for this LIVE EXPLORATION test. "
                            "most_important_unresolved_question must be the next "
                            "research question this result raises."),
            "output_contract": (
                "Return ONE JSON object only: no code fences, no prose outside "
                "the JSON. The claims field is REQUIRED and must be an array of "
                "objects, each exactly {\"text\": <string>, \"label\": <one of "
                "\"user_provided\", \"retrieved\", \"model_generated_hypothesis\", "
                "\"executed_result\">, \"ledger_entries\": [<ledger entry ids>]}. "
                "Do not use a \"statement\" field; the field is named \"text\". "
                "Cite the executed result as \"%s\" in ledger_entries." % tool_entry),
        }
        reporter = run_role(client, ledger, "reporter", canon(reporter_input),
                            system=prompts["reporter"])
        if reporter.get("status") != "ok":
            rec["status"] = "failed"
            rec["result"] = {"status": "failed", "error": reporter.get("error"),
                             "failed_role": "reporter", "plan": plan,
                             "computation": result, "ceiling": ceiling,
                             "critic_classification": classification,
                             "final_verdict": outcome["final"]}
            return
        rep = reporter["parsed"]
        rec["status"] = "completed"
        rec["result"] = {
            "status": "ok", "plan": plan, "computation": result,
            "ceiling": ceiling, "critic_classification": classification,
            "final_verdict": outcome["final"],
            "verdict_lowered": outcome["lowered"],
            "reporter": {"summary": rep.get("summary"),
                         "what_happened": rep.get("what_happened"),
                         "limits": rep.get("limits"),
                         "next_question": rep.get("most_important_unresolved_question"),
                         "recommended_next_action": rep.get("recommended_next_action")},
        }
    except Exception as exc:  # sanitized: type only
        rec["status"] = "failed"
        rec["result"] = {"status": "failed", "error": type(exc).__name__}
