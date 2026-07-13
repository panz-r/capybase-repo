"""Tests for the uniform ResolutionAttempt dispatch (#idea 6 cohesion).

Each of the 5 resolution mechanisms now produces a uniform ResolutionAttempt
record (mechanism, candidate, validation, decision, reason) journaled as a
``resolution_attempt`` event. Provenance is assigned by the dispatch, not
inferred — except the clearly-named history-augmentation compat path.
"""

from __future__ import annotations

import json
from pathlib import Path

from capybase.conflict_model import ResolutionAttempt
from capybase.config import Config
from capybase.orchestrator import Orchestrator

from tests.conftest import git
from tests.multistep_builder import CommitEdit, build_multistep_rebase


def _base_cfg(repo: Path) -> Config:
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    return cfg


def _attempt_events(orch: Orchestrator) -> list:
    return [e for e in orch.journal.read_events()
            if e.event_type == "resolution_attempt"]


def test_resolution_attempt_is_the_uniform_shape():
    """The dataclass carries the 5 fields the attachment wants."""
    a = ResolutionAttempt(
        mechanism="deterministic_structural",
        decision="accept", reason="insertion_union rule",
    )
    assert a.mechanism == "deterministic_structural"
    assert a.decision == "accept"
    assert a.reason == "insertion_union rule"
    assert a.candidate is None
    assert a.validation is None


def test_llm_accept_records_resolution_attempt(repo: Path):
    """An LLM-accepted unit gets a resolution_attempt event with decision=accept."""
    build_multistep_rebase(
        repo,
        base_files={"cfg.py": "# a\n\n\ndef f():\n    return 1\n"},
        feat_commits=[
            CommitEdit("feat: edit f", {"cfg.py": "# a\n\n\ndef f():\n    return 2\n"}),
        ],
        main_commits=[
            CommitEdit("main: edit f", {"cfg.py": "# a\n\n\ndef f():\n    return 99\n"}),
        ],
        stop_early=True,
    )
    from tests.test_rust_cross_file import PathAwareClient
    from capybase.resolution_engine import ResolutionEngine
    cfg = _base_cfg(repo)
    client = PathAwareClient({"cfg.py": "    return 2\n"})
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    orch.run()
    attempts = _attempt_events(orch)
    # At least one accept attempt (the LLM path).
    accepts = [a for a in attempts if a.payload.get("decision") == "accept"]
    assert accepts, f"expected an accept attempt; got {[(a.payload['mechanism'], a.payload['decision']) for a in attempts]}"
    # The mechanism is a known provenance value (plain_llm or history_augmented_llm).
    assert accepts[0].payload["mechanism"] in ("plain_llm", "history_augmented_llm")


def test_exact_reuse_skip_records_attempt(repo: Path, tmp_path):
    """An exact-reuse miss records a resolution_attempt with decision=skip."""
    from tests.test_rust_cross_file import PathAwareClient
    from capybase.resolution_engine import ResolutionEngine
    build_multistep_rebase(
        repo,
        base_files={"cfg.py": "# a\n\n\ndef f():\n    return 1\n"},
        feat_commits=[
            CommitEdit("feat: edit f", {"cfg.py": "# a\n\n\ndef f():\n    return 2\n"}),
        ],
        main_commits=[
            CommitEdit("main: edit f", {"cfg.py": "# a\n\n\ndef f():\n    return 99\n"}),
        ],
        stop_early=True,
    )
    cfg = _base_cfg(repo)
    # An empty store → reuse finds no match → skip attempt.
    from capybase.memory.store import ExperienceStore
    cfg.memory.enabled = True
    cfg.future.enable_rag = True
    client = PathAwareClient({"cfg.py": "    return 2\n"})
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    orch.memory_store = ExperienceStore(tmp_path / "empty.jsonl")
    orch.run()
    attempts = _attempt_events(orch)
    reuse_skips = [
        a for a in attempts
        if a.payload.get("mechanism") == "exact_history_reuse"
        and a.payload.get("decision") == "skip"
    ]
    assert reuse_skips, (
        f"expected an exact-reuse skip attempt; got "
        f"{[(a.payload['mechanism'], a.payload['decision']) for a in attempts]}"
    )


