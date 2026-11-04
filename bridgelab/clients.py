"""One client interface for fake and live Muse calls, with provenance.

Both clients expose the same interface:

    client.chat(messages, tools=None) -> response dict
    client.model  (str)
    client.provenance  (str, one of the PROVENANCE_LABELS)

FakeMuseClient returns scripted responses and never touches the
network. LiveMuseClient talks to the Muse API through the registered
connector surrogate: the real credential never enters this process
(dynamic_credentials exchanges it per request, only for api.meta.ai),
and the client never stores, logs, or returns headers, surrogates, or
request objects. Errors raised by LiveMuseClient are sanitized to a
status string only (HTTP status or a generic failure) so that no
credential material can leak through an exception message, a ledger
entry, a report, or a UI surface.

Provenance labels for results (kept in sync with bridgelab/ledger.py
PROVENANCE, which additionally keeps the legacy pipeline labels
"user", "model", and "executed" valid for older entries):

    fake-scripted    scripted fake-client output (tests, demos, fault paths)
    live-muse        a real Muse API response
    python-executed  a number or selection computed by Python, never by a model
    user-provided    text or data supplied by the user
    retrieved        data retrieved from a dataset or document

run_model_call and agent.run_role record the calling client's
provenance on model_call ledger entries via provenance_of(); clients
without a provenance attribute (legacy test fakes) fall back to
"model" so existing entries are unchanged.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

FAKE_SCRIPTED = "fake-scripted"
LIVE_MUSE = "live-muse"
PYTHON_EXECUTED = "python-executed"
USER_PROVIDED = "user-provided"
RETRIEVED = "retrieved"

PROVENANCE_LABELS = (FAKE_SCRIPTED, LIVE_MUSE, PYTHON_EXECUTED,
                     USER_PROVIDED, RETRIEVED)

LIVE_MODEL = "muse-spark-1.3"
LIVE_API_URL = "https://api.meta.ai/v1/chat/completions"


def provenance_of(client) -> str:
    """Return the ledger provenance for a client's output.

    Clients declaring one of the new labels use it; anything else
    (legacy fakes, unknown objects) falls back to "model".
    """
    provenance = getattr(client, "provenance", "model")
    if provenance in PROVENANCE_LABELS or provenance in ("user", "model", "executed", "retrieved"):
        return provenance
    return "model"


class MuseClient:
    """The shared client interface. Subclasses implement chat()."""

    model = ""
    provenance = "model"

    def chat(self, messages, tools=None):
        raise NotImplementedError


class FakeMuseClient(MuseClient):
    """Scripted fake client. Never touches the network.

    responses: a list consumed in order. Each item is either a
    response dict (returned as-is), a callable taking (messages,
    tools) and returning a response dict, or an exception instance to
    raise (for timeout/failure paths). When the list is exhausted, the
    last response is repeated. messages_received records every call's
    messages; calls_made counts calls. model defaults to
    "fake-scripted" but may be set to "muse-spark-1.3" when a test
    needs run_model_call's model-id check to pass; provenance is
    always "fake-scripted" regardless of the model value.
    """

    def __init__(self, responses, model="fake-scripted"):
        self._responses = list(responses)
        self._index = 0
        self.model = model
        self.provenance = FAKE_SCRIPTED
        self.calls_made = 0
        self.messages_received = []

    def chat(self, messages, tools=None):
        self.calls_made += 1
        self.messages_received.append(messages)
        if not self._responses:
            raise RuntimeError("FakeMuseClient has no scripted responses")
        if self._index < len(self._responses):
            item = self._responses[self._index]
            self._index += 1
        else:
            item = self._responses[-1]
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(messages, tools)
        return item


class LiveMuseClient(MuseClient):
    """Live Muse client through the registered connector surrogate.

    The credential is never an input to this process: __init__ only
    loads the surrogate helper, and each chat() call exchanges a
    short-lived surrogate for that single request to api.meta.ai.
    Nothing credential-like is stored on the instance, logged, or
    included in any raised error. Errors are sanitized: an HTTP
    failure surfaces only its status code, anything else surfaces a
    generic message, and the original exception (with any transport
    detail) is never chained, so tracebacks cannot carry it either.
    last_http_status records the most recent HTTP status (an int on
    success or HTTP failure, None when no HTTP response arrived); it
    carries no header, body, or credential material.
    """

    def __init__(self, timeout=180):
        import sys
        sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
        try:
            from dynamic_credentials import add_surrogate_to_request
        except ImportError as exc:
            raise SystemExit("REFUSED: connector credential helper unavailable: %s" % exc)
        self._add_surrogate = add_surrogate_to_request
        self.model = LIVE_MODEL
        self.provenance = LIVE_MUSE
        self.timeout = timeout
        self.calls_made = 0
        self.last_http_status = None

    def chat(self, messages, tools=None):
        # tools are accepted for interface parity; the live chat API
        # call used here carries no tool specifications.
        self.calls_made += 1
        body = {"model": self.model, "messages": messages}
        request = urllib.request.Request(
            LIVE_API_URL, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        self._add_surrogate(request, "custom.meta-model-api", allowed_hosts=["api.meta.ai"])
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                self.last_http_status = resp.status
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            self.last_http_status = exc.code
            raise RuntimeError("live-muse request failed: HTTP %s" % exc.code) from None
        except (urllib.error.URLError, TimeoutError):
            raise RuntimeError("live-muse request failed: timeout or connection error") from None
        except Exception:
            raise RuntimeError("live-muse request failed") from None
