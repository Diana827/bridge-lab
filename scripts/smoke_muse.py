"""Smoke run for BRIDGE LAB, fake client only (Stage 1).

Usage, from the bridge-lab directory:
    python3 scripts/smoke_muse.py --fake

This script never calls a live API and never reads an API key. The
client below is a scripted fake client, labelled fake-scripted, and
its output must never be presented as Meta output. Without --fake the
script refuses to run: live Meta validation remains blocked by billing
in Stage 1, and no live client is created or changed here.
"""
from __future__ import annotations
import argparse, copy, json, os, sys, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from bridgelab.ledger import Ledger
from bridgelab.orchestration import load_role_prompts, run_pipeline

ROLE_ORDER = ["problem_structurer", "mathematician", "physicist", "chemist",
              "biology_validator", "transfer_judge", "critic", "reporter"]


def _structurer():
    return {"objective": "Find high-scoring pairs under a budget.", "inputs": ["pairs"],
            "available_evidence": ["scores"], "desired_output": "ranked pairs",
            "constraints": ["budget"], "controllable_variables": ["selection"],
            "observable_outcomes": ["hit rate"], "unknowns": [], "data_requirements": ["scores"],
            "success_definition": "Beat random.", "failure_definition": "No improvement.",
            "problem_structure": "search_and_prioritization", "assumptions": [], "clarification": None}


def _expert(domain, principle):
    base = {"domain": domain, "principle": principle, "original_problem_solved": "O",
            "mapping_to_biology": "M", "mechanism": "Mech", "required_assumptions": ["A"],
            "testable_prediction": "Pred", "falsifier": "F", "minimum_experiment": "Exp",
            "baseline": "random", "required_data": ["scores"], "risks_of_misleading": ["R"],
            "confidence_in_transfer": "medium"}
    if domain == "physics":
        base["state_variables"] = ["v"]
        base["interaction_or_feedback"] = "I"
    if domain == "chemistry":
        base["structural_features_needed"] = ["scaffold"]
    return base


def _validator():
    reviews = []
    for domain, fit, verdict, reason in (
            ("mathematics", "high", "keep", "Good."),
            ("physics", "medium", "revise", "Weaker."),
            ("chemistry", "low", "reject", "No structure data.")):
        reviews.append({"domain": domain, "biological_fit": fit, "missing_biological_context": [],
                        "confounders": [], "assay_or_dataset_limitations": [], "required_controls": [],
                        "falsification_test": "T", "verdict": verdict, "reason": reason})
    return {"reviews": reviews}


def _judge():
    scores = []
    for domain, fit in (("mathematics", 0.9), ("physics", 0.5), ("chemistry", 0.1)):
        scores.append({"domain": domain, "structural_fit": fit, "mechanism_quality": fit,
                       "testability": fit, "data_availability": fit, "feasibility": fit,
                       "misleading_risk": 0.2, "score_explanation": "Score."})
    return {"candidate_scores": scores, "selected_domain": "mathematics",
            "selected_principle": "MATH-PRINCIPLE-123", "biological_hypothesis": "Hypothesis H",
            "mechanism": "Mech", "prediction": "Pred", "falsifier": "F",
            "minimum_experiment": "Exp", "fair_baselines": ["random"],
            "required_tools": ["summarize_numbers"],
            "rejected_candidates": [{"domain": "physics", "reason": "Not selected."},
                                    {"domain": "chemistry", "reason": "No structure data."}],
            "missing_capability": None,
            "selected_method": "drug_mean", "method_parameters": {},
            "required_data_fields": ["item_id", "nsc1", "nsc2", "observed_score"]}


def _critic(messages):
    data = json.loads(messages[1]["content"])
    executed = data.get("executed_results", [])
    if not executed:
        return json.dumps({"classification": "not_executed", "failure_type": "none", "evidence": [],
                           "validity_conditions": "No execution.", "contradictory_evidence": [],
                           "next_action": "stop", "next_action_rationale": "Rationale.",
                           "requested_calls": [], "unresolved_question": "None."})
    entry = executed[0]
    mean = entry["result"]["output"]["mean"]
    return json.dumps({"classification": "supported", "failure_type": "none",
                       "evidence": [{"claim": "mean is %s" % mean, "ledger_entry": entry["entry_id"], "value": mean}],
                       "validity_conditions": "Executed entry present.", "contradictory_evidence": [],
                       "next_action": "stop", "next_action_rationale": "Rationale.",
                       "requested_calls": [], "unresolved_question": "None."})


