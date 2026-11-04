"""Critic rerun budget and new-seal-per-rerun enforcement.

Safeguard 4: a Critic may request only a bounded number of reruns.
Safeguard 5: every rerun must start from a fresh seal; reusing a revealed
seal is refused by seal.reveal itself, and this runner refuses a rerun that
does not supply a new sealed secret.
"""
from __future__ import annotations
from . import seal

DEFAULT_MAX_RERUNS = 2


class CriticRerunBudget:
    """Counts Critic-requested reruns. Total experiments = 1 initial + max_reruns."""

    def __init__(self, max_reruns: int = DEFAULT_MAX_RERUNS):
        if not isinstance(max_reruns, int) or isinstance(max_reruns, bool) or max_reruns < 0:
            raise ValueError("max_reruns must be a non-negative integer")
        self.max_reruns = max_reruns
        self.used = 0

    @property
    def remaining(self) -> int:
        return self.max_reruns - self.used

    def consume(self) -> int:
        """Consume one rerun. Returns the rerun number (1-based), or raises."""
        if self.used >= self.max_reruns:
            raise RuntimeError("critic rerun budget exhausted: %d reruns used" % self.used)
        self.used += 1
        return self.used

    def can_rerun(self) -> bool:
        return self.used < self.max_reruns


class SealedExperiment:
    """One sealed experiment, with reruns that each require a new seal.

    start() seals the first payload. rerun(new_secret) consumes one Critic
    rerun budget unit and records the new seal; the caller must have sealed
    new_secret with seal.seal() first, and must not reuse the previous secret.
    """

    def __init__(self, ledger, budget: CriticRerunBudget | None = None):
        self.ledger = ledger
        self.budget = budget if budget is not None else CriticRerunBudget()
        self.attempt = 0
        self.current = None
        self.previous_seal_entry = None

    def start(self, payload: dict) -> dict:
        if self.current is not None:
            raise RuntimeError("experiment already started; use rerun() for a follow-up")
        self.current = seal.seal(self.ledger, payload)
        self.attempt = 1
        return self.current

    def rerun(self, new_secret: dict) -> dict:
        if self.current is None:
            raise RuntimeError("no experiment started; use start() first")
        if not isinstance(new_secret, dict) or new_secret.get("seal_entry") == self.current["seal_entry"]:
            raise ValueError("rerun requires a new seal, not the previous seal")
        rerun_number = self.budget.consume()
        self.previous_seal_entry = self.current["seal_entry"]
        self.current = new_secret
        self.attempt += 1
        self.ledger.append("note", "executed", {
            "rerun": rerun_number, "attempt": self.attempt,
            "previous_seal_entry": self.previous_seal_entry,
            "new_seal_entry": new_secret["seal_entry"]})
        return self.current
