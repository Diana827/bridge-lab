"""Minimal Meta Model API client (standard library only), Chat Completions-compatible endpoint.

Key comes from the MODEL_API_KEY environment variable and is never logged or written to the ledger.
Base URL and model can be overridden with MUSE_BASE_URL / MUSE_MODEL.
"""
from __future__ import annotations
import json, os, time, urllib.error, urllib.request

BASE_URL = "https://api.meta.ai/v1"
MODEL = "muse-spark-1.3"


class MuseError(RuntimeError):
    pass


class MuseClient:
    def __init__(self, api_key: str = "", base_url: str = "", model: str = "", timeout: int = 90):
        self.api_key = api_key or os.environ.get("MODEL_API_KEY", "")
        self.base_url = (base_url or os.environ.get("MUSE_BASE_URL") or BASE_URL).rstrip("/")
        self.model = model or os.environ.get("MUSE_MODEL") or MODEL
        self.timeout = timeout
        if not self.api_key:
            raise MuseError("MODEL_API_KEY is not set")

    def chat(self, messages: list, tools: list = None) -> dict:
        body = {"model": self.model, "messages": messages}
        if tools:
            body["tools"] = tools
        req = urllib.request.Request(self.base_url + "/chat/completions", data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.api_key})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:500]
                if e.code == 429 and attempt < 3:
                    retry_after = e.headers.get("Retry-After")
                    try:
                        delay = min(30.0, max(1.0, float(retry_after)))
                    except (TypeError, ValueError):
                        delay = 2.0 ** attempt
                    time.sleep(delay)
                    continue
                if e.code == 429:
                    raise MuseError("HTTP 429: API rate limit reached after retries: %s" % detail)
                if e.code >= 500 and attempt < 3:
                    time.sleep(2.0 ** attempt)
                    continue
                raise MuseError("HTTP %d: %s" % (e.code, detail))
            except urllib.error.URLError as e:
                if attempt < 3:
                    time.sleep(2.0 ** attempt)
                    continue
                raise MuseError("network error: %s" % e.reason)
