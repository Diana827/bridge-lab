"""Live comparison caller (scripts/run_live_comparison.py).

Runs a full two-arm comparison through comparison_driver and saves
ONE artifact: the execution manifest + the driver report + the full
run ledger, in a single JSON file. No separate ledger file is left
behind (the temporary ledger is embedded and then removed).

Modes (explicit --mode is required; fake and live are never mixed):
  fake: synthetic small data, scripted FakeClient, scripted Judge
    outputs, synthetic labels. Never reads a packet file, the raw
    archive, or any real label. Verifies wiring, the gate, the
    manifest shape, and the log scrub. Fake budgets are small and
    fake-only; they are NOT the frozen budgets.
  live: reads the packet (train/hidden/baseline prompt) from
    --packet-dir, runs the live Muse pipeline per run index (the
    pipeline loads transfer_judge_v2.md natively),
    extracts each index's Judge output from the pipeline result
    state, runs Baseline + Harness through the driver with the same
    inputs, budgets, and run indices, and opens hidden labels only
    through the driver's HiddenLabelGate after both arms finish.
    Live mode is REFUSED unless a seal record exists (--seal-record)
    whose seal_id matches --seal-id and whose packet_hash matches
    the packet's packet_hash. Since no seal exists yet, live mode
    currently refuses by design; that refusal is the guard, not a bug.

Secrets and hidden labels:
  - The API key is never read, printed, or logged by this script.
    Live calls go through the connector client (dynamic_credentials
    surrogate); the key itself is never in this process's arguments,
    environment, or output.
  - Hidden labels are computed only inside the label loader, which
    the driver calls through the gate after both arms finish. This
    script never calls the loader early and never prints labels or
    id lists. Before writing, the output is scrub-checked for
    surrogate tokens, bearer tokens, and key names; a scrub failure
    aborts the write.
  - The saved report carries summaries (hashes, counts, usage),
    never id lists and never label values.

Pipeline parameters (live only) are caller-supplied and recorded in
the manifest: --problem-prompt-file, --dataset-manifest-file,
--metric, --baseline, --confidence, --minimum-effect,
--pipeline-budget-count or --pipeline-budget-fraction, --total-items.
No defaults are invented for them: missing live parameters refuse
the run before any API call. These parameters describe the pipeline
experiment framing; the comparison budgets themselves always come
from the packet manifest and must equal the frozen budgets.
"""
import argparse
import csv
import hashlib
import json
import os
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridgelab import comparison_driver
from bridgelab.comparison import FROZEN_BUDGET_COUNTS
from bridgelab.ledger import Ledger
from bridgelab import pricing

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTCOME_HEADER_TOKENS = ("EXPECTEDGROWTH", "PERCENTGROWTH", "observed_score")


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def combined_code_hash():
    h = hashlib.sha256()
    bridgelab = os.path.join(ROOT, "bridgelab")
    for name in sorted(os.listdir(bridgelab)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(bridgelab, name)
        h.update(name.encode())
        h.update(b"\n")
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        h.update(b"\n")
    return h.hexdigest()


def scrub_check(obj):
    """Refuse output containing credential or authorization material."""
    text = json.dumps(obj, sort_keys=True, ensure_ascii=False)
    for token in ("hsurr:", "Bearer ", "MODEL_API_KEY", "Authorization:"):
        if token in text:
            raise ValueError("scrub failure: forbidden token %r in output" % token)

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                lowered = str(key).lower()
                if lowered in ("api_key", "api-key") or lowered.endswith("_token"):
                    if isinstance(value, str):
                        raise ValueError("scrub failure: credential-like key %r" % key)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(obj)


# ---------------------------------------------------------------- fake mode

def model_response(text):
    return {"model": "muse-spark-1.3", "choices": [{"message": {"content": text}}]}


class FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.model = "muse-spark-1.3"

    def chat(self, messages, tools=None):
        item = self._responses.pop(0) if self._responses else model_response("{}")
        if isinstance(item, Exception):
            raise item
        return item


def fake_data():
    """Small synthetic data for fake mode only. Nothing real is read."""
    candidates = [
        {"item_id": "c1", "nsc1": "A", "nsc2": "B"},
        {"item_id": "c2", "nsc1": "A", "nsc2": "B"},
        {"item_id": "c3", "nsc1": "A", "nsc2": "C"},
        {"item_id": "c4", "nsc1": "D", "nsc2": "E"},
        {"item_id": "c5", "nsc1": "X", "nsc2": "Y"},
        {"item_id": "c6", "nsc1": "X", "nsc2": "Z"},
        {"item_id": "c7", "nsc1": "A", "nsc2": "D"},
        {"item_id": "c8", "nsc1": "X", "nsc2": "Y"},
        {"item_id": "c9", "nsc1": "B", "nsc2": "C"},
        {"item_id": "c10", "nsc1": "D", "nsc2": "F"},
        {"item_id": "c11", "nsc1": "X", "nsc2": "Z"},
        {"item_id": "c12", "nsc1": "A", "nsc2": "B"},
    ]
    train = [
        {"item_id": "t1", "nsc1": "A", "nsc2": "B", "observed_score": 10.0},
        {"item_id": "t2", "nsc1": "A", "nsc2": "B", "observed_score": 0.0},
        {"item_id": "t3", "nsc1": "A", "nsc2": "C", "observed_score": 4.0},
        {"item_id": "t4", "nsc1": "D", "nsc2": "E", "observed_score": -2.0},
        {"item_id": "t5", "nsc1": "X", "nsc2": "Y", "observed_score": 9.0},
        {"item_id": "t6", "nsc1": "X", "nsc2": "Z", "observed_score": -9.0},
    ]
    labels = {"c1": 5.0, "c2": -1.0, "c3": 2.0, "c4": -3.0, "c5": 8.0,
              "c6": -6.0, "c7": 1.0, "c8": 7.0, "c9": -2.0, "c10": 3.0,
              "c11": -4.0, "c12": 4.0}
    return train, candidates, labels


def fake_judge(method="drug_mean", domain="mathematics"):
    return {"selected_domain": domain, "selected_principle": "P",
            "selected_method": method, "method_parameters": {},
            "prediction": "pred", "falsifier": "fals",
            "required_data_fields": ["item_id", "nsc1", "nsc2", "observed_score"]}


def fake_judge_none():
    return {"selected_domain": "none", "selected_principle": "",
            "selected_method": "none", "method_parameters": {},
            "prediction": "", "falsifier": "", "required_data_fields": []}


# ---------------------------------------------------------------- live helpers

def load_packet(packet_dir):
    """Load packet inputs. Raises on any inconsistency; reads no outcomes."""
    manifest_path = os.path.join(packet_dir, "packet_manifest_v3.json")
    prereg_path = os.path.join(packet_dir, "preregistration_v3.json")
    if not os.path.exists(manifest_path):
        manifest_path = os.path.join(packet_dir, "packet_manifest.json")
    if not os.path.exists(prereg_path):
        prereg_path = os.path.join(packet_dir, "preregistration.json")
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    with open(prereg_path, encoding="utf-8") as f:
        prereg = json.load(f)
    with open(os.path.join(packet_dir, "train_items.csv"), newline="") as f:
        reader = csv.DictReader(f)
        train_rows = [{"item_id": r["item_id"], "nsc1": r["nsc1"], "nsc2": r["nsc2"],
                       "observed_score": float(r["observed_score"])} for r in reader]
    with open(os.path.join(packet_dir, "hidden_candidates.csv"), newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        if any(tok in header for tok in OUTCOME_HEADER_TOKENS):
            raise ValueError("hidden candidates header carries outcome columns")
        candidate_rows = [{"item_id": r["item_id"], "nsc1": r["nsc1"],
                           "nsc2": r["nsc2"], "cellname": r["cellname"]} for r in reader]
    with open(os.path.join(packet_dir, "baseline_prompt.md"), encoding="utf-8") as f:
        prompt_template = f.read()
    if "{budget_count}" not in prompt_template:
        raise ValueError("baseline prompt lacks the {budget_count} placeholder")
    budgets_raw = manifest.get("budgets_floor")
    if isinstance(budgets_raw, dict):
        budgets = sorted(budgets_raw.values())
    else:
        budgets = list(budgets_raw or [])
    if tuple(budgets) != tuple(FROZEN_BUDGET_COUNTS):
        raise ValueError("packet budgets are not the frozen budgets")
    packet_hash = prereg.get("packet_hash")
    if not packet_hash:
        raise ValueError("packet preregistration has no packet_hash")
    return {"manifest": manifest, "prereg": prereg, "train_rows": train_rows,
            "candidate_rows": candidate_rows, "prompt_template": prompt_template,
            "budgets": budgets, "packet_hash": packet_hash}


def check_seal(seal_record_path, seal_id, packet_hash):
    """Seal guard: refuse unless the seal record matches id + packet."""
    if not seal_record_path or not os.path.exists(seal_record_path):
        raise SystemExit("REFUSED: no seal record at %s; live mode requires "
                         "an existing seal (E0002 has not been created)." % seal_record_path)
    with open(seal_record_path, encoding="utf-8") as f:
        record = json.load(f)
    if record.get("seal_id") != seal_id:
        raise SystemExit("REFUSED: seal record seal_id does not match --seal-id.")
    if record.get("packet_hash") != packet_hash:
        raise SystemExit("REFUSED: seal record packet_hash does not match the packet.")
    seal_hash = record.get("seal_hash")
    if not isinstance(seal_hash, str) or len(seal_hash) != 64:
        raise SystemExit("REFUSED: seal record has no valid seal_hash.")
    return seal_hash


# The live client now lives in bridgelab/clients.py under one interface
# shared with the fake client (same chat()/model/provenance shape).
# ConnectorMuseClient is kept as an alias so existing call sites and
# imports keep working; it is LiveMuseClient, which additionally
# sanitizes errors so no credential material can leak through them.
from bridgelab.clients import LiveMuseClient as ConnectorMuseClient


def make_hidden_label_loader(candidate_rows, data_path, stats):
    """Build a label loader over the raw archive (called only via the gate).

    Reads combination rows only (NSC2 present), computes the sealed
    score as the unrounded mean per-point (EXPECTEDGROWTH -
    PERCENTGROWTH), and returns labels for candidate ids only. Rows
    with missing/unparseable growth values are skipped (unscored
    rows are excluded, never imputed). stats records coverage; it is
    reported in the manifest, never the labels themselves."""
    wanted = {}
    for row in candidate_rows:
        wanted[(str(row["nsc1"]), str(row["nsc2"]), str(row["cellname"]))] = row["item_id"]

    def loader():
        sums = {}
        counts = {}
        if data_path.endswith(".zip"):
            with zipfile.ZipFile(data_path) as zf:
                names = [n for n in zf.namelist() if n.endswith(".csv")]
                if len(names) != 1:
                    raise ValueError("raw archive must contain exactly one CSV")
                stream = zf.open(names[0])
                reader = csv.DictReader((line.decode("utf-8") for line in stream))
                _accumulate(reader, wanted, sums, counts)
        else:
            with open(data_path, newline="", encoding="utf-8") as f:
                _accumulate(csv.DictReader(f), wanted, sums, counts)
        labels = {}
        for key, item_id in wanted.items():
            if counts.get(key):
                labels[item_id] = sums[key] / counts[key]
        stats["candidates"] = len(wanted)
        stats["labeled"] = len(labels)
        stats["unlabeled"] = len(wanted) - len(labels)
        return labels

    return loader


def _accumulate(reader, wanted, sums, counts):
    for row in reader:
        if not row.get("NSC2"):
            continue
        key = (str(row.get("NSC1")), str(row.get("NSC2")), str(row.get("CELLNAME")))
        if key not in wanted:
            continue
        try:
            diff = float(row["EXPECTEDGROWTH"]) - float(row["PERCENTGROWTH"])
        except (TypeError, ValueError, KeyError):
            continue
        sums[key] = sums.get(key, 0.0) + diff
        counts[key] = counts.get(key, 0) + 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run a two-arm BRIDGE LAB comparison.")
    parser.add_argument("--mode", required=True, choices=["fake", "live"])
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--packet-dir", default=None)
    parser.add_argument("--seal-record", default=None)
    parser.add_argument("--seal-id", default=None)
    parser.add_argument("--problem-prompt-file", default=None)
    parser.add_argument("--dataset-manifest-file", default=None)
    parser.add_argument("--metric", default=None)
    parser.add_argument("--baseline", default=None)
    parser.add_argument("--confidence", default=None, type=float)
    parser.add_argument("--minimum-effect", default=None, type=float)
    parser.add_argument("--pipeline-budget-count", default=None, type=int)
    parser.add_argument("--pipeline-budget-fraction", default=None, type=float)
    parser.add_argument("--total-items", default=None, type=int)
    parser.add_argument("--split-ids-file", default=None)
    parser.add_argument("--raw-data", default=None)
    parser.add_argument("--prices", default=os.path.join(ROOT, "bridgelab", "model_prices.json"))
    args = parser.parse_args(argv)

    started = time.monotonic()
    fd, ledger_path = tempfile.mkstemp(prefix="comparison-ledger-", suffix=".jsonl")
    os.close(fd)
    os.unlink(ledger_path)
    ledger = Ledger(ledger_path)
    try:
        if args.mode == "fake":
            run_id = args.run_id or "fake-verification"
            out_path = args.out or os.path.join(ROOT, "runs", "comparison_%s.json" % run_id)
            train_rows, candidate_rows, labels = fake_data()
            budgets = [3, 5]
            prompt_template = 'Return JSON only: {"selected_item_ids": [...]} with exactly {budget_count} ids.'
            ids = [r["item_id"] for r in candidate_rows]
            client = FakeClient([model_response(json.dumps({"selected_item_ids": ids[:b]}))
                                 for b in budgets for _ in (0, 1, 2)])
            judges = {0: fake_judge("drug_mean"), 1: fake_judge_none(),
                      2: fake_judge("exact_pair_mean")}
            report = comparison_driver.run_comparison(
                client, ledger, prompt_template, judges, train_rows, candidate_rows,
                budgets, lambda: dict(labels), run_indices=(0, 1, 2),
                n_bootstrap=100, seed=42, bootstrap_indices=(),
                run_id=run_id, ledger_path=out_path)
            manifest = {"run_id": run_id, "mode": "fake",
                        "packet_hash": None, "code_hash": combined_code_hash(),
                        "seal_id": None, "seal_hash": None,
                        "driver_version": comparison_driver.VERSION,
                        "budgets": budgets, "run_indices": [0, 1, 2],
                        "problem_prompt_sha256": sha256_text("fake comparison problem"),
                        "driver_report": report,
                        "pipeline_usage": None, "total_usage": report["usage"],
                        "cost": {"price_config_version": None, "price_config_source": None,
                                 "cost": "MISSING", "currency": None},
                        "ledger_entries": ledger.entries(),
                        "fake_note": "Synthetic data only; fake budgets are not the frozen budgets; "
                                     "no packet file, raw archive, or real label was read.",
                        "elapsed_seconds": round(time.monotonic() - started, 3)}
            scrub_check(manifest)
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=1, sort_keys=True)
            print("mode: fake")
            print("run_id: %s" % run_id)
            print("gate: %s" % json.dumps(report["gate"], sort_keys=True))
            print("claims: %s" % json.dumps(report["claims"], sort_keys=True))
            print("output: %s" % out_path)
            print("scrub: passed")
            return 0

        # ---- live mode: guards first, no API call before they pass ----
        if not args.packet_dir:
            raise SystemExit("REFUSED: live mode requires --packet-dir.")
        packet = load_packet(args.packet_dir)
        seal_hash = check_seal(args.seal_record, args.seal_id, packet["packet_hash"])
        if not args.problem_prompt_file or not os.path.exists(args.problem_prompt_file):
            raise SystemExit("REFUSED: live mode requires --problem-prompt-file.")
        if not args.dataset_manifest_file or not os.path.exists(args.dataset_manifest_file):
            raise SystemExit("REFUSED: live mode requires --dataset-manifest-file.")
        missing = [name for name, value in
                   (("metric", args.metric), ("baseline", args.baseline),
                    ("confidence", args.confidence), ("minimum_effect", args.minimum_effect),
                    ("total_items", args.total_items)) if value is None]
        if missing:
            raise SystemExit("REFUSED: live mode missing parameters: %s" % ", ".join(missing))
        if args.pipeline_budget_count is None and args.pipeline_budget_fraction is None:
            raise SystemExit("REFUSED: live mode requires --pipeline-budget-count or "
                             "--pipeline-budget-fraction.")
        if args.total_items != len(packet["candidate_rows"]):
            raise SystemExit("REFUSED: --total-items does not match the packet candidate count.")
        if not args.raw_data or not os.path.exists(args.raw_data):
            raise SystemExit("REFUSED: live mode requires --raw-data (raw archive or CSV).")
        with open(args.problem_prompt_file, encoding="utf-8") as f:
            problem_prompt = f.read()
        with open(args.dataset_manifest_file, encoding="utf-8") as f:
            dataset_manifest = json.load(f)
        # Split contract (fixed 2026-10-27): the packet's split_ids.json
        # is a JSON OBJECT. Lists are refused, the five required keys
        # and id types are validated, train/hidden overlap is refused,
        # and the file hash must match the packet record when the
        # record carries one (v3). The SAME dict object goes to
        # run_pipeline and to run_comparison below.
        from bridgelab import orchestration
        if not args.split_ids_file:
            raise SystemExit("REFUSED: live mode requires --split-ids-file "
                             "(the packet's split_ids.json object).")
        if not os.path.exists(args.split_ids_file):
            raise SystemExit("REFUSED: --split-ids-file does not exist: %s"
                             % args.split_ids_file)
        try:
            split_ids = orchestration.load_split_ids_file(args.split_ids_file)
        except ValueError as exc:
            raise SystemExit("REFUSED: invalid split_ids file: %s" % exc)
        with open(args.split_ids_file, "rb") as f:
            split_ids_file_sha256 = hashlib.sha256(f.read()).hexdigest()
        recorded_split_hash = None
        try:
            recorded_split_hash = packet["prereg"]["execution_parameters"]["split_ids"]["sha256"]
        except (KeyError, TypeError):
            recorded_split_hash = None
        if recorded_split_hash is not None and recorded_split_hash != split_ids_file_sha256:
            raise SystemExit("REFUSED: --split-ids-file hash does not match "
                             "the packet record's split_ids hash.")
        split_ids_source = "file"

        # The live pipeline now loads transfer_judge_v2.md natively
        # (orchestration ROLE_TO_PROMPT_FILE); no override is needed.
        prompts = orchestration.load_role_prompts(os.path.join(ROOT, "prompts"))
        run_id = args.run_id or ("live-%s-%s" % (args.seal_id, time.strftime("%Y%m%d-%H%M%S")))
        out_path = args.out or os.path.join(ROOT, "runs", "comparison_%s.json" % run_id)
        client = ConnectorMuseClient()
        budgets = packet["budgets"]
        run_indices = (0, 1, 2)
        judges = {}
        pipeline_usage = {}
        pipeline_latency = {}
        for index in run_indices:
            start = len(ledger.entries())
            result = orchestration.run_pipeline(
                client, ledger, problem_prompt, prompts, dataset_manifest,
                split_ids=split_ids, metric=args.metric, baseline=args.baseline,
                confidence=args.confidence, minimum_effect=args.minimum_effect,
                budget_fraction=args.pipeline_budget_fraction,
                budget_count=args.pipeline_budget_count,
                total_items=args.total_items, max_total_model_steps=100)
            segment = comparison_driver.aggregate_usage(ledger.entries()[start:])
            pipeline_usage[index] = {
                "prompt_tokens": segment["prompt_tokens"],
                "completion_tokens": segment["completion_tokens"],
                "total_tokens": segment["total_tokens"],
                "cost": segment["cost"],
                "n_model_calls": segment["n_model_calls"],
                "usage_missing_calls": segment["usage_missing_calls"]}
            pipeline_latency[index] = segment["latency_ms_total"]
            state = result.get("state") if isinstance(result, dict) else None
            judge = state.get("judge") if isinstance(state, dict) else None
            judges[index] = judge  # None -> harness failed for that index
        label_stats = {}
        loader = make_hidden_label_loader(packet["candidate_rows"], args.raw_data, label_stats)
        report = comparison_driver.run_comparison(
            client, ledger, packet["prompt_template"], judges, packet["train_rows"],
            packet["candidate_rows"], budgets, loader, run_indices=run_indices,
            n_bootstrap=1000, seed=42, bootstrap_indices=tuple(run_indices),
            run_id=run_id, ledger_path=out_path,
            harness_usage_by_index=pipeline_usage,
            harness_latency_by_index=pipeline_latency,
            split_ids=split_ids)
        try:
            prices = pricing.load_prices(args.prices)
        except FileNotFoundError:
            prices = pricing.load_prices(None)
        driver_usage = report["usage"]
        total_prompt = total_completion = None
        pipe_prompt = sum((pipeline_usage[i]["prompt_tokens"] for i in run_indices
                           if isinstance(pipeline_usage[i]["prompt_tokens"], int)), 0)
        pipe_completion = sum((pipeline_usage[i]["completion_tokens"] for i in run_indices
                               if isinstance(pipeline_usage[i]["completion_tokens"], int)), 0)
        pipe_complete = all(isinstance(pipeline_usage[i]["prompt_tokens"], int)
                            and isinstance(pipeline_usage[i]["completion_tokens"], int)
                            for i in run_indices)
        if pipe_complete and isinstance(driver_usage["prompt_tokens"], int):
            total_prompt = pipe_prompt + driver_usage["prompt_tokens"]
            total_completion = pipe_completion + driver_usage["completion_tokens"]
        total_usage = {
            "prompt_tokens": total_prompt, "completion_tokens": total_completion,
            "total_tokens": (total_prompt + total_completion
                             if total_prompt is not None else None),
            "cost": pricing.compute_cost(total_prompt, total_completion,
                                         "muse-spark-1.3", prices),
            "driver_usage": driver_usage, "pipeline_usage": pipeline_usage,
            "label_stats": label_stats}
        manifest = {"run_id": run_id, "mode": "live",
                    "packet_hash": packet["packet_hash"],
                    "split_ids_sha256": split_ids_file_sha256,
                    "code_hash": combined_code_hash(),
                    "seal_id": args.seal_id, "seal_hash": seal_hash,
                    "driver_version": comparison_driver.VERSION,
                    "budgets": budgets, "run_indices": list(run_indices),
                    "problem_prompt_sha256": sha256_text(problem_prompt),
                    "problem_prompt": problem_prompt,
                    "pipeline_parameters": {
                        "metric": args.metric, "baseline": args.baseline,
                        "confidence": args.confidence,
                        "minimum_effect": args.minimum_effect,
                        "pipeline_budget_count": args.pipeline_budget_count,
                        "pipeline_budget_fraction": args.pipeline_budget_fraction,
                        "total_items": args.total_items,
                        "split_ids_source": split_ids_source,
                        "max_total_model_steps": 100},
                    "driver_report": report,
                    "pipeline_usage": pipeline_usage,
                    "total_usage": total_usage,
                    "cost": {"price_config_version": prices.get("version"),
                             "price_config_source": prices.get("source"),
                             "cost": total_usage["cost"],
                             "currency": prices.get("currency")},
                    "ledger_entries": ledger.entries(),
                    "elapsed_seconds": round(time.monotonic() - started, 3)}
        scrub_check(manifest)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=1, sort_keys=True)
        print("mode: live")
        print("run_id: %s" % run_id)
        print("gate: %s" % json.dumps(report["gate"], sort_keys=True))
        print("claims: %s" % json.dumps(report["claims"], sort_keys=True))
        print("cost: %s" % json.dumps(manifest["cost"], sort_keys=True))
        print("output: %s" % out_path)
        print("scrub: passed")
        return 0
    finally:
        if os.path.exists(ledger_path):
            os.unlink(ledger_path)


if __name__ == "__main__":
    sys.exit(main())