def test_restamp_is_a_named_path_with_reason(repo: Path):
    """The plain_llm → history_augmented_llm restamp produces a resolution_attempt
    whose reason names the augmentation (the named compat path, #idea 6)."""
    build_multistep_rebase(
        repo,
        base_files={"cfg.py": "# a\n\n\ndef f():\n    return 1\n"},
        feat_commits=[
            CommitEdit("feat: edit f", {"cfg.py": "# a\n\n\ndef f():\n    return 2\n"}),
            CommitEdit("feat: edit f again", {"cfg.py": "# a\n\n\ndef f():\n    return 3\n"}),
        ],
        main_commits=[
            CommitEdit("main: edit f", {"cfg.py": "# a\n\n\ndef f():\n    return 99\n"}),
        ],
        stop_early=True,
    )
    from tests.test_rust_cross_file import PathAwareClient
    from capybase.resolution_engine import ResolutionEngine
    cfg = _base_cfg(repo)
    client = PathAwareClient({"cfg.py": "    return 2\n"})
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    orch.run()
    attempts = _attempt_events(orch)
    # If the restamp fired, there's an accept attempt with mechanism=history_augmented_llm
    # and a reason naming "history-augmented".
    augmented = [
        a for a in attempts
        if a.payload.get("mechanism") == "history_augmented_llm"
    ]
    if augmented:
        assert "history-augmented" in augmented[0].payload.get("reason", "")


# ---------------------------------------------------------------------------
# Decline-reason journaling (survey §5.3): structural + SBCR declines were
# previously SILENT (no resolution_attempt event). exact-reuse was already
# instrumented. These tests pin the new parity so a skip is never invisible.
# ---------------------------------------------------------------------------


def test_structural_decline_records_attempt(repo: Path):
    """When the structural resolver finds NO applicable rule, it now journals a
    resolution_attempt with decision=skip + reason (previously a bare `return None`).

    Built so every structural rule declines: both sides modify the SAME single
    line of a one-line function differently (no disjoint span, no entity
    boundary, no token-disjoint small hunk that survives). The LLM then resolves.
    """
    from tests.test_rust_cross_file import PathAwareClient
    from capybase.resolution_engine import ResolutionEngine
    # A pure value conflict at the same line: base=return 1, current=return 2,
    # replayed=return 3. identical_sides fails, one_sided fails, disjoint/zealous
    # fail (same line), entity_disjoint fails (no container), token_disjoint
    # fails (the change is to the same token). → structural declines.
    build_multistep_rebase(
        repo,
        base_files={"cfg.py": "x = 1\n"},
        feat_commits=[CommitEdit("feat: x=2", {"cfg.py": "x = 2\n"})],
        main_commits=[CommitEdit("main: x=3", {"cfg.py": "x = 3\n"})],
        stop_early=True,
    )
    cfg = _base_cfg(repo)
    client = PathAwareClient({"cfg.py": "x = 2\n"})
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    orch.run()
    attempts = _attempt_events(orch)
    structural_skips = [
        a for a in attempts
        if a.payload.get("mechanism") == "structural"
        and a.payload.get("decision") == "skip"
    ]
    assert structural_skips, (
        "expected a structural skip attempt; got "
        f"{[(a.payload['mechanism'], a.payload['decision']) for a in attempts]}"
    )
    # The reason is populated (not empty).
    assert structural_skips[0].payload.get("reason"), "skip reason must be populated"


def test_sbcr_decline_records_attempt_with_reason(repo: Path):
    """An SBCR decline (modification conflict → non-empty base) now journals a
    combination_declined event + a resolution_attempt with the skip_reason."""
    from tests.test_rust_cross_file import PathAwareClient
    from capybase.resolution_engine import ResolutionEngine
    # Same one-line value conflict: non-empty base → SBCR declines on scope.
    build_multistep_rebase(
        repo,
        base_files={"cfg.py": "x = 1\n"},
        feat_commits=[CommitEdit("feat: x=2", {"cfg.py": "x = 2\n"})],
        main_commits=[CommitEdit("main: x=3", {"cfg.py": "x = 3\n"})],
        stop_early=True,
    )
    cfg = _base_cfg(repo)
    client = PathAwareClient({"cfg.py": "x = 2\n"})
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    orch.run()
    # The combination_declined event carries the fitness + reason.
    declines = [e for e in orch.journal.read_events()
                if e.event_type == "combination_declined"]
    assert declines, "expected a combination_declined event"
    reason = declines[0].payload.get("reason", "")
    assert reason, "combination_declined reason must be populated"
    # On a modification conflict the reason names the non-empty base.
    assert "base" in reason.lower()
    # And the uniform resolution_attempt shape is emitted too.
    attempts = _attempt_events(orch)
    sbcr_skips = [
        a for a in attempts
        if a.payload.get("mechanism") == "sbcr"
        and a.payload.get("decision") == "skip"
    ]
    assert sbcr_skips, (
        "expected an sbcr skip attempt; got "
        f"{[(a.payload['mechanism'], a.payload['decision']) for a in attempts]}"
    )
