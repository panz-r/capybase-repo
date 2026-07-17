"""Two-phase critic guardrail: deterministic anchor + show-your-work reflection.

The verifier-model critic can return high-confidence false positives — claiming
a side's intent was dropped when the deterministic validators prove both sides
are preserved at ratio 1.0. This three-layer guardrail fixes that:

- Phase 1: inject the deterministic preservation math into the critic's initial
  prompt (SYSTEM ASSERTION) so it doesn't hallucinate drops.
- Phase 3: hard suppress when coverage is unanimously perfect (the backstop).
- Phase 2: show-your-work reflection — a second call demanding the critic quote
  the exact missing snippet, verified programmatically (substring match).

Tests use a fake LLM client recording call count + returning scripted verdicts.
"""

from __future__ import annotations

import re

import pytest

from capybase.conflict_model import (
    ConflictSide,
    ConflictUnit,
    ContextBundle,
)
from capybase.adapters.llm_openai import LLMResponse
from capybase.resolution_engine import (
    DeterministicPreservation,
    _deterministic_assertion_block,
    _deterministic_preservation,
    build_verifier_prompt,
    build_verifier_reassessment_prompt,
)
from capybase.verification import (
    VerificationContext,
    VerifierModelValidator,
)
from capybase.config import ValidationConfig


# Opt IN to the real verifier critic (the autouse _isolate_verifier_critic
# conftest fixture replaces verify() with a no-op; these tests need the real
# implementation to exercise the guardrail phases).
pytestmark = pytest.mark.usefixtures("verifier_critic_enabled")


# ---------------------------------------------------------------------------
# Fakes + fixtures
# ---------------------------------------------------------------------------


