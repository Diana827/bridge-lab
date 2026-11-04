"""BRIDGE LAB server — stdlib only.

Serves the UI and runs the real pipeline behind a small API:

  GET  /                          -> demo-ui/index.html
  GET  /files/<name>              -> files from demo-ui/ (guarded)
  POST /api/runs {question?, mode: "fake"|"live"}
                                  -> starts run_pipeline in a thread
  GET  /api/runs/<id>             -> run status + summary
  GET  /api/runs/<id>/events      -> SSE stream of ledger entries
  GET  /api/comparison/status     -> seal record of the v3 packet
  POST /api/comparison            -> 403: the sealed comparison runs
                                     only through the reviewed caller

Live mode calls the Meta Model API through LiveMuseClient (connector
surrogate; the credential never enters this process). Runs write an
append-only ledger per run under server/runs/. The comparison gate is
not reachable from this server.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)

from bridgelab.ledger import Ledger
# E0004 harness: same pipeline, plus drug_cell_mean and the v3 Judge prompt (bridgelab/harness_v2.py).
from bridgelab.harness_v2 import load_role_prompts, run_pipeline_v2 as run_pipeline
from bridgelab.structure_resolver import build_manifest

RUNS_DIR = os.path.join(ROOT, "server", "runs")
TESTS_DIR = os.path.join(ROOT, "server", "tests")
UI_DIR = os.path.join(ROOT, "demo-ui")
RUNS = {}
TESTS = {}
LOCK = threading.Lock()
MAX_REQUEST_BYTES = 64 * 1024
MAX_CONCURRENT_RUNS = 2
MAX_RUN_SECONDS = 15 * 60
RUN_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_RUNS)
SERVER_ID = "bridge-lab-local-v4"

REQUIRED_RUNTIME_FILES = (
    "prompts/problem_structurer.md",
    "prompts/transfer_judge_v3.md",
    "prompts/critic_v2.md",
    "prompts/reporter_v2.md",
    "comparison-prereg-v4/dataset_manifest.json",
    "comparison-prereg-v4/split_ids.json",
    "comparison-prereg-v4/train_items.csv",
    "comparison-prereg-v4/inputs/nsc_structure.csv",
    "comparison-prereg-v4/inputs/candidate_items.csv",
)


def _missing_runtime_files():
    return [rel for rel in REQUIRED_RUNTIME_FILES
            if not os.path.isfile(os.path.join(ROOT, rel))]


def _mark_run_timeout(run_id):
    with LOCK:
        rec = RUNS.get(run_id)
        if rec and rec.get("status") == "running":
            rec["status"] = "timed_out"
            rec["result"] = {"status": "timed_out", "error": "run_timeout"}


def _load_smoke_module():
    path = os.path.join(ROOT, "scripts", "smoke_muse.py")
    spec = importlib.util.spec_from_file_location("smoke_muse", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _train_pair_means():
    import csv
    sums, counts = {}, {}
    with open(os.path.join(ROOT, "comparison-prereg-v4", "train_items.csv")) as f:
        for row in csv.DictReader(f):
            key = (row["nsc1"], row["nsc2"])
            sums[key] = sums.get(key, 0.0) + float(row["observed_score"])
            counts[key] = counts.get(key, 0) + 1
    return [sums[k] / counts[k] for k in sorted(sums)]


def _live_execution_callback(judge, sealed_plan, ledger):
    """Run the sealed method before any post-execution reviewer is called."""
    from server.compute_test_v2 import compute_method_test_v2 as compute_method_test
    method = judge.get("selected_method")
    train_path = os.path.join(ROOT, "comparison-prereg-v4", "train_items.csv")
    try:
        result = compute_method_test(train_path, method)
    except Exception as exc:
        return "failed", [], [], "execution_failed", method
    entry = ledger.append("tool_call", "executed", {
        "tool": "python.compute_method_test",
        "arguments": {"method": method, "dataset": "train_items.csv"},
        "result": {"status": "ok", "output": result},
        "provenance": "python-executed",
    })
    return "ok", [entry], ["python.compute_method_test"], None, None


def _execute_run(run_id, mode, question):
    rec = RUNS[run_id]
    ledger_path = os.path.join(RUNS_DIR, run_id + ".jsonl")
    ledger = Ledger(ledger_path)
    timeout_timer = threading.Timer(MAX_RUN_SECONDS, _mark_run_timeout, args=(run_id,))
    timeout_timer.daemon = True
    timeout_timer.start()
    try:
        missing = _missing_runtime_files()
        if missing:
            rec["status"] = "failed"
            rec["result"] = {"status": "failed", "error": "runtime_files_missing",
                             "project_root": ROOT, "missing_files": missing}
            return
        def review_callback(payload):
            rec["review"] = {"status": "waiting", "payload": payload}
            rec["review_event"].wait(MAX_RUN_SECONDS)
            review = rec.get("review") or {}
            return {"action": review.get("action", "end"),
                    "direction": review.get("direction", "")}
        prompts = load_role_prompts(os.path.join(ROOT, "prompts"))
        if mode == "fake":
            smoke = _load_smoke_module()
            client = smoke.FakeRoleClient()
            manifest = {"dataset_id": "nci_almanac_combo",
                        "molecular_structure_available": False,
                        "structure_source": None, "structure_field": None,
                        "structure_file_hash": None}
            split_ids = {"train_ids": ["pair_%03d" % i for i in range(300)],
                         "hidden_ids": ["pair_%03d" % i for i in range(300, 400)],
                         "start_set_ids": ["pair_001"], "split_seed": 42,
                         "split_method": "fixed_random_split"}
            result = run_pipeline(
                client, ledger, question or "Find pairs.", prompts, manifest,
                split_ids, metric="hit_rate", baseline="random", confidence=0.95,
                minimum_effect=0.1, budget_fraction=0.05, total_items=400,
                execution_calls=[{"tool": "summarize_numbers",
                                  "arguments": {"values": [1, 2, 3], "budget_count": 20}}],
                verdict_input={"value": 0.5, "ci_low": 0.4, "ci_high": 0.6,
                               "baseline_value": 0.2})
        else:
            from local_client import make_live_client
            client = make_live_client(timeout=180)
            with open(os.path.join(ROOT, "comparison-prereg-v4", "dataset_manifest.json")) as f:
                manifest = json.load(f)
            manifest, _ = build_manifest(
                manifest,
                os.path.join(ROOT, "comparison-prereg-v4", "inputs", "nsc_structure.csv"),
                os.path.join(ROOT, "comparison-prereg-v4", "inputs", "candidate_items.csv"),
            )
            from bridgelab.orchestration import load_split_ids_file
            split_ids = load_split_ids_file(os.path.join(
                ROOT, "comparison-prereg-v4", "split_ids.json"))
            result = run_pipeline(
                client, ledger, question, prompts, manifest, split_ids,
                metric="precision_at_5pct_budget", baseline="single_muse_call",
                confidence=0.95, minimum_effect=0.0, budget_fraction=0.05,
                total_items=293626,
                # The Judge's plan governs execution: the production
                # callers pass no caller-invented tool calls, because the
                # pipeline runs only judge-planned tools. (Run 4 failed
                # with tool_not_in_plan when the server imposed its own
                # summarize_numbers call the live Judge had not planned.)
                execution_calls=[],
                max_total_model_steps=100,
                review_callback=review_callback,
                execution_callback=_live_execution_callback)
        rstatus = result.get("status")
        state = result.get("state") or {}
        if rec.get("status") != "timed_out":
            if rstatus == "ok":
                rec["status"] = "completed"
            elif rstatus == "blocked_on_clarification":
                rec["status"] = "clarification"
            elif rstatus == "review_ended":
                rec["status"] = "ended_by_user"
            else:
                rec["status"] = "failed"
        judge = state.get("judge") or {}
        if rec.get("status") != "timed_out":
            rec["result"] = {"status": rstatus,
                             "error": result.get("error"),
                             "failed_role": result.get("role"),
                             "clarification": result.get("question"),
                             "verdict": state.get("final_verdict"),
                             "next_experiment": {
                                 "selected_method": judge.get("selected_method"),
                                 "selected_domain": judge.get("selected_domain"),
                             "execution_results": state.get("execution_results", []),
                                 "prediction": judge.get("prediction"),
                                 "falsifier": judge.get("falsifier"),
                                 "budget_count": state.get("budget_count")}}
    except FileNotFoundError as exc:  # safe path diagnostic; never expose secrets
        filename = os.path.realpath(exc.filename) if exc.filename else ""
        try:
            missing = os.path.relpath(filename, ROOT) if filename.startswith(ROOT + os.sep) else os.path.basename(filename)
        except (TypeError, ValueError):
            missing = os.path.basename(filename)
        rec["status"] = "failed"
        rec["result"] = {"status": "failed", "error": "FileNotFoundError",
                         "missing_file": missing, "project_root": ROOT}
    except Exception as exc:  # sanitized: type only, never message bodies
        rec["status"] = "failed"
        rec["result"] = {"status": "failed", "error": type(exc).__name__}
    finally:
        timeout_timer.cancel()
        rec["finished_at"] = time.time()
        RUN_SLOTS.release()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, content_type):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return self._json(404, {"error": "not found"})
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _video(self, path):
        """Serve the demo video with byte-range support so players can seek."""
        try:
            size = os.path.getsize(path)
        except OSError:
            return self._json(404, {"error": "demo.mp4 not found in demo-ui/"})
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes=") and "-" in rng:
            first, _, last = rng[6:].partition("-")
            try:
                start = int(first) if first else max(0, size - int(last))
                end = int(last) if first and last else end
                code = 206
            except ValueError:
                start, end, code = 0, size - 1, 200
        end = min(end, size - 1)
        if start > end:
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % size)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(code)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            return self._file(os.path.join(UI_DIR, "index.html"), "text/html; charset=utf-8")
        if self.path == "/api/health":
            return self._json(200, {"ok": True, "server": SERVER_ID,
                                    "project_root": ROOT,
                                    "missing_runtime_files": _missing_runtime_files()})
        if self.path == "/preview.html":
            return self._file(os.path.join(UI_DIR, "preview.html"), "text/html; charset=utf-8")
        if self.path == "/demo.mp4":
            return self._video(os.path.join(UI_DIR, "demo.mp4"))
        if self.path.startswith("/files/"):
            name = os.path.basename(self.path[len("/files/"):])
            return self._file(os.path.join(UI_DIR, name), "text/html; charset=utf-8")
        if self.path == "/api/comparison/status":
            # Current seal: E0003 (v4 packet). E0002/v3 remains the preserved
            # The current sealed record is E0003 in comparison-prereg-v4/.
            rec_path = os.path.join(ROOT, "comparison-prereg-v4", "seal_record_v4.json")
            if os.path.exists(rec_path):
                with open(rec_path) as f:
                    record = json.load(f)
                # The public demo package intentionally does not contain the
                # seal secret or private ledger payload. Do not report the
                # record's original ledger_verify value as if it were checked
                # locally when the referenced ledger is absent.
                ledger_file = record.get("ledger_file")
                ledger_path = os.path.join(ROOT, ledger_file) if ledger_file else None
                if not ledger_path or not os.path.exists(ledger_path):
                    record = dict(record)
                    record["ledger_verify"] = None
                    record["ledger_status"] = "public_record_only"
                    record["ledger_note"] = "Private seal ledger is not bundled in the demo package."
                return self._json(200, record)
            return self._json(200, {"sealed": False})
        if self.path == "/api/dataset/status":
            manifest_path = os.path.join(ROOT, "comparison-prereg-v4", "structure_manifest.json")
            with open(manifest_path) as handle:
                manifest = json.load(handle)
            return self._json(200, dict(manifest, candidate_items=293626,
                                        candidate_pairs=5040, cell_lines=60,
                                        drug_count=102, budget_count=29363))
        parts = self.path.strip("/").split("/")
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "computational-tests":
            test_id = parts[2]
            rec = TESTS.get(test_id)
            if rec is None:
                return self._json(404, {"error": "unknown test"})
            if len(parts) == 3:
                entries = (Ledger(rec["ledger_path"]).entries()
                           if os.path.exists(rec["ledger_path"]) else [])
                return self._json(200, {"id": test_id, "run_id": rec["run_id"],
                                        "mode": "LIVE EXPLORATION",
                                        "status": rec["status"],
                                        "events": len(entries),
                                        "result": rec.get("result")})
            if len(parts) == 4 and parts[3] == "events":
                return self._events(test_id, rec)
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "runs":
            run_id = parts[2]
            rec = RUNS.get(run_id)
            if rec is None:
                return self._json(404, {"error": "unknown run"})
            if len(parts) == 3:
                entries = Ledger(rec["ledger_path"]).entries()
                return self._json(200, {"id": run_id, "mode": rec["mode"],
                                        "status": rec["status"],
                                        "events": len(entries),
                                        "result": rec.get("result")})
            if len(parts) == 4 and parts[3] == "events":
                return self._events(run_id, rec)
        return self._json(404, {"error": "not found"})

    def _events(self, run_id, rec):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        sent = 0
        while True:
            entries = Ledger(rec["ledger_path"]).entries()
            while sent < len(entries):
                e = entries[sent]
                sent += 1
                data = e.get("data") or {}
                payload = {"id": e.get("id"), "ts": e.get("ts"),
                           "kind": e.get("kind"),
                           "provenance": e.get("provenance"),
                           "role": data.get("role"),
                           "status": data.get("status"),
                           "summary": _summarize(e),
                           "text": _event_text(e),
                           "latency_ms": data.get("latency_ms"),
                           "usage": data.get("usage")}
                try:
                    self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
            if rec["status"] != "running" and sent >= len(entries):
                try:
                    self.wfile.write(b"event: done\ndata: {}\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            time.sleep(0.3)

    def do_POST(self):
        if self.path == "/api/comparison":
            return self._json(403, {"error": "the sealed comparison runs only "
                                            "through the reviewed live caller"})
        if self.path.startswith("/api/runs/") and self.path.endswith("/review"):
            run_id = self.path.split("/")[3]
            rec = RUNS.get(run_id)
            if rec is None or "review_event" not in rec:
                return self._json(404, {"error": "review gate not found"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_REQUEST_BYTES:
                return self._json(413, {"error": "request body too large"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json(400, {"error": "invalid json"})
            action = body.get("action")
            if action not in ("continue", "end"):
                return self._json(400, {"error": "action must be continue or end"})
            with LOCK:
                rec["review"] = {"status": "released", "action": action,
                                  "direction": str(body.get("direction") or "")[:12000]}
                rec["review_event"].set()
            return self._json(202, {"id": run_id, "status": "released"})
        if self.path == "/api/computational-tests":
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_REQUEST_BYTES:
                return self._json(413, {"error": "request body too large"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json(400, {"error": "invalid json"})
            run_id = body.get("run_id") or ""
            if not os.path.exists(os.path.join(RUNS_DIR, run_id + ".jsonl")):
                return self._json(404, {"error": "unknown run"})
            from test_runner import execute_test
            test_id = "test-" + uuid.uuid4().hex[:12]
            os.makedirs(TESTS_DIR, exist_ok=True)
            rec = {"id": test_id, "run_id": run_id, "status": "running",
                   "ledger_path": os.path.join(TESTS_DIR, test_id + ".jsonl"),
                   "result": None}
            with LOCK:
                TESTS[test_id] = rec
            thread = threading.Thread(target=execute_test, args=(rec, run_id),
                                      daemon=True)
            thread.start()
            return self._json(202, {"id": test_id, "run_id": run_id,
                                    "status": "running",
                                    "events_url": "/api/computational-tests/%s/events" % test_id})
        if self.path not in ("/api/runs", "/api/research-runs"):
            return self._json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_REQUEST_BYTES:
            return self._json(413, {"error": "request body too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json(400, {"error": "invalid json"})
        mode = body.get("mode", "fake")
        if mode not in ("fake", "live"):
            return self._json(400, {"error": "mode must be fake or live"})
        question = body.get("question") or None
        if mode == "live" and not question:
            question = ("Prioritize the next research candidates in our dataset, "
                        "then validate the selected method with a train-only Python evaluation.")
        if question and len(question) > 12000:
            return self._json(413, {"error": "question too long"})
        if not RUN_SLOTS.acquire(blocking=False):
            return self._json(429, {"error": "too many runs", "retry_after_seconds": 10})
        run_id = "run-" + uuid.uuid4().hex[:12]
        ledger_path = os.path.join(RUNS_DIR, run_id + ".jsonl")
        with LOCK:
            RUNS[run_id] = {"mode": mode, "status": "running",
                            "ledger_path": ledger_path, "result": None,
                            "started_at": time.time(),
                            "review_event": threading.Event(), "review": None}
        thread = threading.Thread(target=_execute_run,
                                  args=(run_id, mode, question), daemon=True)
        thread.start()
        return self._json(202, {"id": run_id, "mode": mode, "status": "running",
                                "events_url": "/api/runs/%s/events" % run_id})


def _utterance(role, raw):
    """Render one model output as a short human line for the event feed."""
    if not raw:
        return ""
    cleaned = str(raw).strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    try:
        obj = json.loads(cleaned)
    except (ValueError, TypeError):
        return "Model response was not valid JSON; the run did not use this text as evidence."
    if not isinstance(obj, dict):
        return str(raw)[:220]
    if role == "problem_structurer":
        text = obj.get("objective") or ""
    elif role in ("mathematician", "physicist", "chemist"):
        text = " · ".join(x for x in [obj.get("principle"),
                                      obj.get("testable_prediction")] if x)
    elif role == "biology_validator":
        reviews = obj.get("reviews") or []
        text = " · ".join("%s: %s" % (r.get("domain"), r.get("verdict"))
                           for r in reviews if isinstance(r, dict))
    elif role == "transfer_judge":
        text = "selected %s (%s) — %s" % (obj.get("selected_method"),
                                          obj.get("selected_domain"),
                                          obj.get("prediction") or "")
    elif role == "critic":
        text = "%s — next: %s" % (obj.get("classification") or "",
                                  obj.get("next_action") or "")
    elif role == "reporter":
        text = obj.get("summary") or ""
    else:
        text = ""
    return str(text)[:280]


def _event_text(entry):
    data = entry.get("data") or {}
    kind = entry.get("kind")
    if kind == "model_call":
        return _utterance(data.get("role"), data.get("text"))
    if kind == "tool_call":
        result = data.get("result") or {}
        out = result.get("output") or {}
        parts = ["%s -> %s" % (data.get("tool"), result.get("status"))]
        if isinstance(out, dict) and "precision_method" in out:
            parts.append("precision %s vs random %s · diff %s" % (
                out["precision_method"], out["precision_random_baseline"],
                out["difference"]))
        elif isinstance(out, dict) and "mean" in out:
            parts.append("mean %s · n %s" % (round(out["mean"], 4), out.get("count")))
        return " · ".join(parts)
    if kind == "verdict":
        return str(data.get("final") or data.get("classification") or "")
    if kind == "note" and data.get("status") == "review_ready":
        return "Review before Judge · paused after the three proposals"
    return ""


def _summarize(entry):
    data = entry.get("data") or {}
    kind = entry.get("kind")
    if kind == "model_call":
        return "model call: %s" % (data.get("role") or "?")
    if kind == "tool_call":
        return "tool: %s -> %s" % (data.get("tool"), (data.get("result") or {}).get("status"))
    if kind == "user_input":
        return "question received"
    if kind in ("seal", "reveal", "verdict"):
        return kind
    return kind or "entry"


if __name__ == "__main__":
    port = int(os.environ.get("BRIDGE_PORT", "8791"))
    missing = _missing_runtime_files()
    if missing:
        raise SystemExit("BRIDGE LAB runtime files missing under %s: %s" % (ROOT, ", ".join(missing)))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("BRIDGE LAB server on http://127.0.0.1:%d (root: %s)" % (port, ROOT))
    server.serve_forever()
