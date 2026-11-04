"""Create the E0003 comparison seal for the v4 packet ONLY (no arms, no labels).

Modeled on scripts/create_comparison_seal_v3.py (which created E0002 for
the v3 packet), retargeted to comparison-prereg-v4/. The v4 packet binds
the Reporter output-contract repair: prompts/reporter_v2.md and the
one-line ROLE_TO_PROMPT_FILE change in bridgelab/orchestration.py. The
seal payload additionally binds the Reporter v2 prompt hash and the v4
execution-code combined hash explicitly, on top of the v3 payload keys.

Preconditions, all asserted before anything is written:
  - comparison-prereg-v4/preregistration_v4.json exists with packet_hash,
    harness combined hash, execution code combined hash, and Reporter v2
    prompt hash exactly the values Diana approved.
  - seal_status is NOT_CREATED and arms_run is False.
  - No v4 seal ledger / secret / record exists yet (refuses otherwise:
    a second seal must never silently replace the first).
  - No packet field is typed by hand beyond the approved hash constants:
    the payload values are read programmatically from
    preregistration_v4.json.
  - preregistration_v4.json and packet_manifest_v4.json are NOT edited
    (editing would break the packet hash; seal status lives in the
    external seal record, as with E0001 and E0002).
  - Earlier packets and seals are not touched: v1/E0001, v2 draft, and
    v3/E0002 remain preserved historical records.

The seal uses bridgelab.seal.seal(): only the commit hash
sha256(canon(payload) + "|" + nonce) is written to the ledger. The
payload + nonce live in seal_secret.json for a future single reveal;
the nonce is NEVER printed. Nothing here reads the raw archive, hidden
candidate/result data, or any hidden label, and nothing runs either arm.

Outputs:
  comparison-prereg-v4/seal_ledger.jsonl   (one seal entry)
  comparison-prereg-v4/seal_secret.json    (secret: seal_entry, payload, nonce)
  comparison-prereg-v4/seal_record_v4.json (public record: seal_id E0003,
                                            seal hash, packet hash,
                                            harness/code hashes, verification)
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridgelab.ledger import Ledger
from bridgelab import seal as seal_module

PREREG = "comparison-prereg-v4"
APPROVED_PACKET_HASH = "478d0edbe60150de11e4e364d64312fc979c4b9b818ab9390d837b2d877409e5"
APPROVED_HARNESS_HASH = "9d34668eb547ef71c855b9d4b46d28fa6d8b71134d02d136aa61a69db3042ce7"
APPROVED_CODE_HASH = "e7337ad3c610e2ca02827a2b3c6de1278ed1ffb41c094d09a0a640ef2df73ffb"
APPROVED_REPORTER_HASH = "a323b9ba41cb94ed1df26dc4ee8bcdb6dc4677550bb8cb3bfae51ffa8df46639"
SEAL_ID = "E0003"
LEDGER_PATH = f"{PREREG}/seal_ledger.jsonl"
SECRET_PATH = f"{PREREG}/seal_secret.json"
RECORD_PATH = f"{PREREG}/seal_record_v4.json"


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    if os.path.exists(LEDGER_PATH):
        raise SystemExit("refusing: v4 seal ledger already exists; a second seal must not replace the first")
    if os.path.exists(SECRET_PATH) or os.path.exists(RECORD_PATH):
        raise SystemExit("refusing: v4 seal secret/record already exists")
    with open(f"{PREREG}/preregistration_v4.json") as f:
        prereg = json.load(f)
    assert prereg["packet_hash"] == APPROVED_PACKET_HASH, "packet hash does not match the approved value"
    assert prereg["hashes"]["harness_prompts_combined_sha256"] == APPROVED_HARNESS_HASH, "harness hash mismatch"
    assert prereg["pipeline_code_supplementary"]["combined_sha256"] == APPROVED_CODE_HASH, "execution code hash mismatch"
    assert prereg["harness_prompts"]["file_hashes"]["reporter_v2.md"] == APPROVED_REPORTER_HASH, "reporter prompt hash mismatch"
    assert sha256_file("prompts/reporter_v2.md") == APPROVED_REPORTER_HASH, "actual reporter_v2.md hash mismatch"
    assert prereg["seal_status"] == "NOT_CREATED", "prereg already records a seal"
    assert prereg["arms_run"] is False, "arms already recorded as run"
    assert prereg["proposed_seal"].startswith("E0003 (PROPOSED"), "unexpected proposed seal"

    # Payload built ONLY from frozen prereg fields (approved hash
    # constants above are assertions, not payload values).
    h = prereg["hashes"]
    ep = prereg["execution_parameters"]
    model_field = prereg["model"]
    model_id = model_field["identifier"] if isinstance(model_field, dict) else model_field
    payload = {
        "seal_purpose": "comparison preregistration v4: Baseline vs Harness, sealed comparison; Reporter output contract aligned to validator",
        "seal_id": SEAL_ID,
        "packet_hash": prereg["packet_hash"],
        "python_seed": prereg["python_seed"],
        "budgets": prereg["budgets"]["counts"],
        "primary_metric": prereg["analysis_rules"]["primary"],
        "hit_rule": prereg["metric_and_threshold"]["hit_rule"],
        "borderline_rule": prereg["metric_and_threshold"]["borderline"],
        "primary_analysis": prereg["failure_handling"]["primary_scoring"],
        "judge_none": prereg["failure_handling"]["empty_or_declined_selection"],
        "superiority_gates": prereg["analysis_rules"]["claim_gate"],
        "model": model_id,
        "model_seed": "unavailable; must be reported as unavailable",
        "token_cost": "record if available, else MISSING; never estimated",
        "hidden_labels": "remain unread until both arms finish",
        "dataset_hash": h["dataset_raw_sha256"],
        "pool_hash": h["pool_hash"],
        "split_hash": h["split_hash"],
        "train_items_sha256": h["train_items_sha256"],
        "hidden_candidates_sha256": h["hidden_candidates_sha256"],
        "baseline_prompt_sha256": h["baseline_prompt_sha256"],
        "harness_prompts_combined_sha256": h["harness_prompts_combined_sha256"],
        "reporter_prompt_sha256": prereg["harness_prompts"]["file_hashes"]["reporter_v2.md"],
        "execution_code_combined_sha256": prereg["pipeline_code_supplementary"]["combined_sha256"],
        "execution_pipeline": ep["pipeline"],
        "problem_prompt_sha256": ep["problem_prompt"]["sha256"],
        "dataset_manifest_sha256": ep["dataset_manifest"]["sha256"],
        "split_ids_sha256": ep["split_ids"]["sha256"],
    }

    ledger = Ledger(LEDGER_PATH)
    secret = seal_module.seal(ledger, payload)
    entry = ledger.get(secret["seal_entry"])
    assert entry is not None and entry["kind"] == "seal", "seal entry not found in ledger"
    assert entry["data"]["sha256"] == seal_module.commit_hash(payload, secret["nonce"]), "seal hash mismatch"
    ok, n_entries, first_bad = ledger.verify()
    assert ok and n_entries == 1 and first_bad is None, f"ledger verify failed: {ok} {n_entries} {first_bad}"

    with open(SECRET_PATH, "w") as f:
        json.dump(secret, f, indent=1, sort_keys=True)
    record = {
        "seal_id": SEAL_ID,
        "seal_entry_id": secret["seal_entry"],
        "seal_hash": entry["data"]["sha256"],
        "seal_entry_ledger_hash": entry["hash"],
        "packet_hash": prereg["packet_hash"],
        "harness_prompts_combined_sha256": APPROVED_HARNESS_HASH,
        "execution_code_combined_sha256": APPROVED_CODE_HASH,
        "reporter_prompt_sha256": APPROVED_REPORTER_HASH,
        "ledger_file": LEDGER_PATH,
        "ledger_entries": n_entries,
        "ledger_verify": ok,
        "seal_hash_recomputed": True,
        "packet_fields_changed": False,
        "arms_run": False,
        "hidden_labels_read": False,
        "note": ("Only the commit hash is in the ledger. The payload + nonce live "
                 "in seal_secret.json for a future single reveal; the nonce is not "
                 "printed here or in chat. This seal binds the v4 comparison packet "
                 "(Reporter v2 contract version, including its execution code hash); "
                 "it is not an experiment seal (no hypothesis has been selected; "
                 "no arm has run). E0002 and the v1/v2/v3 packets are preserved "
                 "historical records and were not modified."),
    }
    with open(RECORD_PATH, "w") as f:
        json.dump(record, f, indent=1, sort_keys=True)
    print(json.dumps(record, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
