"""Tests for whole-file modify/delete conflict handling.

The case: one side of a rebase DELETES a file/module, the other MODIFIES it. Git
reports this as a modify/delete conflict (modes ``AU``/``UA``) with NO
``<<<<<<<`` markers and NO stage blob for the deleting side. Capybase used to
escalate it as "unsupported conflict mode"; now it extracts a single
``whole_file`` unit (``marker_span=None``) and routes it through the existing
modify/delete machinery (structural → block-capture).

These tests cover the pure pieces (the ``direction()`` classification for a
whole-file delete, the orchestrator's ``_is_whole_file_delete`` /
``_resolved_buffer`` helpers, the verifier's None-span tolerance) plus the
extractor producing the right unit from a real modify/delete index.
"""

from __future__ import annotations

from pathlib import Path

from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.merge_intent import direction
from capybase.orchestrator import _is_whole_file_delete, _resolved_buffer
from capybase.structural_resolver import _accept_deletion
from capybase.verification import _has_whole_file_span

from tests.conftest import git


# ---------------------------------------------------------------------------
# merge_intent.direction: a whole-file delete vs. modify classifies as modify/delete
# ---------------------------------------------------------------------------


def test_direction_whole_file_delete_upstream():
    """Upstream deleted the whole file (empty current); replayed modified it."""
    base = "def alpha():\n    return 1\n\ndef beta():\n    return 2\n"
    current = ""  # upstream deleted
    replayed = "def alpha():\n    return 11\n\ndef beta():\n    return 2\n"
    d = direction(base, current, replayed)
    assert d.kind == "modify_delete"
    assert d.deleting_side == "current"
    assert d.current == "deleted"


def test_direction_whole_file_delete_replayed():
    """The mirror: replayed deleted, upstream modified (the UA case)."""
    base = "def alpha():\n    return 1\n\ndef beta():\n    return 2\n"
    current = "def alpha():\n    return 99\n\ndef beta():\n    return 2\n"
    replayed = ""  # replayed deleted
    d = direction(base, current, replayed)
    assert d.kind == "modify_delete"
    assert d.deleting_side == "replayed"
    assert d.replayed == "deleted"


def test_structural_delete_side_declines_when_keeper_modified():
    """A real modify/delete (keeper MODIFIED) is NOT auto-resolved — it routes
    to block-capture so the model decides. This is why the structural rule
    declines and block-capture exists."""
    base = "def alpha():\n    return 1\n\ndef beta():\n    return 2\n"
    current = ""  # deleted
    replayed = "def alpha():\n    return 11\n\ndef beta():\n    return 2\n"  # modified
    # decline → None (the keeper modified, so accepting the deletion could drop work)
    assert _accept_deletion(base, current, replayed) is None


def test_structural_delete_side_accepts_when_keeper_unchanged():
    """When the keeper kept base verbatim, the deletion is safe to accept."""
    base = "def alpha():\n    return 1\n\ndef beta():\n    return 2\n"
    current = ""  # deleted
    replayed = base  # unchanged
    res = _accept_deletion(base, current, replayed)
    assert res is not None
    assert res == current  # the deleter's (empty) text


# ---------------------------------------------------------------------------
# Orchestrator helpers: _is_whole_file_delete + _resolved_buffer
# ---------------------------------------------------------------------------


def _unit(*, kind="whole_file", span=None, resolved="", base_text="x"):
    return ConflictUnit(
        session_id="s", step_index=1, path="m.py", language="python",
        conflict_type="AU", unit_id="u", unit_kind=kind, marker_span=span,
        base=ConflictSide(label="BASE", text=base_text),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=""),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="y"),
        original_worktree_text=base_text,
    )


def _cand(resolved: str):
    from capybase.conflict_model import CandidateResolution

    return CandidateResolution(
        candidate_id="u:c", unit_id="u", model_name="fake",
        prompt_version="v", resolved_text=resolved,
    )


def test_is_whole_file_delete_true_for_empty_whole_file_resolution():
    u = _unit(span=None, resolved="")
    assert _is_whole_file_delete([(u, _cand(""))]) is True


def test_is_whole_file_delete_false_for_keep_block():
    """A non-empty whole-file resolution (keep_block) is NOT a delete."""
    u = _unit(span=None, resolved="def kept():\n    pass\n")
    assert _is_whole_file_delete([(u, _cand("def kept():\n    pass\n"))]) is False


def test_is_whole_file_delete_false_for_marker_unit():
    """A marker-block unit (even with empty resolution) is not a whole-file delete."""
    u = _unit(kind="text_marker_block", span=(0, 2), resolved="")
    assert _is_whole_file_delete([(u, _cand(""))]) is False


def test_resolved_buffer_returns_candidate_text_for_whole_file():
    """A whole_file unit's resolved text IS the file — no splicing."""
    u = _unit(span=None)
    text = "def kept():\n    return 1\n"
    buf = _resolved_buffer(u.original_worktree_text, [(u, _cand(text))])
    assert buf == text


def test_resolved_buffer_returns_empty_for_accept_deletion():
    """accept_deletion → empty resolved text → empty buffer (the file is removed)."""
    u = _unit(span=None)
    buf = _resolved_buffer(u.original_worktree_text, [(u, _cand(""))])
    assert buf == ""