def _reporter(messages):
    data = json.loads(messages[1]["content"])
    executed = data.get("executed_results", [])
    level = data.get("final_verdict", "supported")
    claims = []
    if executed:
        entry = executed[0]
        mean = entry["result"]["output"]["mean"]
        claims = [{"text": "mean is %s" % mean, "label": "executed_result",
                   "ledger_entries": [entry["entry_id"]]}]
    return json.dumps({"summary": "Summary text.", "what_we_tested": "What we tested.",
                       "what_happened": "What happened.", "evidence_level": level,
                       "limits": ["One limit."], "most_important_unresolved_question": "Next question.",
                       "recommended_next_action": "Next action.", "claims": claims})


class FakeRoleClient:
    # Use the production model id for schema/path validation while keeping
    # provenance explicitly fake-scripted. No network call is made.
    model = "muse-spark-1.3"
    provenance = "fake-scripted"

    def __init__(self):
        self.responses = {
            "problem_structurer": [json.dumps(_structurer())],
            "mathematician": [json.dumps(_expert("mathematics", "MATH-PRINCIPLE-123"))],
            "physicist": [json.dumps(_expert("physics", "PHYS-PRINCIPLE-456"))],
            "chemist": [json.dumps(_expert("chemistry", "CHEM-PRINCIPLE-789"))],
            "biology_validator": [json.dumps(_validator())],
            "transfer_judge": [json.dumps(_judge())],
            "critic": [_critic],
            "reporter": [_reporter],
        }
        self._pos = 0
        self._current = None

    def chat(self, messages):
        last = messages[-1].get("content", "") if messages else ""
        if last.startswith("The previous output was not valid"):
            role = self._current
        else:
            role = ROLE_ORDER[self._pos]
            self._pos += 1
            self._current = role
        response = self.responses[role][0]
        text = response(messages) if callable(response) else response
        return {"model": self.model, "choices": [{"message": {"role": "assistant", "content": text}}], "usage": None}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--fake", action="store_true", help="Run with the fake scripted client only.")
    args = parser.parse_args(argv)
    if not args.fake:
        print("smoke_muse: live mode is not enabled in Stage 1. Use --fake. "
              "Live Meta validation remains blocked by billing.", file=sys.stderr)
        return 2
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    prompts = load_role_prompts(os.path.join(root, "prompts"))
    ledger = Ledger(os.path.join(tempfile.mkdtemp(), "smoke.jsonl"))
    manifest = {"dataset_id": "nci_almanac_combo", "molecular_structure_available": False,
                "structure_source": None, "structure_field": None, "structure_file_hash": None}
    split_ids = {"train_ids": ["pair_%03d" % i for i in range(300)],
                 "hidden_ids": ["pair_%03d" % i for i in range(300, 400)],
                 "start_set_ids": ["pair_001"], "split_seed": 42, "split_method": "fixed_random_split"}
    result = run_pipeline(FakeRoleClient(), ledger, "Find pairs.", prompts, manifest, split_ids,
                          metric="hit_rate", baseline="random", confidence=0.95, minimum_effect=0.1,
                          budget_fraction=0.05, total_items=400,
                          execution_calls=[{"tool": "summarize_numbers",
                                            "arguments": {"values": [1, 2, 3], "budget_count": 20}}],
                          verdict_input={"value": 0.5, "ci_low": 0.4, "ci_high": 0.6, "baseline_value": 0.2})
    ok, n_entries, bad = ledger.verify()
    print("fake client: fake-scripted (not Meta output)")
    print("pipeline status: %s" % result["status"])
    print("selected_domain: %s" % result.get("selected_domain"))
    print("final_verdict: %s" % result.get("final_verdict"))
    print("ledger entries: %d, verify: %s, first_bad: %s" % (n_entries, ok, bad))
    if result["status"] != "ok" or not ok:
        print("smoke FAILED: %s" % result, file=sys.stderr)
        return 1
    print("smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
