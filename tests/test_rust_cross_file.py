"""Cross-file (whole-crate) Rust verification: multi-file rebase conflicts.

When a rebase stops with conflicts in TWO Rust files of one crate at once, a
per-file ``cargo check`` fails because the sibling file still holds raw
``<<<<<<<`` markers (``error: encountered diff marker``). The orchestrator's
two-phase resolution fixes this: Phase 1 writes every resolved file before any
cargo check, so each check sees a marker-free crate.

These drive the ``rust_multi_file_conflicted_repo`` fixture through the full
orchestrator. Requires cargo (skipped on CI without a toolchain).
"""

from __future__ import annotations

import json
import shutil

import pytest

from capybase.adapters.llm_openai import LLMResponse
from capybase.config import Config
from capybase.orchestrator import Orchestrator
from capybase.resolution_engine import ResolutionEngine

from tests.conftest import git

CARGO = shutil.which("cargo")
skip_no_cargo = pytest.mark.skipif(CARGO is None, reason="cargo not installed")


def _config(repo) -> Config:
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = None
    cfg.tests.final = None
    return cfg


def _payload(text: str) -> str:
    return json.dumps(
        {"resolved_text": text, "explanation": "merge", "self_reported_confidence": 0.8}
    )


class PathAwareClient:
    """Returns the correct merge for whichever file the prompt is about.

    The orchestrator resolves files in units_by_path order (which mirrors
    git's unmerged-path listing and isn't guaranteed), so a fixed response
    sequence would be order-dependent. This client inspects the prompt text
    for the file path and returns that file's correct merge.
    """

    def __init__(self, by_path: dict[str, str]):
        self.by_path = by_path
        self.calls = 0

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        self.calls += 1
        prompt = messages[-1]["content"]
        for path, text in self.by_path.items():
            if path in prompt:
                return LLMResponse(text=_payload(text))
        # Fallback: first entry.
        return LLMResponse(text=_payload(next(iter(self.by_path.values()))))


@skip_no_cargo
def test_multi_file_rust_conflict_resolves_and_compiles(rust_multi_file_conflicted_repo):
    """Two simultaneous Rust conflicts in one crate resolve and compile.

    This is the regression test for cross-file verification: without the
    two-phase fix, the first file's cargo check fails because the second file
    still has raw markers. With the fix, both files are written resolved first,
    so the whole crate compiles and the rebase continues.
    """
    repo = rust_multi_file_conflicted_repo["repo"]
    # The correct merge for each file's single conflict block:
    # config.rs (the new() line): keep upstream's port 9090.
    r_config = "    pub fn new() -> Self { Config { port: 9090 } }"
    # server.rs (the label line): combine both label styles.
    r_server = (
        'pub fn label(c: &Config) -> String { format!("[PORT]={}", c.port) }'
    )
    client = PathAwareClient({
        "src/config.rs": r_config,
        "src/server.rs": r_server,
    })
    cfg = _config(repo)
    # The correct merge for the config.rs port-number conflict takes ONE side's
    # value (9090) — two different port numbers can't be combined, so taking one
    # side's value is the semantically correct resolution, not a missed merge.
    # This test exercises cross-file (whole-crate) cargo verification, not the
    # risk engine's side-preservation judgment, so relax the two heuristics that
    # would otherwise flag the intentional one-sided port pick
    # (both_sides_represented / preservation_heuristic) and force a retry cycle.
    cfg.validation.reject_if_drops_a_side = False
    cfg.validation.reject_if_copies_one_side = False
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert not result.escalated, result.reason
    config_text = (repo / "src" / "config.rs").read_text()
    server_text = (repo / "src" / "server.rs").read_text()
    assert "<<<<<<<" not in config_text
    assert "<<<<<<<" not in server_text
    # Both files carry the correct merge.
    assert "port: 9090" in config_text
    assert "[PORT]={}" in server_text
    # The rebase continued to completion.
    git(repo, "rebase", "--abort", check=False)
    log = git(repo, "log", "--oneline").stdout
    assert "feat" in log or "port 7070" in log


@skip_no_cargo
def test_multi_file_rust_conflict_inspect_extracts_both(rust_multi_file_conflicted_repo):
    """Inspect detects both conflicted files without mutating the worktree."""
    repo = rust_multi_file_conflicted_repo["repo"]
    before_config = (repo / "src" / "config.rs").read_text()
    before_server = (repo / "src" / "server.rs").read_text()
    orch = Orchestrator(_config(repo), repo=str(repo))
    result = orch.inspect()
    assert not result.escalated
    assert "src/config.rs" in result.units_by_path
    assert "src/server.rs" in result.units_by_path
    # worktree untouched
    assert (repo / "src" / "config.rs").read_text() == before_config
    assert (repo / "src" / "server.rs").read_text() == before_server
