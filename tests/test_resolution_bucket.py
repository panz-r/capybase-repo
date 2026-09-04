"""Resolution-bucket classification (live_eval_realworld) — the histogram.

The bucket answers ONE question per case: who produced the accepted
candidates? deterministic (zero model calls) | llm_one_shot | llm_cegis.
It must be a total order — every case with an accepted unit lands in
exactly one bucket, so the columns add up to PASS+WORKING.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path


_SPEC = importlib.util.spec_from_file_location(
    "_live_eval_realworld",
    Path(__file__).resolve().parent.parent / "scripts" / "live_eval_realworld.py")
_ler = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("_live_eval_realworld", _ler)
_SPEC.loader.exec_module(_ler)


@dataclass
class _Cand:
    provenance: str = ""


@dataclass
class _Outcome:
    accepted: _Cand | None = None
    attempts: list = field(default_factory=list)


def test_all_deterministic():
    outs = [
        _Outcome(accepted=_Cand("deterministic_structural")),
        _Outcome(accepted=_Cand("exact_history_reuse")),
        _Outcome(accepted=_Cand("combination_search")),
        _Outcome(accepted=_Cand("deterministic_source_current_only")),
    ]
    bucket, mix = _ler.classify_resolution_bucket(outs)
    assert bucket == "deterministic"
    assert mix["exact_history_reuse"] == 1


def test_hybrid_counts_as_llm():
    # plain_llm + deterministic closure: the model produced the accepted
    # text — LLM bucket, and the hybrid shows in the mix for the
    # histogram's finer rows.
    outs = [_Outcome(
        accepted=_Cand("plain_llm+import_union+named_field_union"),
        attempts=[_Cand("plain_llm+import_union+named_field_union")])]
    bucket, mix = _ler.classify_resolution_bucket(outs)
    assert bucket == "llm_one_shot"
    assert mix["plain_llm+import_union+named_field_union"] == 1


def test_one_shot_llm():
    outs = [
        _Outcome(accepted=_Cand("deterministic_structural"),
                 attempts=[_Cand("deterministic_structural")]),
        _Outcome(accepted=_Cand("plain_llm"), attempts=[_Cand("plain_llm")]),
    ]
    bucket, _ = _ler.classify_resolution_bucket(outs)
    assert bucket == "llm_one_shot"


def test_cegis_when_model_saw_a_failure():
    # attempt 1 failed validation, attempt 2 (with feedback) accepted.
    outs = [_Outcome(
        accepted=_Cand("history_augmented_llm"),
        attempts=[_Cand("history_augmented_llm"),
                  _Cand("history_augmented_llm")])]
    bucket, _ = _ler.classify_resolution_bucket(outs)
    assert bucket == "llm_cegis"


def test_best_of_n_sampling_stays_one_shot():
    # The sampling loop appends ONE candidate per resolve round; N sampled
    # candidates must not read as CEGIS.
    outs = [_Outcome(accepted=_Cand("plain_llm"), attempts=[_Cand("plain_llm")])]
    bucket, _ = _ler.classify_resolution_bucket(outs)
    assert bucket == "llm_one_shot"


def test_deterministic_retries_do_not_make_cegis():
    # A structural decline then an LLM accept in ONE round is a mechanism
    # switch, not counterexample feedback — only >1 LLM attempts count.
    outs = [_Outcome(
        accepted=_Cand("plain_llm"),
        attempts=[_Cand("deterministic_structural"),   # declined try
                  _Cand("plain_llm")])]                # accepted
    bucket, _ = _ler.classify_resolution_bucket(outs)
    assert bucket == "llm_one_shot"


def test_block_capture_is_llm():
    # The model DECIDED keep/delete — an LLM call even though the splice
    # is mechanical.
    outs = [_Outcome(accepted=_Cand("block_capture"),
                     attempts=[_Cand("block_capture")])]
    bucket, _ = _ler.classify_resolution_bucket(outs)
    assert bucket == "llm_one_shot"


def test_micro_patch_repair_is_llm():
    outs = [_Outcome(
        accepted=_Cand("micro_patch_repair"),
        # the micro patch follows a failed plain_llm attempt
        attempts=[_Cand("plain_llm"), _Cand("micro_patch_repair")])]
    bucket, _ = _ler.classify_resolution_bucket(outs)
    assert bucket == "llm_cegis"


def test_escalated_only_is_unbucketed():
    outs = [_Outcome(accepted=None, attempts=[])]
    bucket, mix = _ler.classify_resolution_bucket(outs)
    assert bucket == ""
    assert mix == {}


def test_nothing_at_all():
    assert _ler.classify_resolution_bucket(None) == ("", {})
    assert _ler.classify_resolution_bucket([]) == ("", {})


def test_mixed_case_any_llm_wins_any_cegis_wins():
    # Case level is a total order: one LLM unit anywhere -> llm; one
    # cegis unit anywhere -> cegis.
    outs = [
        _Outcome(accepted=_Cand("deterministic_structural")),
        _Outcome(accepted=_Cand("plain_llm"),
                 attempts=[_Cand("plain_llm"), _Cand("plain_llm")]),
    ]
    assert _ler.classify_resolution_bucket(outs)[0] == "llm_cegis"
