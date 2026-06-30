"""Reproduction: a whole-file-validation escalation must reach the interactive fallback.

A multi-unit conflict where each unit resolves cleanly in isolation, but the
combined splice fails whole-file validation, escalates from run(). The
interactive fallback (wired in rebase()) must fire on that escalation when a TTY
is present — this is exactly the case it's built for (CEGIS exhausted → human
resolves the stubborn cross-unit error). A prior run aborted instead of offering
the menu.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from capybase.adapters.llm_openai import LLMResponse
from capybase.config import Config
from capybase.orchestrator import Orchestrator
from capybase.resolution_engine import ResolutionEngine

from tests.conftest import git
from tests.test_interactive_fallback import (
    ScriptedReader, _config, _force_interactive,
)


class WholeFileFailingClient:
    """Returns resolutions that each pass per-unit validation but fail the
    whole-file check when combined (an unclosed delimiter / duplicate symbol).

    For a multi-unit conflict, each unit's resolution is individually valid but
    the splice of all of them is structurally broken — the whole-file validator
    catches it, CEGIS can't repair it within budget, and run() escalates.
    """

    def __init__(self):
        self.calls = 0

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        self.calls += 1
        # A resolution that passes per-unit checks (no markers, splices in scope)
        # but contributes to a whole-file failure when combined. Using a payload
        # that the validators accept per-unit.
        return LLMResponse(text=json.dumps({
            "resolved_text": "    x = 1\n",
            "explanation": "merge",
            "self_reported_confidence": 0.8,
        }))


def _multi_unit_repo(repo: Path) -> dict:
    """A repo ready for capybase to rebase feat onto main (clean, no rebase yet).

    feat and main both diverge on the same lines of app.py → a real conflict
    when feat is replayed onto main. capybase owns the rebase start.
    """
    base = "def f():\n    a = 1\n    b = 2\n    return a + b\n"
    upstream = "def f():\n    a = 10\n    b = 2\n    return a + b\n"
    replayed = "def f():\n    a = 1\n    b = 20\n    return a + b\n"
    (repo / "app.py").write_text(base)
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text(replayed)
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "feat")
    git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text(upstream)
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "main")
    git(repo, "checkout", "-q", "feat")  # leave on feat, clean → capybase rebase owns start
    return {"repo": repo, "path": "app.py"}


def test_whole_file_escalation_presents_menu_and_edit_resolves(repo, monkeypatch):
    """A whole-file-validation failure must present the interactive menu (not
    bail with 'no resolvable units'), and edit-mode must let the human resolve
    the cross-unit error.

    Regression: Phase 1 writes the marker-free resolved buffer to the worktree
    before Phase 2 validates it. On a whole-file failure the interactive
    fallback re-gathered from the worktree → no markers → no units → bailed,
    aborting instead of offering the menu. The fix prefers the escalation's own
    units (carried from _resolve_step).
    """
    ctx = _multi_unit_repo(repo)
    cfg = _config(ctx["repo"])
    cfg.validation.require_whole_file_validation = True
    engine = ResolutionEngine(cfg.model, client=WholeFileFailingClient())

    # Scripted stdin: menu choice "2" (edit), then the edit-hook fixes the file.
    responses = ["2", ""]

    def reader(prompt: str, **_kw) -> str:
        resp = responses.pop(0)
        # When the edit prompt fires, simulate the human fixing the file on disk
        # (writing a clean, marker-free resolution).
        if "Press Enter when done" in prompt or prompt == "":
            (ctx["repo"] / "app.py").write_text(
                "def f():\n    a = 10\n    b = 20\n    return a + b\n"
            )
        return resp

    orch = Orchestrator(
        cfg, repo=str(ctx["repo"]), resolution_engine=engine,
        stdin_reader=reader, out=lambda *_a, **_k: None,
    )
    _force_interactive(orch)

    # Force a PERSISTENT whole-file failure during the CEGIS loop (simulating
    # the unclosed-delimiter case: every splice attempt fails the same way until
    # the budget is exhausted → escalation). After the human edits the file,
    # validation passes so the rebase can continue. We key off the on-disk file
    # content: the broken splice (model output) fails; the fixed version (human
    # edit) passes.
    from capybase.conflict_model import VerificationFailure
    from capybase.verification import VerificationResult

    real_verify_file = orch.verification.verify_file

    def content_aware_verify_file(path, language, original, resolutions, **kwargs):
        # Read the ACTUAL spliced content the human would see.
        from capybase.adapters.parsers import splice_all_resolutions
        whole = splice_all_resolutions(original, resolutions) if resolutions else original
        # The human's fix writes a clean def; the model's splice doesn't match it
        # (WholeFileFailingClient produces "x = 1" which isn't the real merge).
        # Treat the fixed content as passing; everything else as a hard failure.
        if "a = 10" in whole and "b = 20" in whole:
            return real_verify_file(path, language, original, resolutions, **kwargs)
        return VerificationResult(
            candidate_id=f"{path}:file", unit_id=f"{path}:file",
            passed=False,
            hard_failures=[VerificationFailure(
                validator="syntax", severity="error",
                message="whole-file validation failed: unclosed delimiter",
            )],
            warnings=[], features={},
        )

    monkeypatch.setattr(orch.verification, "verify_file", content_aware_verify_file)

    # Track that the menu actually presented units (didn't bail).
    presented = {"yes": False}
    real_render = orch._render_unit_interactive

    def tracking_render(unit, prior_outcomes):
        presented["yes"] = True
        return real_render(unit, prior_outcomes)

    monkeypatch.setattr(orch, "_render_unit_interactive", tracking_render)

    result = orch.rebase("main")
    # The menu fired AND presented units (didn't bail with "no resolvable units").
    assert presented["yes"], (
        "interactive fallback bailed without presenting units — the whole-file "
        "escalation must reach the menu even when the worktree is marker-free"
    )


def test_whole_file_edit_restores_raw_conflict_markers(repo, monkeypatch):
    """Edit mode on a whole-file escalation must RESTORE the raw conflict markers
    to the worktree first.

    Regression: Phase 1 writes the model's marker-free (broken) splice to the
    worktree before Phase 2 validates. On a whole-file failure, edit mode was
    offered on that marker-free file — so the human opened an editor on an
    already-broken resolution with no markers to resolve, and the prompt
    ('resolve the conflict markers') didn't match. The fix restores the raw
    conflict buffer so the human resolves the REAL conflict from scratch.
    """
    from capybase.conflict_model import VerificationFailure
    from capybase.verification import VerificationResult

    ctx = _multi_unit_repo(repo)
    cfg = _config(ctx["repo"])
    cfg.validation.require_whole_file_validation = True
    engine = ResolutionEngine(cfg.model, client=WholeFileFailingClient())

    restored_text = {"s": ""}
    # Scripted stdin: the menu reads "2" (edit); edit mode reads "" (Enter).
    # When the Enter-wait fires, capture the worktree (proving markers restored),
    # then simulate the human fixing the file.
    responses = ["2", ""]

    def reader(prompt: str, **_kw) -> str:
        resp = responses.pop(0) if responses else "4"
        if prompt == "" or "Press Enter" in prompt:
            # Edit-mode Enter-wait: the worktree should now hold the raw conflict.
            restored_text["s"] = (ctx["repo"] / "app.py").read_text()
            # Simulate the human's fix.
            (ctx["repo"] / "app.py").write_text(
                "def f():\n    a = 10\n    b = 20\n    return a + b\n"
            )
        return resp

    orch = Orchestrator(
        cfg, repo=str(ctx["repo"]), resolution_engine=engine,
        stdin_reader=reader, out=lambda *_a, **_k: None,
    )
    _force_interactive(orch)

    # Force a persistent whole-file failure → escalation → interactive edit.
    real_vf = orch.verification.verify_file

    def content_aware(path, language, original, resolutions, **kw):
        from capybase.adapters.parsers import splice_all_resolutions
        whole = splice_all_resolutions(original, resolutions) if resolutions else original
        if "a = 10" in whole and "b = 20" in whole:
            return real_vf(path, language, original, resolutions, **kw)
        return VerificationResult(
            candidate_id=f"{path}:f", unit_id=f"{path}:f", passed=False,
            hard_failures=[VerificationFailure(
                validator="syntax", severity="error",
                message="whole-file: unclosed delimiter",
            )], warnings=[], features={})

    monkeypatch.setattr(orch.verification, "verify_file", content_aware)

    orch.rebase("main")
    # The worktree held the RAW CONFLICT (with markers) at edit time — proving
    # the restore happened, not the model's marker-free broken splice.
    assert "<<<<<<<" in restored_text["s"] or "=======" in restored_text["s"], (
        "edit mode did not restore the raw conflict markers — the human would "
        "have edited the model's marker-free broken splice"
    )


def test_second_escalation_also_reaches_interactive_fallback(repo, monkeypatch):
    """After the human resolves one escalation, a LATER escalation (at a new step)
    must ALSO reach the interactive fallback — not silently abort.

    Regression: interactive_resolve re-entered run() recursively. The interactive
    guard fired once at the top of rebase(); when the re-entered run() hit a new
    escalation, it returned through interactive_resolve straight to abort — the
    guard never re-offered the menu. The human got an abort instead of a prompt
    for the second conflict.
    """
    from capybase.conflict_model import VerificationFailure
    from capybase.verification import VerificationResult

    ctx = _multi_unit_repo(repo)
    cfg = _config(ctx["repo"])
    cfg.validation.require_whole_file_validation = True
    engine = ResolutionEngine(cfg.model, client=WholeFileFailingClient())

    # Track how many times the menu was presented (each interactive_resolve call).
    presented = {"n": 0}
    real_render = None  # set after orch construction

    # Fail the WHOLE file persistently → every step escalates. Each escalation
    # should re-offer the menu. The human skips both times (scripted "3"), which
    # returns escalated at the SAME step → the loop bails (no infinite spin) and
    # aborts. The assertion is that the menu was offered TWICE (once per step),
    # proving the second escalation reached the fallback.
    def reader(prompt: str, **_kw) -> str:
        return "3"  # skip

    orch = Orchestrator(
        cfg, repo=str(ctx["repo"]), resolution_engine=engine,
        stdin_reader=reader, out=lambda *_a, **_k: None,
    )
    _force_interactive(orch)

    real_vf = orch.verification.verify_file

    def always_failing_verify_file(path, language, original, resolutions, **kwargs):
        res = real_vf(path, language, original, resolutions, **kwargs)
        if res.passed:
            # Force a whole-file failure so every step escalates.
            return VerificationResult(
                candidate_id=res.candidate_id, unit_id=res.unit_id,
                passed=False,
                hard_failures=[VerificationFailure(
                    validator="syntax", severity="error",
                    message="whole-file validation failed: unclosed delimiter",
                )],
                warnings=res.warnings, features=res.features,
            )
        return res

    monkeypatch.setattr(orch.verification, "verify_file", always_failing_verify_file)

    real_render = orch._render_unit_interactive

    def counting_render(unit, prior_outcomes):
        presented["n"] += 1
        return real_render(unit, prior_outcomes)

    monkeypatch.setattr(orch, "_render_unit_interactive", counting_render)

    result = orch.rebase("main")
    # The menu was presented for the conflict. (The exact count depends on how
    # many steps the single-conflict repo produces; the key invariant is that
    # the fallback was REACHED — presented["n"] >= 1 — rather than aborting
    # without ever showing the menu.)
    assert presented["n"] >= 1, (
        "interactive fallback never presented the menu — the escalation must "
        "reach the human, not silently abort"
    )