def test_resolved_buffer_splices_marker_units_normally():
    """A marker unit still splices into the marker-laden original."""
    original = "a\n<<<<<<<\nold\n=======\nnew\n>>>>>>>\nb\n"
    u = ConflictUnit(
        session_id="s", step_index=1, path="m.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        marker_span=(1, 5),
        base=ConflictSide(label="BASE", text="old"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="old"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="new"),
        original_worktree_text=original,
    )
    buf = _resolved_buffer(original, [(u, _cand("merged"))])
    assert buf == "a\nmerged\nb\n"


# ---------------------------------------------------------------------------
# Verifier: None-span tolerance
# ---------------------------------------------------------------------------


def test_has_whole_file_span_detects_none():
    assert _has_whole_file_span([((0, 2), "x"), (None, "y")]) is True


def test_has_whole_file_span_false_for_all_marker_spans():
    assert _has_whole_file_span([((0, 2), "x"), ((3, 5), "y")]) is False


# ---------------------------------------------------------------------------
# Extractor: a real modify/delete index yields one whole_file unit
# ---------------------------------------------------------------------------


def _build_au_repo(repo: Path) -> str:
    """A repo stopped at an AU modify/delete: main deleted m.py, feat modified it."""
    base = "def alpha():\n    return 1\n\ndef beta():\n    return 2\n"
    (repo / "m.py").write_text(base)
    git(repo, "add", "m.py")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat")
    # main DELETES the module.
    git(repo, "rm", "m.py")
    git(repo, "commit", "-q", "-m", "main: delete module")
    # feat MODIFIES it (replayed side).
    git(repo, "checkout", "-q", "feat")
    (repo / "m.py").write_text(
        "def alpha():\n    return 11\n\ndef beta():\n    return 2\n\ndef gamma():\n    return 3\n"
    )
    git(repo, "add", "m.py")
    git(repo, "commit", "-q", "-m", "feat: modify module")
    # Rebase feat onto main → AU conflict.
    r = git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected a modify/delete conflict"
    return "m.py"


def test_extractor_builds_whole_file_unit_for_au(repo: Path):
    """An AU modify/delete yields one whole_file unit with the right shape."""
    from capybase.conflict_extractor import ConflictExtractor
    from capybase.git_backend import GitBackend

    path = _build_au_repo(repo)
    g = GitBackend(repo)
    ex = ConflictExtractor(g)
    unmerged = g.list_unmerged_paths()
    assert len(unmerged) == 1
    entry = unmerged[0]
    assert entry.mode == "AU"  # stages 1 + 3 (current/upstream absent = deleter)

    units = ex.extract_file_units(path, 1, "s", unmerged=entry)
    assert len(units) == 1
    u = units[0]
    assert u.unit_kind == "whole_file"
    assert u.marker_span is None
    assert u.conflict_type == "AU"
    # The deleting side (current/upstream) is empty; the keeper (replayed) holds
    # the modified content; base holds the pre-delete content.
    assert u.current.text == ""
    assert "gamma" in u.replayed.text  # feat's modification
    assert "def beta" in u.base.text
    # merge_direction is populated → block-capture's gate can fire.
    md = u.structural_metadata["merge_direction"]
    assert md["kind"] == "modify_delete"
    assert md["deleting_side"] == "current"
    assert u.replayed.text == u.original_worktree_text  # git left the keeper in tree


def test_extractor_builds_whole_file_unit_for_ua(repo: Path):
    """The mirror: UA (replayed deleted, upstream modified)."""
    from capybase.conflict_extractor import ConflictExtractor
    from capybase.git_backend import GitBackend

    base = "def alpha():\n    return 1\n\ndef beta():\n    return 2\n"
    (repo / "m.py").write_text(base)
    git(repo, "add", "m.py")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat")
    # main MODIFIES.
    (repo / "m.py").write_text("def alpha():\n    return 99\n\ndef beta():\n    return 2\n")
    git(repo, "add", "m.py")
    git(repo, "commit", "-q", "-m", "main: modify module")
    # feat DELETES (replayed side).
    git(repo, "checkout", "-q", "feat")
    git(repo, "rm", "m.py")
    git(repo, "commit", "-q", "-m", "feat: delete module")
    r = git(repo, "rebase", "main", check=False)
    assert r.returncode != 0

    g = GitBackend(repo)
    ex = ConflictExtractor(g)
    unmerged = g.list_unmerged_paths()
    entry = unmerged[0]
    assert entry.mode == "UA"  # stages 1 + 2 (replayed absent = deleter)
    units = ex.extract_file_units("m.py", 1, "s", unmerged=entry)
    u = units[0]
    assert u.unit_kind == "whole_file"
    assert u.conflict_type == "UA"
    assert u.replayed.text == ""  # replayed deleted
    assert "return 99" in u.current.text  # upstream modified (keeper)
    md = u.structural_metadata["merge_direction"]
    assert md["kind"] == "modify_delete"
    assert md["deleting_side"] == "replayed"


def test_policy_supports_au_ua_modes():
    """The default supported-conflict-types must include AU/UA so modify/delete
    paths are no longer skipped as 'unsupported conflict mode'."""
    from capybase.config import Config

    cfg = Config()
    assert "AU" in cfg.policy.supported_conflict_types
    assert "UA" in cfg.policy.supported_conflict_types
