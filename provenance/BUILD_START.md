# BRIDGE LAB — Build Start Record

**Build start date: 2026-10-27**

**Project workspace (persistent, not moved):**
`/home/hatch/workspace/bridge-lab`

This is the Muse workspace for this hackathon work. It is not the
user's local computer and it is not, by itself, a public submission
repository. No public repository is connected yet, and no export has
been performed. See "Export plan" below.

## Stage 1 preservation (before Stage 2)

Stage 1 (offline core stabilization, fake clients only) is preserved
verbatim, outside the project tree, so Stage 2 work cannot overwrite
its record:

- Snapshot directory: `/home/hatch/workspace/bridge-lab-stage1-2026-10-27`
- Snapshot manifest: `/home/hatch/workspace/bridge-lab-stage1-2026-10-27/MANIFEST.sha256`
  (SHA-256 for every preserved file, excluding `__pycache__`)
- Verification at preservation time: `diff -r` between the project
  and the snapshot matched, excluding the manifest itself.
- Stage 1 test result at preservation time: 67 tests passed
  (`python3 -I -m unittest discover -s tests`), plus
  `python3 scripts/smoke_muse.py --fake` = smoke OK.
- The snapshot is a record only. Do not edit it. Continue
  development in `/home/hatch/workspace/bridge-lab`.

## No earlier code, data, results, or repository history imported

- Build started 2026-10-27 in this workspace.
- No git repository exists in this project (no `.git`, no history).
- No earlier project code, dataset, experiment result, or repository
  history was imported. The only starter material used was the
  BRIDGE LAB material the user provided for this hackathon itself
  (project documents and the Stage 1 core/prompt files listed for
  Stage 1), plus code written in this workspace during Stage 1.
- No data files and no experiment result files are present in this
  project tree. The only numerical outputs are test fixtures and
  fake-client smoke outputs, computed in Python during tests.
- Live Meta Muse generation has not been used: live validation
  remains blocked by team billing, and all development so far used
  fake clients only.

## Fake outputs are explicitly labelled, never Meta output

- Every fake client uses model name `fake-scripted`.
- `bridgelab/fake.py`, `scripts/smoke_muse.py`, and all test fake
  clients state in code and in output that their output is NOT Meta
  output and must never be presented as such.
- `scripts/smoke_muse.py` without `--fake` refuses to run and does
  not attempt a live call.
- A fake result must never be reported as a live Muse result, a
  dataset result, or a real experiment result.

## What Stage 1 contains (for the export checklist)

- `bridgelab/`: ledger, seal, tools, agent, validation, verdict,
  audit, rerun, orchestration, fake (no live Muse client; no
  `bridgelab/muse.py` was found in the workspace, so none was invented)
- `prompts/`: the 8 role prompt files
- `tests/`: test_core, test_hardening, test_orchestration, test_stage1
- `scripts/smoke_muse.py`: `--fake` smoke only
- Not in Stage 1, by scope: Case A data tools (`load_dataset`,
  `simulate_screening`, `evaluate`), web server, frontend UI, live
  API changes, external dependencies

## Export plan (not yet executed)

The project is NOT moved yet, per instruction. When the user
authorizes export, the steps will be, in order:

1. Freeze a Stage 2 (or final) snapshot the same way Stage 1 was
   preserved: copy the project tree excluding `__pycache__` to a
   dated snapshot directory and generate a SHA-256 manifest.
2. Clean the export tree: remove `__pycache__`, temporary ledger
   files, and any file containing a credential (ledger scrubbing
   already redacts keys, but the export check will grep for key-like
   values before publishing).
3. Run the full test suite and `scripts/smoke_muse.py --fake` from
   the export tree itself, and record the results in the export
   record. Do not present fake smoke output as live demo output.
4. Copy the cleaned tree into the submission repository chosen by
   the user (a public repository URL or upload method the user
   provides; none is connected yet). If that repository is a git
   repository, initialize or clone it, add the files, and commit
   with a message stating the build start date 2026-10-27 and that
   fake validation is labelled as fake. Do not push to a public
   repository without the user's explicit approval of the
   destination and the file list.
5. Demo from the exported repository: run the same commands from
   the exported copy (`python3 -I -m unittest discover -s tests -v`
   and `python3 scripts/smoke_muse.py --fake`), and, only after the
   billing block is lifted and the user approves a live run, run a
   live Muse smoke separately and label it as live. The demo must
   keep fake and live results in separate, labelled sections.
6. Keep this workspace as the working copy until the user confirms
   the exported repository is complete; do not delete the workspace
   copy as part of export.
