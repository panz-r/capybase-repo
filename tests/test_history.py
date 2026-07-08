"""Tests for the history-awareness substrate foundation (#history steps 1-5).

The data layer that lets capybase answer "where is this conflict in the commit
sequence, what later commits touch the same region?" Built from scratch (no v0
layer existed), attached read-only to the existing pipeline, degrading to
current behavior when history is unavailable.

Covers:
- RebasePlan / ReplayCommit: the source-sequence data model + serialization.
- RegionKey: the lightweight structural coordinate built from existing metadata.
- HistoryQueryService: answers per-conflict history questions from the plan.
- git_backend.replayed_commit_sequence + rebase_stopped_sha: the git primitives.
- The orchestrator wiring: plan generation at rebase start + replay-identity
  stamping at gather time.
"""

from __future__ import annotations

import json
from pathlib import Path

from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.history import (
    HistoryContext,
    HistoryQueryService,
    RebasePlan,
    ReplayCommit,
    RegionKey,
    region_key_from_unit,
)
from tests.conftest import git


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _commit(oid, parent, subject, files, index, body=""):
    return ReplayCommit(
        oid=oid, parent_oid=parent, subject=subject, body_summary=body,
        touched_files=files, diffstat={f: 1 for f in files}, patch_id="",
        index=index,
    )


def _unit(path="src/config.py", language="python", meta=None):
    return ConflictUnit(
        session_id="s", step_index=1, path=path, language=language,
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="x"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="y"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="z"),
        original_worktree_text="x", marker_span=(0, 0),
        structural_metadata=meta or {},
    )


