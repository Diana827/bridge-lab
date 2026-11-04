"""Scripted stand-in for MuseClient, for tests and offline runs. It is NOT Meta output and must never be
presented as such; the scripts/smoke_muse.py --fake run prints that label."""
from __future__ import annotations
import json

class FakeClient:
    model = "fake-scripted"

    def __init__(self, values):
        self.values, self.n = values, 0

    def chat(self, messages, tools=None):
        self.n += 1
        if self.n == 1:
            call = {"id": "call_1", "type": "function", "function": {"name": "summarize_numbers", "arguments": json.dumps({"values": self.values})}}
            return {"model": self.model, "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]}}], "usage": None}
        last = json.loads(messages[-1]["content"])
        txt = "mean = %s (ledger %s)" % (last["output"]["mean"], last["ledger_entry"])
        return {"model": self.model, "choices": [{"message": {"role": "assistant", "content": txt}}], "usage": None}