class _FakeClient:
    """Records calls; returns scripted verdict JSON per call index."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        self.calls.append(list(messages))
        idx = len(self.calls) - 1
        text = self.responses[idx] if idx < len(self.responses) else self.responses[-1]
        return LLMResponse(text=text)


def _unit(*, base="", current="", replayed="", resolved="", language="python"):
    return ConflictUnit(
        session_id="s", step_index=0, path="a.py", language=language,
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text=base, marker_span=(0, 0),
    )


def _candidate(resolved=""):
    from capybase.conflict_model import CandidateResolution

    return CandidateResolution(
        candidate_id="c", unit_id="u", model_name="m",
        prompt_version="resolve_text_block.v1", resolved_text=resolved,
    )


def _verify(client, unit, candidate, *, cfg=None):
    cfg = cfg or ValidationConfig()
    v = VerifierModelValidator(client)
    ctx = VerificationContext(unit=unit, candidate=candidate, config=cfg)
    return v.verify(ctx)


_VERDICT_DROPS_REPLAYED = (
    '```json\n{"preserves_current": true, "preserves_replayed": false, '
    '"reason": "drops timeout_ms", "confidence": 1.0}\n```'
)
_VERDICT_PRESERVES_BOTH = (
    '```json\n{"preserves_current": true, "preserves_replayed": true, '
    '"reason": "ok", "confidence": 0.9}\n```'
)


# ---------------------------------------------------------------------------
# Phase 1: deterministic assertion in the prompt
# ---------------------------------------------------------------------------


def test_assertion_block_directive_when_unanimous():
    dp = DeterministicPreservation(
        cur_ratio=1.0, rep_ratio=1.0,
        dropped_cur_additions=False, dropped_replayed_additions=False,
        cur_dropped_names=[], rep_dropped_names=[],
    )
    block = _deterministic_assertion_block(dp)
    assert "SYSTEM ASSERTION" in block
    assert "MUST NOT flag missing additions" in block
    assert "1.00" in block


def test_assertion_block_pointer_when_imperfect():
    dp = DeterministicPreservation(
        cur_ratio=1.0, rep_ratio=0.5,
        dropped_cur_additions=False, dropped_replayed_additions=True,
        cur_dropped_names=[], rep_dropped_names=["load_config"],
    )
    block = _deterministic_assertion_block(dp)
    assert "GENUINE gaps" in block
    assert "load_config" in block
    assert "MUST NOT flag" not in block  # not directive when imperfect


def test_build_verifier_prompt_contains_assertion():
    unit = _unit(
        base="def f():\n    x = 1\n",
        current="def f():\n    x = 1\n    y = 2\n",
        replayed="def f():\n    x = 1\n    z = 3\n",
        resolved="def f():\n    x = 1\n    y = 2\n    z = 3\n",
    )
    cand = _candidate(resolved="def f():\n    x = 1\n    y = 2\n    z = 3\n")
    ctx = ContextBundle(primary_text="x")
    prompt = build_verifier_prompt(unit, cand, ctx)
    assert "SYSTEM ASSERTION" in prompt
    assert "MUST NOT flag missing additions" in prompt  # unanimous (both sides present)


def test_build_verifier_prompt_assertion_disabled():
    unit = _unit(base="x=1", current="x=1\ny=2", replayed="x=1\nz=3", resolved="x=1\ny=2\nz=3")
    cand = _candidate(resolved="x=1\ny=2\nz=3")
    ctx = ContextBundle(primary_text="x")
    prompt = build_verifier_prompt(unit, cand, ctx, assertion_enabled=False)
    assert "SYSTEM ASSERTION" not in prompt


# ---------------------------------------------------------------------------
# Phase 3: hard suppress (unanimous deterministic)
# ---------------------------------------------------------------------------


def test_phase3_hard_suppress_when_unanimous():
    """Critic flags replayed + both ratios 1.0 + no drops → suppressed, 1 call."""
    unit = _unit(
        base="def f():\n    x = 1\n",
        current="def f():\n    x = 1\n    y = 2\n",   # adds y
        replayed="def f():\n    x = 1\n    z = 3\n",  # adds z
        resolved="def f():\n    x = 1\n    y = 2\n    z = 3\n",  # both present
    )
    cand = _candidate(resolved="def f():\n    x = 1\n    y = 2\n    z = 3\n")
    client = _FakeClient([_VERDICT_DROPS_REPLAYED])  # critic wrongly flags
    res = _verify(client, unit, cand)
    assert res.passed is True  # suppressed
    assert res.features["verifier_guardrail_suppressed"] is True
    assert "unanimous" in res.features["verifier_guardrail_reason"]  # type: ignore
    assert len(client.calls) == 1  # no reassessment call (Phase 3 is zero-call)


def test_phase3_no_suppress_when_coverage_imperfect():
    """Critic flags + a real drop (ratio < 1.0) → no hard suppress."""
    unit = _unit(
        base="def f():\n    x = 1\n",
        current="def f():\n    x = 1\n    y = 2\n",
        replayed="def f():\n    x = 1\n    z = 3\n",
        resolved="def f():\n    x = 1\n    y = 2\n",  # z genuinely dropped
    )
    cand = _candidate(resolved="def f():\n    x = 1\n    y = 2\n")
    # Coverage imperfect → Phase 2 fires (need a reassessment response).
    client = _FakeClient([
        _VERDICT_DROPS_REPLAYED,
        '{"original_verdict_accurate": true, "reasoning": "z missing", '
        '"evidence_snippet": "z = 3"}',
    ])
    res = _verify(client, unit, cand)
    assert res.passed is False  # not suppressed — critic is right
    assert res.features["verifier_guardrail_suppressed"] is False


# ---------------------------------------------------------------------------
# Phase 2: show-your-work reflection
# ---------------------------------------------------------------------------


def test_phase2_revoke_on_null_evidence():
    """Critic flags + imperfect coverage + reassessment returns null evidence → squash."""
    # Construct a case where token-level coverage is imperfect (so Phase 3 doesn't fire)
    # but a side's distinctive token is absent — yet the reassessment can't ground it.
    # Uses real code (not comments) so the token check detects the drop — a
    # comment-only difference is no longer a drop after the r43 comment-blanking
    # fix to _deterministic_preservation.
    unit = _unit(
        base="def f():\n    x = 1\n",
        current="def f():\n    x = 1\n    y = 2\n",
        replayed="def f():\n    x = 1\n    z = 3\n",
        resolved="def f():\n    x = 1\n    y = 2\n",  # z = 3 absent
    )
    cand = _candidate(resolved="def f():\n    x = 1\n    y = 2\n")
    client = _FakeClient([
        _VERDICT_DROPS_REPLAYED,
        '{"original_verdict_accurate": false, "reasoning": "cant find it", '
        '"evidence_snippet": null}',
    ])
    res = _verify(client, unit, cand)
    assert res.passed is True  # squashed
    assert res.features["verifier_reassessed"] is True
    assert res.features["verifier_reassessment_outcome"] == "revoke"
    assert len(client.calls) == 2


def test_phase2_revoke_on_fabricated_evidence():
    """Evidence not a substring of any side/resolved → fabricated → squash."""
    unit = _unit(
        base="def f():\n    x = 1\n",
        current="def f():\n    x = 1\n    y = 2\n",
        replayed="def f():\n    x = 1\n    z = 3\n",
        resolved="def f():\n    x = 1\n    y = 2\n",  # z = 3 absent
    )
    cand = _candidate(resolved="def f():\n    x = 1\n    y = 2\n")
    client = _FakeClient([
        _VERDICT_DROPS_REPLAYED,
        '{"original_verdict_accurate": true, "reasoning": "missing", '
        '"evidence_snippet": "this string does not exist anywhere"}',  # fabricated
    ])
    res = _verify(client, unit, cand)
    assert res.passed is True  # squashed (fabricated)
    assert res.features["verifier_reassessment_outcome"] == "revoke"


def test_phase2_hold_on_grounded_evidence():
    """Evidence is a verbatim substring of a genuinely-absent side → stand."""
    unit = _unit(
        base="def f():\n    x = 1\n",
        current="def f():\n    x = 1\n    y = 2\n",
        replayed="def f():\n    x = 1\n    z = 3\n",
        resolved="def f():\n    x = 1\n    y = 2\n",  # z = 3 genuinely absent
    )
    cand = _candidate(resolved="def f():\n    x = 1\n    y = 2\n")
    client = _FakeClient([
        _VERDICT_DROPS_REPLAYED,
        '{"original_verdict_accurate": true, "reasoning": "z missing", '
        '"evidence_snippet": "z = 3"}',  # verbatim from replayed, absent from resolved
    ])
    res = _verify(client, unit, cand)
    assert res.passed is False  # stands — critic is right
    assert res.features["verifier_reassessment_outcome"] == "hold"


def test_phase2_revoke_when_evidence_actually_present():
    """Evidence is present in the resolved text → critic is wrong → squash."""
    unit = _unit(
        base="def f():\n    x = 1\n",
        current="def f():\n    x = 1\n    y = 2\n",
        replayed="def f():\n    x = 1\n    z = 3\n",
        resolved="def f():\n    x = 1\n    y = 2\n    z = 3\n",  # both present
    )
    cand = _candidate(resolved="def f():\n    x = 1\n    y = 2\n    z = 3\n")
    # Note: this case would hit Phase 3 (unanimous) first. To isolate Phase 2,
    # disable the guardrail so only reflection runs.
    cfg = ValidationConfig()
    cfg.enable_verifier_guardrail = False
    client = _FakeClient([
        _VERDICT_DROPS_REPLAYED,
        '{"original_verdict_accurate": true, "reasoning": "z missing", '
        '"evidence_snippet": "z = 3"}',  # present in resolved → critic wrong
    ])
    res = _verify(client, unit, cand, cfg=cfg)
    assert res.passed is True  # squashed (evidence is actually present)
    assert res.features["verifier_reassessment_outcome"] == "revoke"


def test_phase2_skipped_below_coverage_floor():
    """Min ratio < floor → no reflection, original verdict stands.

    Uses function-level entities so ``preservation_coverage`` computes a real
    ratio (module-level assignments aren't enumerated, so they'd default to 1.0).
    """
    base = "def base_fn():\n    return 0\n"
    # Replay side adds two functions; resolution drops both → ratio 0.0.
    replayed = base + "def alpha():\n    return 1\n\ndef beta():\n    return 2\n"
    current = base + "def gamma():\n    return 3\n"
    resolved = base + "def gamma():\n    return 3\n"  # alpha + beta dropped
    unit = _unit(base=base, current=current, replayed=replayed, resolved=resolved)
    cand = _candidate(resolved=resolved)
    client = _FakeClient([_VERDICT_DROPS_REPLAYED])
    res = _verify(client, unit, cand)
    assert res.passed is False
    assert res.features["verifier_reassessed"] is False
    assert len(client.calls) == 1  # no reassessment


# ---------------------------------------------------------------------------
# Toggles off (regression guards)
# ---------------------------------------------------------------------------


def test_guardrail_disabled_no_suppress():
    unit = _unit(
        base="def f():\n    x = 1\n",
        current="def f():\n    x = 1\n    y = 2\n",
        replayed="def f():\n    x = 1\n    z = 3\n",
        resolved="def f():\n    x = 1\n    y = 2\n    z = 3\n",
    )
    cand = _candidate(resolved="def f():\n    x = 1\n    y = 2\n    z = 3\n")
    cfg = ValidationConfig()
    cfg.enable_verifier_guardrail = False
    cfg.enable_verifier_reflection = False  # isolate: no suppression at all
    client = _FakeClient([_VERDICT_DROPS_REPLAYED])
    res = _verify(client, unit, cand, cfg=cfg)
    assert res.passed is False  # not suppressed
    assert res.features["verifier_guardrail_suppressed"] is False


def test_reflection_disabled_no_reassessment():
    unit = _unit(
        base="def f():\n    x = 1\n",
        current="def f():\n    x = 1\n    y = 2\n",
        replayed="def f():\n    x = 1\n    z = 3\n",
        resolved="def f():\n    x = 1\n    y = 2\n",  # z = 3 absent
    )
    cand = _candidate(resolved="def f():\n    x = 1\n    y = 2\n")
    cfg = ValidationConfig()
    cfg.enable_verifier_reflection = False
    client = _FakeClient([_VERDICT_DROPS_REPLAYED])
    res = _verify(client, unit, cand, cfg=cfg)
    assert res.passed is False  # stands (no reflection to revoke)
    assert res.features["verifier_reassessed"] is False
    assert len(client.calls) == 1


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


def test_telemetry_features_set_on_suppress():
    unit = _unit(
        base="def f():\n    x = 1\n",
        current="def f():\n    x = 1\n    y = 2\n",
        replayed="def f():\n    x = 1\n    z = 3\n",
        resolved="def f():\n    x = 1\n    y = 2\n    z = 3\n",
    )
    cand = _candidate(resolved="def f():\n    x = 1\n    y = 2\n    z = 3\n")
    client = _FakeClient([_VERDICT_DROPS_REPLAYED])
    res = _verify(client, unit, cand)
    assert res.features["verifier_guardrail_suppressed"] is True
    assert res.features["verifier_reassessed"] is False
    assert res.features["verifier_reassessment_outcome"] == ""


# ---------------------------------------------------------------------------
# Preserves-both verdict (no guardrail needed)
# ---------------------------------------------------------------------------


def test_preserves_both_no_guardrail_fires():
    unit = _unit(
        base="def f():\n    x = 1\n",
        current="def f():\n    x = 1\n    y = 2\n",
        replayed="def f():\n    x = 1\n    z = 3\n",
        resolved="def f():\n    x = 1\n    y = 2\n    z = 3\n",
    )
    cand = _candidate(resolved="def f():\n    x = 1\n    y = 2\n    z = 3\n")
    client = _FakeClient([_VERDICT_PRESERVES_BOTH])
    res = _verify(client, unit, cand)
    assert res.passed is True
    assert res.features["verifier_guardrail_suppressed"] is False
    assert len(client.calls) == 1


# ---------------------------------------------------------------------------
# Round 43 — _deterministic_preservation comment-blanking
# ---------------------------------------------------------------------------


def test_r43_deterministic_preservation_blanks_comments():
    """r43 (HIGH): ``_deterministic_preservation._toks`` used raw
    ``re.findall(r'\\w+')`` with no comment/string blanking. A token that
    survived only inside a comment (e.g. a side's added ``validate`` call that
    the merge COMMENTED OUT: ``# validate(x)``) was found in the merged text →
    ``dropped_replayed_additions=False`` → ``unanimous=True`` → the Phase 3
    critic guardrail suppressed a CORRECT critic flag (silent wrong accept of a
    dropped safety check). Now blanks comments/strings first."""
    from capybase.conflict_model import ConflictUnit, ConflictSide
    unit = ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="def f():\n    pass\n"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="def f():\n    pass\n"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="def f():\n    validate(x)\n"),
        original_worktree_text="", marker_span=None,
    )
    cand = _candidate(resolved="def f():\n    # validate(x)\n")
    dp = _deterministic_preservation(
        unit, cand,
        cur_lines="def f():\n    pass\n",
        rep_lines="def f():\n    validate(x)\n",
        base_lines="def f():\n    pass\n",
    )
    # ``validate`` was replayed's real addition; the merge commented it out (a
    # genuine drop). The token check must detect the drop (not find ``validate``
    # in the comment), so unanimous=False and the critic flag is NOT suppressed.
    assert dp.dropped_replayed_additions is True, (
        f"comment-leak → dropped_replayed_additions=False (critic false-suppressed): "
        f"dp={dp}"
    )
    assert dp.unanimous is False
