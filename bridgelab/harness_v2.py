"""Harness v2 entry point for E0004 (harness-v2 v1).

New module only. orchestration.py, validation.py and agent.py are not
modified, so the code hash bound by seal E0003 stays verifiable.

orchestration.py hard-codes two things the E0004 Harness must change: the
allowed_methods list shown to the Transfer Judge, and the Judge prompt file.
This module derives orchestration_v2 from orchestration.py's own source by
exactly those three replacements, each asserted to match once. If
orchestration.py ever changes, the assertions fail loudly instead of the
Harness silently running a different pipeline.

The Judge output validator (validation._validate_judge) checks
selected_method against validation.JUDGE_METHODS. run_pipeline_v2 widens that
tuple for the duration of one call and always restores it.

Selection is compiled by decision_compiler_v2.compile_decision_v2.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import types

from . import validation
from .decision_compiler_v2 import METHODS_V2, compile_decision_v2

VERSION = "harness-v2 v1"
JUDGE_PROMPT_FILE = "transfer_judge_v3.md"

_REPLACEMENTS = (
    ('"allowed_methods": ["exact_pair_mean", "drug_mean", "pair_frequency"],',
     '"allowed_methods": ["exact_pair_mean", "drug_mean", "pair_frequency", "drug_cell_mean", "structure_cell_knn"],'),
    ('"transfer_judge": "transfer_judge_v2.md",',
     '"transfer_judge": "%s",' % JUDGE_PROMPT_FILE),
    # v1's instruction told the Judge not to require molecular structures, which blocked structure_cell_knn.
    ('"method; do not require clinical endpoints, per-combination costs, "\n            "or molecular structures. Report those as limitations, not blockers. "',
     '"method; do not require clinical endpoints or per-combination costs. "\n            "Molecular structures exist for 103 of the 105 drugs and one allowlisted "\n            "method uses them. Report missing context as limitations, not blockers. "'),
)


def _orchestration_source():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orchestration.py")
    with open(path, encoding="utf-8") as f:
        return f.read()


def source_hashes():
    """sha256 of orchestration.py and of the derived v2 source, for the packet."""
    src = _orchestration_source()
    return {"orchestration_v1": hashlib.sha256(src.encode()).hexdigest(),
            "orchestration_v2": hashlib.sha256(_derive(src).encode()).hexdigest()}


def _derive(src):
    for old, new in _REPLACEMENTS:
        if src.count(old) != 1:
            raise RuntimeError("orchestration.py no longer contains exactly one copy of %r" % old[:60])
        src = src.replace(old, new)
    return src


def load_orchestration_v2():
    module = types.ModuleType("bridgelab.orchestration_v2")
    module.__package__ = "bridgelab"
    module.__file__ = "<derived from bridgelab/orchestration.py>"
    exec(compile(_derive(_orchestration_source()), module.__file__, "exec"), module.__dict__)
    return module


orchestration_v2 = load_orchestration_v2()
load_role_prompts = orchestration_v2.load_role_prompts


@contextlib.contextmanager
def judge_methods_v2():
    original = validation.JUDGE_METHODS
    validation.JUDGE_METHODS = tuple(m for m in METHODS_V2) + ("none",)
    try:
        yield
    finally:
        validation.JUDGE_METHODS = original


def run_pipeline_v2(*args, **kwargs):
    """orchestration.run_pipeline with the v2 contract and Judge prompt."""
    with judge_methods_v2():
        return orchestration_v2.run_pipeline(*args, **kwargs)


def judge_output_of(result):
    """The Transfer Judge output of a finished pipeline result, or None when the
    pipeline failed before the Judge (a failed Harness run under ITT)."""
    if not isinstance(result, dict):
        return None
    judge = (result.get("state") or {}).get("judge")
    return judge if isinstance(judge, dict) else None


def compile_harness_selection(judge_output, train_rows, candidate_rows, budget_count, ledger=None):
    return compile_decision_v2(judge_output, train_rows, candidate_rows, budget_count, ledger=ledger)
