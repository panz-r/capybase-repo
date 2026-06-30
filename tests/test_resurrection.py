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
