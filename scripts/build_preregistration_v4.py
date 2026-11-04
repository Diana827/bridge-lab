"""Build the comparison pre-registration v4 DRAFT packet (NOT a seal).

v4 exists for exactly one reason: the Reporter output contract. Live
exploration on 2026-10-29 showed the live Reporter repeatedly failing
validation because prompts/reporter.md never specified the `claims`
field that bridgelab.validation._validate_reporter requires (the model
improvised claims as strings or {statement, hyphenated label} objects).
v4 swaps in prompts/reporter_v2.md, which states the validator's schema
exactly (fields, claims shape {text, label, ledger_entries}, the label
enum, no-fence rule), and flips one line of
bridgelab/orchestration.py (ROLE_TO_PROMPT_FILE: reporter -> reporter_v2.md).

Everything else is byte-identical to the v3 packet sealed by E0002:
same dataset files, split, budgets, baseline prompt, problem prompt,
primary metric, and selection logic. The impact audit in the record
carries the file-level evidence.

This script READS the v3 packet and the E0002 seal record; it never
modifies them. It creates NO seal: the v4 record carries seal_status
NOT_CREATED and proposed_seal "E0003 (PROPOSED ONLY, NOT CREATED)".
A seal is created only on the user's explicit approval, by a separate
script, after review.

Packet hash construction (same as v1/v2/v3): SHA-256 over
train_file_hash + "\\n" + hidden_file_hash + "\\n" +
baseline_prompt_hash + "\\n" + harness_combined_hash + "\\n" +
canonical sorted JSON of the record without the packet_hash field.
Harness combined hash: SHA-256 over, in pipeline order, each
(filename + "\\n" + file bytes + "\\n"). Pipeline code hash: same
construction over bridgelab/*.py sorted by filename (supplementary).
"""
import copy
import hashlib
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
V3 = os.path.join(ROOT, "comparison-prereg-v3")
V4 = os.path.join(ROOT, "comparison-prereg-v4")
PROMPT_ORDER_V4 = ["problem_structurer.md", "mathematician.md", "physicist.md",
                   "chemist.md", "biology_validator.md", "transfer_judge_v2.md",
                   "critic_v2.md", "reporter_v2.md"]
