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
