# E0003 sealed comparison — Attempt 1 interrupted (record of the original attempt)

- Run id: live-E0003-20261029 (scripts/run_live_comparison.py --mode live,
  packet staged byte-identical from comparison-prereg-v4, seal E0003).
- Launched 2026-10-29 ~18:05 PDT after read-only seal verification passed.
- Progress observed via the caller's temporary ledger (snapshots):
  - ~10 min: 88 entries. All 3 Harness pipeline runs complete
    (structurer/mathematician/physicist/chemist/validator/judge/critic/
    reporter per index; Reporter succeeded on its single recorded call in
    each run); 3 internal plan seals; 3 verdicts. Baseline arm in progress
    (4 baseline calls).
  - ~13 min: 97 entries. ~15 min: 109 entries, 20 baseline-arm calls.
  - ~17 min (last observation): 117 entries; 51 model_call entries
    (30 pipeline + 21 baseline-arm); 12 note entries (baseline retries /
    failures being recorded by the driver, as designed).
- Hidden label gate: at every observation the comparison driver had NOT
  reached scoring; no gate/reveal entries existed in the ledger, and the
  loader is invoked only after both arms finish. As of the last
  observation the hidden labels had NOT been read.
- Interruption: the platform replaced the VM during a monitoring call
  (~18:24 PDT). /tmp was wiped: the caller process, its log, and the
  temporary comparison ledger are lost. The caller writes its output
  file only at the very end, so NO output file exists for attempt 1.
- Preserved intact (home directory): the v4 packet, the E0003 seal
  (commit recomputes, ledger verify True), E0002 and all earlier packets.
- No result exists for attempt 1: no precisions, no claim evaluation,
  nothing scored. Nothing from attempt 1 may be presented as a result.
- Awaiting Diana's decision: relaunch the sealed comparison (a fresh
  execution under the same E0003 seal) or stop. Per her standing rule,
  no unilateral rerun was started.
