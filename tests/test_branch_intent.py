"""Tests for branch final-intent summaries (#9 step 6).

A structural (non-LLM) summary of the source branch's net effect per file:
which symbols changed in which commits, and the final state (added/removed/
changed). Computed once per rebase from the source commits' patches.
"""

from __future__ import annotations

from capybase.branch_intent import BranchIntent, build_branch_intent
from capybase.history import RebasePlan, ReplayCommit


def _commit(oid, subject, files, index):
    return ReplayCommit(
        oid=oid, parent_oid="p", subject=subject, body_summary="",
        touched_files=files, diffstat={}, patch_id="", index=index,
    )


def _plan(commits):
    return RebasePlan(
        source_commits=commits, target_base_oid="base", target_tip_oid="tip",
        source_tip_oid="src", created_at="now",
    )


def _patch(path, added=(), removed=()):
    """Build a single-file patch with added/removed definition lines."""
    lines = [f"diff --git a/{path} b/{path}", "--- a/" + path, "+++ b/" + path,
             "@@ -1,1 +1,1 @@"]
    for d in removed:
        lines.append(f"-def {d}():")
        lines.append(f"-    pass")
    for d in added:
        lines.append(f"+def {d}():")
        lines.append(f"+    pass")
    return "\n".join(lines).encode()


# ---------------------------------------------------------------------------
# basic extraction
# ---------------------------------------------------------------------------


def test_no_plan_yields_empty_intent():
    assert build_branch_intent(None, {}).empty


def test_empty_source_commits_yields_empty_intent():
    plan = _plan([])
    assert build_branch_intent(plan, {}).empty


def test_single_commit_adding_a_symbol():
    """One commit adding parse_config → file intent with an 'added' symbol."""
    c1 = _commit("c1", "add config", ["cfg.py"], 1)
    plan = _plan([c1])
    patches = {"c1": _patch("cfg.py", added=["parse_config"])}
    intent = build_branch_intent(plan, patches)
    assert not intent.empty
    assert len(intent.files) == 1
    f = intent.files[0]
    assert f.path == "cfg.py"
    assert "parse_config" in f.added
    assert "parse_config" in f.symbols_changed


def test_symbol_touched_across_multiple_commits_lists_positions():
    """parse_config changed in commits 3, 7, 8 → positions [3,7,8]."""
    commits = [
        _commit("c1", "s1", ["cfg.py"], 1),
        _commit("c2", "s2", ["other.py"], 2),
        _commit("c3", "edit parse", ["cfg.py"], 3),
        _commit("c4", "s4", ["cfg.py"], 4),
    ]
    # commits 1, 3, 4 touch cfg.py and modify parse_config.
    plan = _plan(commits)
    patches = {
        "c1": _patch("cfg.py", added=["parse_config"]),
        "c3": _patch("cfg.py", added=["parse_config"]),  # re-edited
        "c4": _patch("cfg.py", added=["parse_config"]),  # re-edited
    }
    intent = build_branch_intent(plan, patches)
    f = intent.files[0]
    assert f.path == "cfg.py"
    assert f.symbols_changed["parse_config"] == {1, 3, 4}


def test_removed_symbol_marked_removed():
    """A symbol deleted across the branch → 'removed' tag."""
    c1 = _commit("c1", "rm helper", ["cfg.py"], 1)
    plan = _plan([c1])
    patches = {"c1": _patch("cfg.py", removed=["old_helper"])}
    intent = build_branch_intent(plan, patches)
    f = intent.files[0]
    assert "old_helper" in f.removed


def test_render_block_lists_files_and_symbols():
    c1 = _commit("c1", "add", ["cfg.py"], 1)
    plan = _plan([c1])
    patches = {"c1": _patch("cfg.py", added=["parse_config"])}
    intent = build_branch_intent(plan, patches)
    block = intent.render_block()
    assert "Branch final intent" in block
    assert "cfg.py" in block
    assert "parse_config" in block
    assert "added" in block


def test_render_block_empty_when_no_changes():
    c1 = _commit("c1", "noop", ["cfg.py"], 1)
    plan = _plan([c1])
    # A patch with no definition changes (e.g. only a comment edit).
    patches = {"c1": b"diff --git a/cfg.py b/cfg.py\n@@ -1,1 +1,1 @@\n+# comment\n"}
    intent = build_branch_intent(plan, patches)
    assert intent.render_block() == ""


# ---------------------------------------------------------------------------
# multi-file patches
# ---------------------------------------------------------------------------


def test_multi_file_patch_split_per_file():
    """A commit touching two files attributes changes to each."""
    c1 = _commit("c1", "two files", ["cfg.py", "util.py"], 1)
    plan = _plan([c1])
    # A combined patch with two diff --git sections.
    combined = (
        _patch("cfg.py", added=["parse_config"])
        + b"\n"
        + _patch("util.py", added=["normalize"])
    )
    intent = build_branch_intent(plan, {"c1": combined})
    paths = {f.path for f in intent.files}
    assert paths == {"cfg.py", "util.py"}


def test_files_sorted_most_changed_first():
    """The file with the most symbol changes is listed first."""
    c1 = _commit("c1", "many", ["cfg.py", "util.py"], 1)
    plan = _plan([c1])
    combined = (
        _patch("cfg.py", added=["a", "b", "c"])
        + b"\n"
        + _patch("util.py", added=["only_one"])
    )
    intent = build_branch_intent(plan, {"c1": combined})
    assert intent.files[0].path == "cfg.py"  # 3 symbols > 1 symbol
