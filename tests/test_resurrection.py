"""Tests for silent-resurrection detection (:mod:`capybase.resurrection`).

The dangerous case: upstream deliberately deletes dead code (a cleanup commit),
and a replayed branch that predates the cleanup keeps it. Git's 3-way merge can
resolve CLEANLY (no conflict) while resurrecting the dead code — git sees no
conflict, so capybase historically saw none either, and the cleanup was silently
undone. These tests build that exact scenario in a real repo and prove the scan
catches it (and reports nothing when the deletion correctly held).
"""

from __future__ import annotations

from pathlib import Path

from capybase.git_backend import GitBackend
from capybase.resurrection import scan_resurrections, scan_step

from tests.conftest import git


# ---------------------------------------------------------------------------
# A builder for the silent-resurrection scenario.
# ---------------------------------------------------------------------------


def _build_resurrection_repo(repo: Path) -> dict:
    """A repo with three trees expressing a silent resurrection.

    The scan takes three revisions — base (merge-base), onto (the upstream side
    that deleted content), and result (the merge result). This builder constructs
    them directly so the test exercises the *scan logic* robustly, independent of
    whether a particular git rebase happens to auto-resolve cleanly (git's diff3
    heuristics are inconsistent about resurrecting vs. flagging, which is exactly
    why a dedicated scan is needed):

      base   : app.py with a dead() function (the content onto will delete)
      onto   : deletes dead() (the cleanup commit) — the deletion intent
      result : a commit (off base) that keeps dead() — stands in for whatever
               produced the resurrection (a clean merge, a checkout-recovery, or
               capybase's own resolution that re-added it)

    Returns oids + the repo for the scan assertions.
    """
    base = (
        "def useful():\n    return 1\n\n"
        "def dead():\n    # old impl\n    do_thing()\n    cleanup()\n\n"
    )
    (repo / "app.py").write_text(base)
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "base: add useful + dead")
    base_oid = git(repo, "rev-parse", "HEAD").stdout.strip()

    # onto (main): the cleanup — deletes dead().
    git(repo, "branch", "feat")  # keep base reachable via feat for the merge-base
    (repo / "app.py").write_text("def useful():\n    return 1\n\n")
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "main: remove dead() cleanup")
    onto_oid = git(repo, "rev-parse", "HEAD").stdout.strip()

    # result: off base, keeps dead() + a replay edit — the resurrected tree.
    git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text(base + "# added by replay\n")
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "result: keeps dead() + replay edit")
    result_oid = git(repo, "rev-parse", "HEAD").stdout.strip()

    return {
        "repo": repo,
        "base_oid": base_oid,
        "onto_oid": onto_oid,
        "result_oid": result_oid,
    }


# ---------------------------------------------------------------------------
# scan_resurrections
# ---------------------------------------------------------------------------


def test_scan_detects_silent_resurrection():
    """The headline case: the merge result resurrects deliberately-deleted dead()."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        rp = Path(d)
        git(rp, "init", "-q", "-b", "main")
        ctx = _build_resurrection_repo(rp)
        g = GitBackend(rp)
        findings = scan_resurrections(
            g,
            base_oid=ctx["base_oid"],
            onto_oid=ctx["onto_oid"],
            result_oid=ctx["result_oid"],
        )
        assert len(findings) == 1
        f = findings[0]
        assert f.path == "app.py"
        # The dead() block came back whole.
        assert any("dead()" in b.text and "do_thing()" in b.text for b in f.blocks)
        assert f.resurrected_line_count >= 3
        # The deleting commit's subject is attributed (the cleanup).
        assert "remove dead()" in f.deleting_commit


def test_scan_reports_nothing_when_deletion_held():
    """When the deletion correctly held in the result, nothing is flagged."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        rp = Path(d)
        git(rp, "init", "-q", "-b", "main")
        ctx = _build_resurrection_repo(rp)
        g = GitBackend(rp)
        # Use main's tree (the deletion held) as the result — no resurrection.
        findings = scan_resurrections(
            g,
            base_oid=ctx["base_oid"],
            onto_oid=ctx["onto_oid"],
            result_oid=ctx["onto_oid"],
        )
        assert findings == []


