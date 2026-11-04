"""Seal a hypothesis, prediction and success rule BEFORE the experiment runs; reveal and check it afterwards.

commit = sha256(canon(payload) + '|' + nonce). The public commitment (hash only) goes into the ledger first;
the payload and nonce are written to the ledger at reveal time. Written fresh for this repo.
"""
from __future__ import annotations
import secrets
from .ledger import Ledger, canon, sha256


def commit_hash(payload: dict, nonce: str) -> str:
    return sha256(canon(payload) + "|" + nonce)


def seal(ledger: Ledger, payload: dict) -> dict:
    """Writes only the hash to the ledger. Returns the secret part the caller must keep until reveal."""
    if not isinstance(payload, dict) or not payload:
        raise ValueError("seal payload must be a non-empty object")
    nonce = secrets.token_hex(16)
    entry = ledger.append("seal", "executed", {"sha256": commit_hash(payload, nonce)})
    return {"seal_entry": entry["id"], "payload": payload, "nonce": nonce}


def reveal(ledger: Ledger, secret: dict) -> dict:
    # Safeguard 5: a seal can be revealed once. A rerun must create a new seal;
    # reusing a revealed seal is refused before any hash check, so a revealed
    # secret cannot be replayed as a fresh experiment.
    if not isinstance(secret, dict) or "seal_entry" not in secret or "payload" not in secret or "nonce" not in secret:
        raise ValueError("reveal secret must contain seal_entry, payload and nonce")
    for entry in ledger.entries():
        if entry["kind"] == "reveal" and entry["data"].get("seal_entry") == secret["seal_entry"]:
            raise ValueError("seal already revealed: a rerun requires a new seal")
    sealed = ledger.get(secret["seal_entry"])
    if sealed is None or sealed["kind"] != "seal":
        raise ValueError("seal entry not found")
    ok = sealed["data"]["sha256"] == commit_hash(secret["payload"], secret["nonce"])
    entry = ledger.append("reveal", "executed", {"seal_entry": secret["seal_entry"], "payload": secret["payload"],
                                                 "nonce": secret["nonce"], "match": ok})
    if not ok:
        raise ValueError("reveal does not match the sealed hash")
    return entry
