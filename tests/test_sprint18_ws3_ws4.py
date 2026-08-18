"""Sprint 18 WS3: deletion-respect swap (pre-stage resurrection repair).

tokio-0037/0046 shape: every conflict unit resolves correctly
(current_only), but git's auto-merge context OUTSIDE the markers
re-introduces content the upstream branch deleted. The end-of-rebase
resurrection scan stops the rebase — correct, but too late to repair. The
deletion-respect swap runs PRE-STAGE (merge-index stages still readable):
when the buffer carries upstream-deleted blocks and is otherwise the
upstream side, swap in the verified upstream side.
"""

from __future__ import annotations

from pathlib import Path

from capybase.config import Config
from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.orchestrator import Orchestrator

from tests.conftest import git
from tests.multistep_builder import CommitEdit, build_multistep_rebase


DEAD_BLOCK = "\n".join(f"def dead_{i}():\n    return {i}" for i in range(4))
# A realistic live region: enough unique lines that the dead block is a small
# fraction of the buffer (tokio-0037: 12 dead lines vs ~200 live lines).
LIVE = "\n".join(f"live_{i} = {i}" for i in range(90))
BASE = f"{LIVE}\n\n{DEAD_BLOCK}\n\ntail = 2\n"
CUR = f"{LIVE}\n\ntail = 2\n"                      # upstream deleted the block
REP = f"{LIVE}\n\n{DEAD_BLOCK}\n\ntail = 3\n"     # replayed kept it + edit


def _repo_at_conflict(repo: Path) -> None:
    git(repo, "init", "-q", "-b", "main")
    build_multistep_rebase(
        repo,
        base_files={"app.py": BASE},
        feat_commits=[CommitEdit("feat: keep block + edit tail",
                                 {"app.py": REP})],
        main_commits=[CommitEdit("main: delete dead block",
                                 {"app.py": CUR})],
        stop_early=True,
    )


def _unit(repo: Path) -> ConflictUnit:
    worktree = (repo / "app.py").read_text()
    return ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="app.py:1:0",
        unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=BASE),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=CUR),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=REP),
        original_worktree_text=worktree, marker_span=(2, 4),
    )


def _orch(repo: Path) -> Orchestrator:
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    return Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)


def test_swap_replaces_context_resurrection(tmp_path: Path):
    repo = tmp_path / "r"
    repo.mkdir()
    _repo_at_conflict(repo)
    orch = _orch(repo)
    unit = _unit(repo)
    # The buffer the per-unit flow produced: correct current_only units, but
    # git's auto-merge context carried the dead block back in.
    buffer = CUR.replace("tail = 2", f"\n{DEAD_BLOCK}\ntail = 2")
    out = orch._try_deletion_respect_swap("app.py", "python", [unit], buffer)
    assert out is not None, "context resurrection should swap to upstream"
    assert out[0][1].resolved_text == CUR
    assert out[0][1].provenance == "deterministic_source_current_only"
    events = [e.event_type for e in orch.journal.read_events()]
    assert "deletion_respect_swap" in events


def test_no_swap_when_buffer_is_clean(tmp_path: Path):
    repo = tmp_path / "r"
    repo.mkdir()
    _repo_at_conflict(repo)
    orch = _orch(repo)
    unit = _unit(repo)
    assert orch._try_deletion_respect_swap(
        "app.py", "python", [unit], CUR) is None
    # no probe event either — nothing resurrected
    events = [e.event_type for e in orch.journal.read_events()]
    assert "deletion_respect_swap_probe" not in events


def test_no_swap_for_woven_merge(tmp_path: Path):
    """A buffer weaving REAL replayed-side features (not just git context)
    must not be swapped: containment in the upstream side is too low, the
    end-of-rebase scan decides."""
    repo = tmp_path / "r"
    repo.mkdir()
    _repo_at_conflict(repo)
    orch = _orch(repo)
    unit = _unit(repo)
    woven = (
        "live = 1\n"
        + "replayed_feature = True\n" * 20  # substantial replayed-side content
        + f"\n{DEAD_BLOCK}\ntail = 3\n"
    )
    assert orch._try_deletion_respect_swap(
        "app.py", "python", [unit], woven) is None


def test_no_swap_when_detection_disabled(tmp_path: Path):
    repo = tmp_path / "r"
    repo.mkdir()
    _repo_at_conflict(repo)
    cfg = Config()
    cfg.model.model = "fake"
    cfg.validation.enable_resurrection_detection = False
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    unit = _unit(repo)
    buffer = CUR.replace("tail = 2", f"\n{DEAD_BLOCK}\ntail = 2")
    assert orch._try_deletion_respect_swap(
        "app.py", "python", [unit], buffer) is None