def test_scan_reports_nothing_when_onto_deleted_nothing():
    """If onto didn't delete anything, there's nothing to resurrect."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        rp = Path(d)
        git(rp, "init", "-q", "-b", "main")
        (rp / "a.txt").write_text("a\n")
        git(rp, "add", "-A")
        git(rp, "commit", "-q", "-m", "base")
        base_oid = git(rp, "rev-parse", "HEAD").stdout.strip()

        git(rp, "branch", "feat")
        git(rp, "checkout", "-q", "feat")
        (rp / "b.txt").write_text("b\n")
        git(rp, "add", "-A")
        git(rp, "commit", "-q", "-m", "feat: add b")

        git(rp, "checkout", "-q", "main")
        (rp / "c.txt").write_text("c\n")
        git(rp, "add", "-A")
        git(rp, "commit", "-q", "-m", "main: add c")
        main_oid = git(rp, "rev-parse", "HEAD").stdout.strip()

        g = GitBackend(rp)
        assert scan_resurrections(
            g, base_oid=base_oid, onto_oid=main_oid, result_oid=main_oid
        ) == []


def test_scan_step_scopes_to_one_commit():
    """scan_step checks a single step's tree as the result."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        rp = Path(d)
        git(rp, "init", "-q", "-b", "main")
        ctx = _build_resurrection_repo(rp)
        g = GitBackend(rp)
        # The replayed result IS the resurrection; scan_step finds it.
        findings = scan_step(
            g,
            step_oid=ctx["result_oid"],
            base_oid=ctx["base_oid"],
            onto_oid=ctx["onto_oid"],
        )
        assert len(findings) == 1
        assert findings[0].path == "app.py"


def test_scan_never_raises_on_missing_revs(repo: Path):
    """Advisory detection must not raise on bogus revisions."""
    g = GitBackend(repo)
    assert scan_resurrections(
        g, base_oid="nope", onto_oid="also-nope", result_oid="bad"
    ) == []


# ---------------------------------------------------------------------------
# classify_deletion_stability (pure function)
# ---------------------------------------------------------------------------

from capybase.merge_intent import classify_deletion_stability


def test_stability_stable_deletion():
    """Block present in early blobs, absent in all later blobs → stable."""
    block = ["int dead_func(void) {", "    return 42;", "}"]
    seq = [
        "int dead_func(void) {\n    return 42;\n}\nint live(void) { return 0; }",
        "int live(void) { return 0; }",  # deleted here
        "int live(void) { return 0; }",  # still gone
        "int live(void) { return 0; }",  # tip: still gone
    ]
    assert classify_deletion_stability(block, seq) == "stable"


def test_stability_transient_deletion():
    """Block deleted then re-added on the same branch → transient."""
    block = ["int dead_func(void) {", "    return 42;", "}"]
    seq = [
        "int dead_func(void) {\n    return 42;\n}\nint live(void) { return 0; }",
        "int live(void) { return 0; }",  # deleted
        "int dead_func(void) {\n    return 42;\n}\nint live(void) { return 0; }",  # re-added!
        "int dead_func(void) {\n    return 42;\n}\nint live(void) { return 0; }",  # tip: present
    ]
    assert classify_deletion_stability(block, seq) == "transient"


def test_stability_absent():
    """Block never present in the sequence → absent."""
    block = ["int dead_func(void) {", "    return 42;", "}"]
    seq = ["int live(void) { return 0; }"]
    assert classify_deletion_stability(block, seq) == "absent"


def test_stability_empty_sequence():
    assert classify_deletion_stability(["line"], []) == "absent"


# ---------------------------------------------------------------------------
# History-walk stability filtering in scan_resurrections
# ---------------------------------------------------------------------------


