"""Local connection test: list models, then one minimal chat completion.

Reads MODEL_API_KEY from the environment (start.sh loads .env first).
Prints only status codes, model ids, and usage — never the key.
Run directly:  MODEL_API_KEY=... python3 test_connection.py
"""
import json
import os
import sys
import urllib.request

BASE = "https://api.meta.ai/v1"
KEY = os.environ.get("MODEL_API_KEY", "")
if not KEY:
    print("MODEL_API_KEY is not set. Copy .env.example to .env and add your key.")
    sys.exit(1)


def call(path, body=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + KEY,
                 "Content-Type": "application/json"},
        method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.status, json.loads(resp.read().decode())


try:
    status, models = call("/models")
    ids = [m.get("id") for m in models.get("data", [])]
    print("GET /models ->", status, "|", len(ids), "models |",
          "muse-spark-1.3 present:", "muse-spark-1.3" in ids)
    status, resp = call("/chat/completions",
                        {"model": "muse-spark-1.3",
                         "messages": [{"role": "user", "content": "Reply with OK"}]})
    print("POST /chat/completions ->", status, "| model:", resp.get("model"),
          "| usage:", resp.get("usage"))
    print("Connection OK.")
except Exception as exc:  # status only; bodies can carry no key but stay terse
    print("Connection FAILED:", type(exc).__name__, getattr(exc, "code", ""))
    sys.exit(1)