def _plan():
    """A 4-commit replay sequence touching src/config.py and src/server.py."""
    return RebasePlan(
        source_commits=[
            _commit("c1", "base", "base", [], 0),
            _commit("c2", "c1", "Add strict validation", ["src/config.py"], 1),
            _commit("c3", "c2", "Add server endpoint", ["src/server.py"], 2),
            _commit("c4", "c3", "Rename parse to load_config", ["src/config.py"], 3),
        ],
        target_base_oid="base", target_tip_oid="target",
        source_tip_oid="c4", created_at="2026-07-01T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# RebasePlan + ReplayCommit: data model + serialization
# ---------------------------------------------------------------------------


def test_rebase_plan_roundtrip():
    plan = _plan()
    d = plan.to_dict()
    assert len(d["source_commits"]) == 4
    again = RebasePlan.from_dict(d)
    assert again == plan
    assert again.source_commits[1].subject == "Add strict validation"


def test_rebase_plan_lookup():
    plan = _plan()
    assert plan.commit_by_oid("c3").subject == "Add server endpoint"
    assert plan.commit_by_oid("nope") is None
    assert plan.index_of("c4") == 3
    assert plan.index_of("missing") is None


# ---------------------------------------------------------------------------
# RegionKey: built from existing structural metadata
# ---------------------------------------------------------------------------


def test_region_key_from_function_unit():
    u = _unit(meta={
        "enclosing_node_type": "function_definition",
        "enclosing_node_signature": "def parse(self):",
        "enclosing_node_span": [10, 20],
        "ast_fingerprint_base_outside": "abc123",
    })
    rk = region_key_from_unit(u)
    assert rk.kind == "function"
    assert rk.name == "def parse(self):"
    assert rk.start_line == 10 and rk.end_line == 20
    assert rk.structural_hash == "abc123"
    assert "src/config.py" in rk.display()


def test_region_key_degrades_to_unknown_without_metadata():
    u = _unit(meta={})  # no structural enrichment
    rk = region_key_from_unit(u)
    assert rk.kind == "unknown"
    assert rk.name is None
    assert rk.structural_hash == ""


def test_region_key_class_kind():
    u = _unit(meta={"enclosing_node_type": "class_definition",
                    "enclosing_node_signature": "class Config:"})
    assert region_key_from_unit(u).kind == "class"


def test_region_key_rust_impl():
    u = _unit(path="src/lib.rs", language="rust", meta={
        "enclosing_node_type": "impl_item", "enclosing_node_signature": "impl Config {"})
    assert region_key_from_unit(u).kind == "impl"


def test_region_key_trusts_parser_coarse_kind():
    """The abstract parser emits coarse kinds (``function``/``method``/...) as
    ``enclosing_node_type``. _coarse_kind must trust these directly rather than
    always falling through to the signature heuristic (the old tree-sitter-keyed
    _NODE_KIND_MAP always missed, forcing methods and keyword-less Family-A
    decls to ``unknown``). A method node_type → kind ``method``, not unknown."""
    u = _unit(meta={
        "enclosing_node_type": "method",
        "enclosing_node_signature": "validate(self):",  # no def/fn prefix → heuristic misses
        "enclosing_node_span": [10, 20],
    })
    assert region_key_from_unit(u).kind == "method"


# ---------------------------------------------------------------------------
# HistoryQueryService: answers per-conflict questions
# ---------------------------------------------------------------------------


def test_empty_service_returns_empty_context():
    qs = HistoryQueryService.empty()
    ctx = qs.for_conflict(_unit())
    assert ctx.source_commit_count == 0
    assert ctx.current_replay_commit is None
    assert ctx.to_features()["history_has_context"] is False


def test_context_identifies_future_file_and_region_touches():
    """Resolving c2 (src/config.py) → c4 later touches the same file AND region
    (the rename), but c3 (src/server.py) touches a different file."""
    qs = HistoryQueryService(_plan())
    u = _unit(meta={"enclosing_node_type": "function_definition",
                    "enclosing_node_signature": "def parse(self):"})
    ctx = qs.for_conflict(u, replayed_commit_oid="c2")
    assert ctx.source_commit_index == 1
    assert ctx.source_commit_count == 4
    # Future commits touching the same file.
    assert [c.subject for c in ctx.future_source_commits_touching_file] == [
        "Rename parse to load_config"
    ]
    # Of those, the one touching the same REGION (by name match on "parse").
    assert [c.subject for c in ctx.future_source_commits_touching_region] == [
        "Rename parse to load_config"
    ]


def test_context_no_future_touches_for_last_commit():
    qs = HistoryQueryService(_plan())
    u = _unit(meta={"enclosing_node_type": "function_definition",
                    "enclosing_node_signature": "def load_config:"})
    ctx = qs.for_conflict(u, replayed_commit_oid="c4")
    assert ctx.future_source_commits_touching_file == []
    assert not ctx.has_future_touches


def test_context_without_replayed_oid_still_returns_count():
    qs = HistoryQueryService(_plan())
    ctx = qs.for_conflict(_unit(), replayed_commit_oid=None)
    assert ctx.source_commit_count == 4
    assert ctx.source_commit_index is None
    assert ctx.current_replay_commit is None


def test_context_features_for_risk_spine():
    qs = HistoryQueryService(_plan())
    u = _unit(meta={"enclosing_node_type": "function_definition",
                    "enclosing_node_signature": "def parse:"})
    ctx = qs.for_conflict(u, replayed_commit_oid="c2")
    feats = ctx.to_features()
    assert feats["history_source_commit_index"] == 1
    assert feats["history_future_file_touch_count"] == 1
    assert feats["history_future_region_touch_count"] == 1
    assert feats["history_has_context"] is True


# ---------------------------------------------------------------------------
# git_backend: the replayed-sequence + stopped-sha primitives
# ---------------------------------------------------------------------------


def test_replayed_commit_sequence_enumerates_commits(repo: Path):
    """git rev-list --reverse yields the source commits oldest-first."""
    g = _build_sequence_repo(repo)
    from capybase.git_backend import GitBackend
    gb = GitBackend(repo)
    mb = gb.merge_base(g["feat_tip"], "main")
    assert mb is not None
    seq = gb.replayed_commit_sequence(mb, g["feat_tip"])
    subjects = [c["subject"] for c in seq]
    assert subjects == ["feat: add file a", "feat: edit file a"]
    assert seq[0]["touched_files"] == ["a.txt"]


def test_replayed_commit_sequence_empty_for_no_divergence(repo: Path):
    from capybase.git_backend import GitBackend
    (repo / "x.txt").write_text("x\n")
    git(repo, "add", "x.txt"); git(repo, "commit", "-q", "-m", "base")
    gb = GitBackend(repo)
    assert gb.replayed_commit_sequence("HEAD", "HEAD") == []


def test_rebase_stopped_sha_none_outside_rebase(repo: Path):
    from capybase.git_backend import GitBackend
    (repo / "x.txt").write_text("x\n")
    git(repo, "add", "x.txt"); git(repo, "commit", "-q", "-m", "base")
    gb = GitBackend(repo)
    assert gb.rebase_stopped_sha() is None


def _build_sequence_repo(repo: Path) -> dict:
    """A repo with a 2-commit feature branch diverging from main."""
    (repo / "base.txt").write_text("base\n")
    git(repo, "add", "base.txt"); git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "a.txt"); git(repo, "commit", "-q", "-m", "feat: add file a")
    (repo / "a.txt").write_text("a\nb\n")
    git(repo, "add", "a.txt"); git(repo, "commit", "-q", "-m", "feat: edit file a")
    feat_tip = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "checkout", "-q", "main")
    return {"feat_tip": feat_tip}


# ---------------------------------------------------------------------------
# Orchestrator wiring: plan generation + replay-identity stamping
# ---------------------------------------------------------------------------


def test_rebase_generates_and_persists_plan(repo: Path):
    """capybase rebase generates a rebase_plan.json with the source sequence."""
    import tempfile
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator

    # Build a divergent repo.
    (repo / "app.py").write_text("x = 1\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text("x = 2\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "feat: change x")
    git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text("x = 3\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "main: change x")
    git(repo, "checkout", "-q", "feat")

    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    # Start the rebase (it'll conflict immediately).
    from capybase.git_backend import GitError
    try:
        orch.rebase("main", interactive=False)
    except Exception:
        pass  # expected to conflict/escalate; we just want the plan written

    plan_path = orch.paths.root / "rebase_plan.json"
    assert plan_path.exists(), "rebase_plan.json not written at rebase start"
    plan_data = json.loads(plan_path.read_text())
    assert len(plan_data["source_commits"]) >= 1
    assert plan_data["source_commits"][0]["subject"] == "feat: change x"
