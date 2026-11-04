"""Show what a seal guarantees, without reading any hidden label.

1. Seal and reveal a toy plan with bridgelab/seal.py: the commitment holds, a
   tampered reveal is refused, and a seal can be revealed only once.
2. With a run ledger path, check that the run's plan was sealed before Python
   executed, and that the whole ledger's hash chain is intact.

Usage: python3 scripts/demo_seal_check.py [path/to/run-ledger.jsonl]
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridgelab.ledger import Ledger  # noqa: E402
from bridgelab.seal import reveal, seal  # noqa: E402


def toy():
    ledger = Ledger(os.path.join(tempfile.mkdtemp(), "toy.jsonl"))
    plan = {"hypothesis": "rank by drug mean within each cell line",
            "prediction": "precision above the random baseline",
            "falsifier": "no lift over the baseline"}
    good = seal(ledger, plan)
    print("sealed plan, commitment %s..." % ledger.get(good["seal_entry"])["data"]["sha256"][:16])
    tampered = dict(good, payload=dict(plan, prediction="precision above 0.9"))
    other = seal(ledger, plan)
    try:
        reveal(ledger, dict(other, payload=tampered["payload"]))
        print("tampered reveal: ACCEPTED (this should never happen)")
    except ValueError as exc:
        print("tampered reveal: REFUSED (%s)" % exc)
    reveal(ledger, good)
    print("honest reveal: matches the commitment")
    try:
        reveal(ledger, good)
        print("second reveal: ACCEPTED (this should never happen)")
    except ValueError as exc:
        print("second reveal: REFUSED (%s)" % exc)
    print("toy ledger hash chain:", ledger.verify())


def run_order(path):
    ledger = Ledger(path)
    entries = ledger.entries()
    seal_i = next((i for i, e in enumerate(entries) if e["kind"] == "seal"), None)
    tool_i = next((i for i, e in enumerate(entries) if e["kind"] == "tool_call"), None)
    if seal_i is None or tool_i is None:
        print("ledger has no seal and tool_call pair")
        return
    s, t = entries[seal_i], entries[tool_i]
    print("run %s: plan sealed at %s (%s...), Python executed at %s: %s" % (
        os.path.basename(path), s["id"], s["data"]["sha256"][:12], t["id"],
        "sealed BEFORE execution" if seal_i < tool_i else "ORDER WRONG"))
    print("run ledger hash chain:", ledger.verify())


if __name__ == "__main__":
    toy()
    if len(sys.argv) > 1:
        print()
        run_order(sys.argv[1])