def _build_transient_repo(repo: Path) -> dict:
    """A repo where the deletion was transient (deleted, re-added, then deleted again).

    This creates a scenario where the 3-way check WOULD flag a resurrection
    (base has dead(), onto tip doesn't, result has it), but the history walk
    reveals the deletion was NOT stable: dead() was briefly re-added between the
    two deletions, proving the removal was not a deliberate permanent cleanup.

    base  : app.py with dead() present.
    onto  : DELETE dead() → RE-ADD dead() → DELETE dead() again. At the onto
            tip, dead() is absent. The 3-way check sees a deletion. But the
            history walk shows dead() was re-added mid-history → transient.
    result: a commit off base that keeps dead() — the merge result.
    """
    base = (
        "def useful():\n    return 1\n\n"
        "def dead():\n    do_thing()\n    cleanup()\n\n"
    )
    (repo / "app.py").write_text(base)
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "base: add useful + dead")
    base_oid = git(repo, "rev-parse", "HEAD").stdout.strip()

    git(repo, "branch", "feat")
    # Delete dead().
    (repo / "app.py").write_text("def useful():\n    return 1\n\n")
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "onto: remove dead() (first removal)")
    # Re-add dead() (the transient re-introduction).
    (repo / "app.py").write_text(base)
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "onto: re-add dead() (transient)")
    # Delete dead() again (so the tip doesn't have it — 3-way flags it).
    (repo / "app.py").write_text("def useful():\n    return 1\n\n")
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "onto: remove dead() again")
    onto_oid = git(repo, "rev-parse", "HEAD").stdout.strip()

    # result: off base, keeps dead().
    git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text(base + "# replay edit\n")
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "result: keeps dead()")
    result_oid = git(repo, "rev-parse", "HEAD").stdout.strip()

    return {
        "repo": repo, "base_oid": base_oid,
        "onto_oid": onto_oid, "result_oid": result_oid,
    }


def test_scan_filters_transient_deletion():
    """A transient deletion (deleted, re-added, then deleted again on the same
    branch) should NOT be flagged as a resurrection — the stability walk
    recognizes the content was re-introduced, proving the removal was not a
    deliberate permanent cleanup."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        rp = Path(d)
        git(rp, "init", "-q", "-b", "main")
        ctx = _build_transient_repo(rp)
        g = GitBackend(rp)
        # With history_depth > 0, the transient deletion is filtered out.
        findings = scan_resurrections(
            g,
            base_oid=ctx["base_oid"],
            onto_oid=ctx["onto_oid"],
            result_oid=ctx["result_oid"],
            history_depth=50,
        )
        assert findings == [], (
            f"transient deletion should be filtered; got {len(findings)} findings"
        )


def test_scan_without_history_walk_still_flags_transient():
    """When history_depth=0 (disabled), the old 3-way behavior is preserved —
    the transient deletion IS flagged (no stability filter applied)."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        rp = Path(d)
        git(rp, "init", "-q", "-b", "main")
        ctx = _build_transient_repo(rp)
        g = GitBackend(rp)
        findings = scan_resurrections(
            g,
            base_oid=ctx["base_oid"],
            onto_oid=ctx["onto_oid"],
            result_oid=ctx["result_oid"],
            history_depth=0,
        )
        # Without the stability filter, the 3-way check flags it.
        assert len(findings) >= 1, "3-way check should flag without stability filter"


def test_scan_stable_deletion_still_flagged_with_history():
    """A stable deletion (deleted and never re-added) is still flagged even with
    the history walk enabled."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        rp = Path(d)
        git(rp, "init", "-q", "-b", "main")
        ctx = _build_resurrection_repo(rp)
        g = GitBackend(rp)
        findings = scan_resurrections(
            g,
            base_oid=ctx["base_oid"],
            onto_oid=ctx["onto_oid"],
            result_oid=ctx["result_oid"],
            history_depth=50,
        )
        assert len(findings) == 1
        f = findings[0]
        # The block should carry stability info.
        assert any(b.extra.get("stability") == "stable" for b in f.blocks)

