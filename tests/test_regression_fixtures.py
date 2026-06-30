"""Regression harness for hand-authored conflict fixtures (#8).

Two tracks, dispatched by the fixture's expected outcome:

- **Resolved-text** fixtures (``expected_resolved``): driven through the
  structural resolver (for ``expected_via: deterministic`` — zero LLM calls) or
  the engine + quality scorer. Repo-free. Proves the pipeline produces the
  canonical merge for each real conflict shape.

- **Escalation** fixtures (``expected_escalated``): driven through a synthesized
  git repo + the full orchestrator (escalation is orchestrator-only). Proves
  capybase escalates rather than guessing a broken merge.

The fixtures live in ``tests/fixtures/regression/`` (committed source-of-truth,
unlike the gitignored session/realworld datasets). Adding a fixture is the way
to grow release confidence: each is a real conflict shape with a known-correct
outcome.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.quality import _is_correct
from capybase.structural_resolver import resolve_structurally
from tests.conftest import git
from tests.regression_loader import load_regression_cases, resolved_cases

CASES = load_regression_cases()
RESOLVED_CASES = resolved_cases()
ESCALATED_CASES = [c for c in CASES if c.expected_escalated]


def _unit_from(case) -> ConflictUnit:
    """Build a ConflictUnit from a fixture's three sides."""
    return ConflictUnit(
        session_id="regression", step_index=0, path=case.path,
        language=case.language, conflict_type=case.conflict_type,
        unit_id=case.id, unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=case.base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=case.current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=case.replayed),
        original_worktree_text=case.base, marker_span=(0, 0),
    )


# ---------------------------------------------------------------------------
# Track 1: resolved-text fixtures (repo-free)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", RESOLVED_CASES, ids=[c.id for c in RESOLVED_CASES])
def test_resolved_fixture(case):
    """A resolved-text fixture must produce its canonical merge."""
    unit = _unit_from(case)

    if case.expected_via == "deterministic":
        # The structural/union rules should resolve it with ZERO LLM calls, and
        # the result must match the expected text (normalized).
        r = resolve_structurally(unit)
        assert r.resolved, (
            f"{case.id}: expected a deterministic ({case.expected_via}) resolution "
            f"but the structural resolver declined (rule={r.rule})"
        )
        assert _is_correct(r.text, case.expected_resolved), (
            f"{case.id}: deterministic resolution did not match expected.\n"
            f"  got: {r.text!r}\n  exp: {case.expected_resolved!r}"
        )
    else:
        # Non-deterministic: drive the engine with a fake client returning the
        # expected text, then verify via the quality scorer (the same correctness
        # check calibration uses). This proves the expected text is a valid,
        # spliceable resolution the engine accepts.
        from capybase.adapters.llm_openai import LLMResponse
        from capybase.config import ModelConfig
        from capybase.context_builder import ContextBuilder
        from capybase.resolution_engine import ResolutionEngine

        class _Client:
            def complete(self, messages, *, model, temperature, max_tokens, json_mode):
                return LLMResponse(text=json.dumps({
                    "resolved_text": case.expected_resolved,
                    "explanation": "regression fixture",
                }))

        engine = ResolutionEngine(ModelConfig(model="fake"), client=_Client())
        candidates = engine.propose(unit, ContextBuilder().build(unit), n_samples=1)
        assert candidates, f"{case.id}: engine produced no candidate"
        assert _is_correct(candidates[0].resolved_text, case.expected_resolved), (
            f"{case.id}: engine candidate did not match expected.\n"
            f"  got: {candidates[0].resolved_text!r}\n  exp: {case.expected_resolved!r}"
        )


# ---------------------------------------------------------------------------
# Track 2: escalation fixtures (synthesized repo + orchestrator)
# ---------------------------------------------------------------------------


def _make_conflicted_repo(repo: Path, case, *, path: str | None = None) -> Path:
    """Synthesize a git repo stopped at a UU conflict from a fixture's sides.

    Mirrors the ``_make_*_conflict`` idiom in test_structural_orchestrator.py:
    commit base → branch feat (replayed) + main (current) → ``git rebase main``
    → conflict. The conflicted path defaults to the fixture's ``path``.
    """
    p = path or case.path
    (repo / p).write_text(case.base)
    git(repo, "add", p); git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    (repo / p).write_text(case.replayed)
    git(repo, "add", p); git(repo, "commit", "-q", "-m", "replayed")
    git(repo, "checkout", "-q", "main")
    (repo / p).write_text(case.current)
    git(repo, "add", p); git(repo, "commit", "-q", "-m", "upstream")
    git(repo, "checkout", "-q", "feat")
    r = git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, f"{case.id}: expected a rebase conflict"
    return repo


@pytest.mark.parametrize("case", ESCALATED_CASES, ids=[c.id for c in ESCALATED_CASES])
def test_escalation_fixture(case, tmp_path: Path):
    """An escalation fixture: capybase must ESCALATE, never guess a broken merge.

    The fixture's three sides produce a conflict; a client returning a broken
    (marker-leaking) candidate every retry must drive the orchestrator to
    escalate rather than accept a bad merge.
    """
    from capybase.adapters.llm_openai import LLMResponse
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator
    from capybase.resolution_engine import ResolutionEngine

    repo = tmp_path
    git(repo, "init", "-q", "-b", "main")
    _make_conflicted_repo(repo, case)

    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"

    class _LeakingClient:
        """Always returns leaked conflict markers → forces escalation."""
        def complete(self, messages, *, model, temperature, max_tokens, json_mode):
            return LLMResponse(text=json.dumps({
                "resolved_text": "    x\n<<<<<<< still leaked\n",
            }))

    engine = ResolutionEngine(cfg.model, client=_LeakingClient())
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert result.escalated, (
        f"{case.id}: expected escalation but the run did not escalate "
        f"(reason={result.reason!r})"
    )
    if case.escalation_reason_substr:
        reason = (result.reason or "").lower()
        assert case.escalation_reason_substr.lower() in reason, (
            f"{case.id}: escalated but reason {result.reason!r} did not contain "
            f"{case.escalation_reason_substr!r}"
        )
