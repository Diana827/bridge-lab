"""Append-only, hash-chained experiment ledger (JSON Lines).

Every step of a run is one entry. Each entry carries a provenance label so the report can tell
user input, retrieved data, model output and executed results apart (principle 10 of the system prompt).
Entry hash = sha256(canon(entry without 'hash')); 'prev' is the previous entry's hash, so editing or
deleting an earlier entry breaks verify().
"""
from __future__ import annotations
import hashlib, json, os, re, time

# Legacy pipeline labels ("user", "model", "executed") stay valid for
# older entries. The client-separation labels live in
# bridgelab/clients.py PROVENANCE_LABELS and are added here so ledger
# entries can carry them: fake-scripted (scripted fake output),
# live-muse (real Muse API output), python-executed (Python-computed),
# user-provided (user-supplied). "retrieved" is shared by both sets.
PROVENANCE = ("user", "retrieved", "model", "executed",
              "fake-scripted", "live-muse", "python-executed", "user-provided")
KINDS = ("user_input", "model_call", "tool_call", "seal", "reveal", "verdict", "note", "influence")

# Safeguard 8: API keys and other credentials must never reach the ledger file.
# Values under these key names are replaced, "Bearer <value>" is replaced, and
# any exact value found in MODEL_API_KEY / META_API_KEY in the process
# environment is replaced. The replacement never prints the secret.
_REDACTED = "[REDACTED]"
_BEARER_RE = re.compile(r"Bearer\s+[^\s]+")


def _secret_values():
    values = []
    for name in ("MODEL_API_KEY", "META_API_KEY"):
        value = os.environ.get(name, "")
        if len(value) >= 8:
            values.append(value)
    return values


def _scrub_text(text: str, secrets) -> str:
    out = _BEARER_RE.sub("Bearer " + _REDACTED, text)
    for secret in secrets:
        if secret in out:
            out = out.replace(secret, _REDACTED)
    return out


def _is_secret_key(lowered: str) -> bool:
    """True for credential key names. The "token" part is matched precisely
    (exactly "token", or ending in "_token") rather than as a substring:
    API usage count keys (prompt_tokens, completion_tokens, total_tokens)
    end in "_tokens" (plural) and are measurements, not credentials — a
    substring match redacted them and made usage recording impossible."""
    for part in ("api_key", "apikey", "secret", "password", "authorization", "auth"):
        if part in lowered:
            return True
    return lowered == "token" or lowered.endswith("_token")


def scrub_data(data):
    """Return a copy of data with credential values redacted. Used by Ledger.append."""
    secrets = _secret_values()

    def scrub(obj):
        if isinstance(obj, dict):
            result = {}
            for key, value in obj.items():
                lowered = str(key).lower()
                if _is_secret_key(lowered):
                    result[key] = _REDACTED
                else:
                    result[key] = scrub(value)
            return result
        if isinstance(obj, list):
            return [scrub(item) for item in obj]
        if isinstance(obj, str):
            return _scrub_text(obj, secrets)
        return obj

    return scrub(data)


def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Ledger:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._entries = self._read()

    def _read(self):
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def entries(self):
        return list(self._entries)

    def append(self, kind: str, provenance: str, data: dict) -> dict:
        if kind not in KINDS:
            raise ValueError("unknown kind: %s" % kind)
        if provenance not in PROVENANCE:
            raise ValueError("unknown provenance: %s" % provenance)
        e = {"id": "E%04d" % (len(self._entries) + 1), "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "kind": kind, "provenance": provenance, "data": json.loads(canon(scrub_data(data))),
             "prev": self._entries[-1]["hash"] if self._entries else "0" * 64}
        e["hash"] = sha256(canon(e))
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        self._entries.append(e)
        return e

    def verify(self):
        """Returns (ok, n_entries, first_bad_id or None)."""
        prev = "0" * 64
        for e in self._read():
            core = {k: v for k, v in e.items() if k != "hash"}
            if e.get("prev") != prev or sha256(canon(core)) != e.get("hash"):
                return False, len(self._entries), e.get("id")
            prev = e["hash"]
        return True, len(self._entries), None

    def get(self, entry_id: str):
        for e in self._entries:
            if e["id"] == entry_id:
                return e
        return None