COPIED_FILES = ("train_items.csv", "hidden_candidates.csv", "baseline_prompt.md",
                "problem_prompt.md", "dataset_manifest.json", "split_ids.json")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def combined_hash(paths):
    h = hashlib.sha256()
    for p in paths:
        h.update(os.path.basename(p).encode())
        h.update(b"\n")
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        h.update(b"\n")
    return h.hexdigest()


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def main():
    if os.path.exists(os.path.join(V4, "preregistration_v4.json")):
        raise SystemExit("refusing: v4 preregistration already exists")
    with open(os.path.join(V3, "preregistration_v3.json")) as f:
        prereg_v3 = json.load(f)
    with open(os.path.join(V3, "packet_manifest_v3.json")) as f:
        manifest_v3 = json.load(f)
    with open(os.path.join(V3, "seal_record_v3.json")) as f:
        seal_v3 = json.load(f)
    assert seal_v3["seal_id"] == "E0002" and seal_v3["packet_hash"] == prereg_v3["packet_hash"]

    os.makedirs(V4, exist_ok=True)
    for name in COPIED_FILES:
        shutil.copyfile(os.path.join(V3, name), os.path.join(V4, name))
    train_hash = sha256_file(os.path.join(V4, "train_items.csv"))
    hidden_hash = sha256_file(os.path.join(V4, "hidden_candidates.csv"))
    baseline_hash = sha256_file(os.path.join(V4, "baseline_prompt.md"))
    assert train_hash == prereg_v3["hashes"]["train_items_sha256"]
    assert hidden_hash == prereg_v3["hashes"]["hidden_candidates_sha256"]
    assert baseline_hash == prereg_v3["hashes"]["baseline_prompt_sha256"]
    for name in ("problem_prompt.md", "dataset_manifest.json", "split_ids.json"):
        assert sha256_file(os.path.join(V4, name)) == sha256_file(os.path.join(V3, name)), name

    # Harness prompts: only the Reporter file changes.
    prompt_hashes = {n: sha256_file(os.path.join(ROOT, "prompts", n)) for n in PROMPT_ORDER_V4}
    v3_hashes = prereg_v3["harness_prompts"]["file_hashes"]
    for name in PROMPT_ORDER_V4[:-1]:
        assert prompt_hashes[name] == v3_hashes[name], "unexpected prompt change: %s" % name
    assert prompt_hashes["reporter_v2.md"] != v3_hashes["reporter.md"]
    harness_hash = combined_hash([os.path.join(ROOT, "prompts", n) for n in PROMPT_ORDER_V4])
    assert harness_hash != prereg_v3["harness_prompts"]["combined_sha256"]

    # Pipeline code: prove the ONLY difference from the v3 execution
    # version is the one-line prompt map change — hashing the current
    # file set with that line reverted must reproduce the v3 record's
    # combined code hash exactly.
    code_files = sorted(os.path.join(ROOT, "bridgelab", n)
                        for n in os.listdir(os.path.join(ROOT, "bridgelab"))
                        if n.endswith(".py"))
    code_hash = combined_hash(code_files)
    v3_supp = prereg_v3["pipeline_code_supplementary"]
    assert [os.path.basename(p) for p in code_files] == v3_supp["files"]

    def combined_from_bytes(pairs):
        h = hashlib.sha256()
        for name, data in pairs:
            h.update(name.encode())
            h.update(b"\n")
            h.update(data)
            h.update(b"\n")
        return h.hexdigest()

    pairs = []
    for path in code_files:
        with open(path, "rb") as f:
            data = f.read()
        if os.path.basename(path) == "orchestration.py":
            needle = b'"reporter": "reporter_v2.md"'
            assert data.count(needle) == 1
            data = data.replace(needle, b'"reporter": "reporter.md"')
        pairs.append((os.path.basename(path), data))
    reverted_hash = combined_from_bytes(pairs)
    assert reverted_hash == v3_supp["combined_sha256"], (
        "reverting the reporter line does not reproduce the v3 code hash: "
        "some other code change is present")
    code_file_records = [{"name": os.path.basename(p), "sha256": sha256_file(p)}
                         for p in code_files]

    record = copy.deepcopy(prereg_v3)
    record["preregistration_version"] = "comparison-prereg v4 DRAFT, 2026-10-29 (NOT SEALED)"
    record["status"] = "DRAFT_FOR_REVIEW"
    record["seal_status"] = "NOT_CREATED"
    record["arms_run"] = False
    record["proposed_seal"] = "E0003 (PROPOSED ONLY, NOT CREATED)"
    record["hashes"]["harness_prompts_combined_sha256"] = harness_hash
    record["harness_prompts"] = {
        "order": PROMPT_ORDER_V4,
        "file_hashes": prompt_hashes,
        "combined_sha256": harness_hash,
        "combined_construction": prereg_v3["harness_prompts"]["combined_construction"],
        "change_note": ("Reporter prompt swapped reporter.md -> reporter_v2.md: the v2 "
                        "prompt states the validator's exact output contract (the "
                        "claims field the v1 prompt never specified). All other "
                        "prompt files are byte-identical to v3."),
        "change_vs_v3": "reporter.md replaced by reporter_v2.md; no other prompt changed.",
    }
    record["pipeline_code_supplementary"] = {
        "files": [os.path.basename(p) for p in code_files],
        "combined_sha256": code_hash,
        "note": ("Supplementary. One line differs from the v3 execution version: "
                 "bridgelab/orchestration.py ROLE_TO_PROMPT_FILE['reporter'] "
                 "reporter.md -> reporter_v2.md. Proof: hashing the current file "
                 "set with that line reverted reproduces the v3 combined code "
                 "hash exactly (asserted by the builder). No scoring, selection, "
                 "metric, or validation code changed."),
    }
    record["changes_vs_v3"] = {
        "reason": ("Live exploration (2026-10-29) found the Reporter prompt and the "
                   "validator disagree: bridgelab.validation._validate_reporter "
                   "requires a claims array of {text, label, ledger_entries} objects "
                   "with label in REPORTER_LABELS, but prompts/reporter.md's output "
                   "template has no claims field. The live Reporter failed validation "
                   "in every full live run (claims written as strings or as "
                   "{statement, 'model-generated hypothesis'} objects; one attempt "
                   "also wrapped the JSON in Markdown fences, which parse_json_strict "
                   "rejects)."),
        "changed": [
            "prompts/reporter_v2.md (new): states the validator's schema exactly — "
            "all eight required fields, the claims object shape, the label enum "
            "verbatim, executed_result claims may cite only numbers present in the "
            "cited executed tool_call entries, no invented entry ids, bare JSON only.",
            "bridgelab/orchestration.py (one line): ROLE_TO_PROMPT_FILE['reporter'] "
            "reporter.md -> reporter_v2.md.",
            "tests/test_reporter_contract.py (new): 18 contract tests pinning "
            "prompt<->validator consistency, audit rejections, retry semantics, "
            "and Reporter-failure score isolation.",
        ],
        "unchanged": [
            "primary metric (precision at the 10% budget, ITT) and the claim gate",
            "selection logic (Transfer Judge + decision compiler) — no code or "
            "prompt change",
            "dataset files, split, budgets, baseline prompt, problem prompt — "
            "byte-identical copies of the v3 packet files",
            "validator and audit code (bridgelab/validation.py, bridgelab/audit.py) "
            "— byte-identical; the prompt was aligned TO the validator, not the "
            "other way around",
        ],
    }
    record["impact_audit"] = {
        "primary_metric_changed": False,
        "selection_logic_changed": False,
        "evidence": [
            "File-level: every v4 packet input file except the harness prompt set "
            "hashes identically to v3 (asserted by the builder).",
            "Code-level: hashing the current bridgelab/*.py set with the single "
            "ROLE_TO_PROMPT_FILE reporter line reverted reproduces the v3 combined "
            "code hash exactly (asserted in scripts/build_preregistration_v4.py), "
            "so the reporter line is the only code difference.",
            "Position: the Reporter runs after execution and verdict. The "
            "comparison caller reads the Judge from the pipeline state even when "
            "a run fails at the Reporter (scripts/run_live_comparison.py), and "
            "tests/test_reporter_contract.py proves the Judge artifact is "
            "identical with a healthy and an always-failing Reporter. The "
            "Reporter therefore cannot change the selection the driver scores.",
            "Validation: 273 tests pass (255 prior + 18 new contract tests); the "
            "fake pipeline smoke and the fake comparison verification pass "
            "unchanged; one live Reporter call with reporter_v2.md passed "
            "validation on the first attempt with its claims audit ok.",
        ],
    }
    record["reporter_v2"] = {
        "file": "prompts/reporter_v2.md",
        "sha256": prompt_hashes["reporter_v2.md"],
        "validator": "bridgelab/validation.py _validate_reporter + REPORTER_LABELS; "
                     "claims audited by bridgelab/audit.py audit_reporter_claims",
        "contract_tests": "tests/test_reporter_contract.py (18 tests, all passing)",
        "live_single_call": "PASS 2026-10-29: first attempt valid; evidence_level "
                            "copied; executed_result claim cited the executed entry "
                            "and only its numbers; claims audit ok.",
    }
    record["readiness_for_sealing"] = {
        "conditions": [
            "User reviews this packet as displayed.",
            "No field changes after seal creation; any pre-seal change requires "
            "re-freeze, new hash, new review display.",
            "The E0003 seal is created only on the user's explicit approval.",
        ],
        "not_ready_for_running": [
            "E0003 has not been created; no arm may run under v4 until it exists.",
        ],
    }
    record.pop("packet_hash", None)
    packet_hash = hashlib.sha256(
        (train_hash + "\n" + hidden_hash + "\n" + baseline_hash + "\n"
         + harness_hash + "\n" + canon(record)).encode("utf-8")).hexdigest()
    record["packet_hash"] = packet_hash

    with open(os.path.join(V4, "preregistration_v4.json"), "w") as f:
        json.dump(record, f, indent=1, sort_keys=True)
    manifest = {
        "packet": "comparison-prereg-v4",
        "seal_status": "NOT_CREATED",
        "proposed_seal": "E0003 (PROPOSED ONLY, NOT CREATED)",
        "arms_run": False,
        "inputs_copied_byte_identical_from_v3": list(COPIED_FILES),
        "train_items": manifest_v3["train_items"],
        "train_items_sha256": train_hash,
        "hidden_candidates": manifest_v3["hidden_candidates"],
        "hidden_candidates_sha256": hidden_hash,
        "baseline_prompt_sha256": baseline_hash,
        "problem_prompt_sha256": sha256_file(os.path.join(V4, "problem_prompt.md")),
        "dataset_manifest_sha256": sha256_file(os.path.join(V4, "dataset_manifest.json")),
        "split_ids_sha256": sha256_file(os.path.join(V4, "split_ids.json")),
        "split": manifest_v3["split"],
        "split_ids": manifest_v3["split_ids"],
        "budgets_floor": manifest_v3["budgets_floor"],
        "eligible_groups": manifest_v3["eligible_groups"],
        "harness_prompts_order": PROMPT_ORDER_V4,
        "harness_prompts_file_hashes": prompt_hashes,
        "harness_prompts_combined_sha256": harness_hash,
        "pipeline_code_supplementary_combined_sha256": code_hash,
        "prompt_changes": "reporter.md -> reporter_v2.md only (Reporter output "
                          "contract aligned to the validator).",
        "packet_hash": packet_hash,
        "supersedes_packet": "comparison-prereg-v3 (sealed by E0002, preserved untouched)",
    }
    with open(os.path.join(V4, "packet_manifest_v4.json"), "w") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)

    print("reporter_v2 prompt:", prompt_hashes["reporter_v2.md"])
    print("harness combined v4:", harness_hash)
    print("code combined v4:  ", code_hash)
    print("packet hash v4 (DRAFT, NOT SEALED):", packet_hash)
    return 0


if __name__ == "__main__":
    sys.exit(main())
