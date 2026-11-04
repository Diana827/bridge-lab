# Evidence of live Meta Model API use

These are append-only run ledgers written by BRIDGE LAB while it called `muse-spark-1.3` through the Meta Model API. Every `model_call` entry has `"provenance": "live-muse"`, the model id, latency, and a hash chain (`prev`, `hash`) that `bridgelab/ledger.py` can verify. API keys are scrubbed from ledgers before they are written, and these files were checked for the key value.

| File | Recorded (UTC) | Judge chose | Python check (train holdout, 5%) | Decision |
|---|---|---|---|---|
| `run-3e649c3f36ff.jsonl` | 2026-11-03 06:27 | `exact_pair_mean` | precision 0.572 vs random 0.2943 | inconclusive |
| `run-063be4e97c68.jsonl` | 2026-11-03 22:34 | `drug_cell_mean` | precision 0.6313 vs random 0.2943 | inconclusive |
| `run-5cf6729c13e4.jsonl` | 2026-11-03 22:59 | `drug_cell_mean` | precision 0.6313 vs random 0.2943 | inconclusive |

"Inconclusive" is by design: a live run reads no held-out data. Verify a ledger: `python3 -c "from bridgelab.ledger import Ledger; print(Ledger('evidence/recorded-runs/run-3e649c3f36ff.jsonl').verify())"`.
