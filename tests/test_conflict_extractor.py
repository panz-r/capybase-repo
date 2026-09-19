from capybase.conflict_extractor import ConflictExtractor, detect_language
from capybase.git_backend import GitBackend


def test_extract_units(conflicted_repo):
    git = GitBackend(conflicted_repo["repo"])
    ex = ConflictExtractor(git)
    units = ex.extract_file_units("app.py", step_index=1, session_id="s1")
    assert len(units) == 1
    u = units[0]
    assert u.unit_kind == "text_marker_block"
    assert u.path == "app.py"
    assert u.language == "python"
    assert u.conflict_type == "UU"
    # base side is the full base file (stage 1 blob).
    assert u.base.text == conflicted_repo["base"]
    # current/replayed sides are the marker-block inner texts.
    assert u.current.text == "    return 'hi'"
    assert u.replayed.text == "    return 'howdy'"
    assert u.marker_span is not None
    assert "<<<<<<<" in u.original_worktree_text


def test_extract_all_classifies(conflicted_repo):
    git = GitBackend(conflicted_repo["repo"])
    ex = ConflictExtractor(git)
    units_by_path, skipped = ex.extract_all(
        1, "s1", supported_types={"UU"}
    )
    assert skipped == []
    assert "app.py" in units_by_path
    assert len(units_by_path["app.py"]) == 1


def test_detect_language():
    assert detect_language("a/b.py") == "python"
    assert detect_language("x.ts") == "typescript"
    assert detect_language("Makefile") is None


def test_extract_multi_unit_populates_sibling_metadata(multi_unit_conflicted_repo):
    """A two-hunk file yields two units, each annotated with sibling metadata
    so the context builder can confine its window across block boundaries."""
    git = GitBackend(multi_unit_conflicted_repo["repo"])
    ex = ConflictExtractor(git)
    units = ex.extract_file_units(
        multi_unit_conflicted_repo["path"], step_index=1, session_id="s1"
    )
    assert len(units) == 2
    for u in units:
        assert u.structural_metadata.get("sibling_count") == 2
        siblings = u.structural_metadata.get("sibling_units")
        assert isinstance(siblings, list) and len(siblings) == 2
        # Each sibling entry has a unit_id and a 2-element marker_span.
        for sib in siblings:
            assert "unit_id" in sib
            assert isinstance(sib["marker_span"], list) and len(sib["marker_span"]) == 2


def test_single_unit_has_no_sibling_metadata(conflicted_repo):
    """A single-hunk file must NOT set sibling metadata (no siblings)."""
    git = GitBackend(conflicted_repo["repo"])
    ex = ConflictExtractor(git)
    units = ex.extract_file_units("app.py", step_index=1, session_id="s1")
    assert len(units) == 1
    assert "sibling_units" not in units[0].structural_metadata


def test_detect_language_strips_linecol_suffix():
    """Git conflict paths may carry a ':line:col' suffix (e.g. 'src/foo.rs:1:0').
    detect_language must strip it so the extension lookup succeeds — without
    this fix, every conflict from git has language=None, silently skipping the
    comment pass + shadow jury."""
    from capybase.conflict_extractor import detect_language
    assert detect_language("src/foo.rs:1:0") == "rust"
    assert detect_language("src/foo.rs") == "rust"
    assert detect_language("lib/bar.py:5:10") == "python"
    assert detect_language("lib/bar.py") == "python"
    # No extension → None
    assert detect_language("Makefile") is None
    assert detect_language("Makefile:1:0") is None


def test_add_add_conflict_extracts_with_empty_base(repo):
    """AA (add/add): no stage-1 base exists. The stage-1 read used to RAISE,
    the gather loop skipped the path as an extraction error, and a step whose
    conflicts were all add/add escalated as 'all conflicted paths are
    unsupported' (libuv-0019's test-emfile.c — a crash misread as a policy
    boundary). The extractor now degrades to an empty base — the same 3-way
    git's own add/add content merge uses."""
    from corpus._gitshim import git

    # empty root, then each side independently ADDS the file (true add/add)
    git(repo, "commit", "-q", "--allow-empty", "-m", "root")
    git(repo, "checkout", "-q", "-b", "side")
    (repo / "emfile.c").write_text(
        "int main(void) {\n  int ok = 2;\n  return ok ? 0 : 1;\n}\n")
    git(repo, "add", "emfile.c")
    git(repo, "commit", "-q", "-m", "theirs adds")
    git(repo, "checkout", "-q", "main")
    (repo / "emfile.c").write_text(
        "int main(void) {\n  int ok = 1;\n  return ok ? 0 : 1;\n}\n")
    git(repo, "add", "emfile.c")
    git(repo, "commit", "-q", "-m", "ours adds")
    git(repo, "merge", "side", check=False)

    git_backend = GitBackend(repo)
    ex = ConflictExtractor(git_backend)
    # The gather's unmerged entry: mode AA, stages 2+3 only.
    class _E:
        path = "emfile.c"
        mode = "AA"
        stages = {2: "x2", 3: "x3"}

    units = ex.extract_file_units(
        "emfile.c", step_index=1, session_id="s1", unmerged=_E())
    assert units, "AA conflict must extract, not raise/skip"
    for u in units:
        assert u.conflict_type == "AA"
        assert u.base.text == ""  # no stage 1 — empty base, like git's merge
        assert u.current.text.strip() and u.replayed.text.strip()


def test_add_add_conflict_supported_by_default_policy(repo):
    """s27-68: the policy gate must pass genuine add/add through. s27-67b
    relabeled {2,3}-without-base from "UU" to "AA" in _synthesize_mode, but
    the policy's supported set never learned "AA" — every add/add was then
    skipped as 'unsupported conflict mode AA', so a step whose only
    conflicts are add/add escalated at gather (clap-0011: 36-second
    ESCALATE at step 1 on .gitignore+README.md), and the extractor's
    empty-base branch (s27-48) became production-dead code."""
    from corpus._gitshim import git
    from capybase.config import Config
    from capybase.policy import Policy

    # Same true add/add shape as the test above: both sides add the file.
    git(repo, "commit", "-q", "--allow-empty", "-m", "root")
    git(repo, "checkout", "-q", "-b", "side")
    (repo / "added.txt").write_text("side adds a line\n")
    git(repo, "add", "added.txt")
    git(repo, "commit", "-q", "-m", "theirs adds")
    git(repo, "checkout", "-q", "main")
    (repo / "added.txt").write_text("main adds a line\n")
    git(repo, "add", "added.txt")
    git(repo, "commit", "-q", "-m", "ours adds")
    git(repo, "merge", "side", check=False)

    backend = GitBackend(repo)
    unmerged = backend.list_unmerged_paths()
    assert any(e.mode == "AA" for e in unmerged), "fixture must produce AA"

    cfg = Config()
    policy = Policy(
        backend,
        supported_file_kinds=set(cfg.policy.supported_file_kinds),
    )
    decision = policy.classify(unmerged)
    assert decision.skipped == [], (
        f"AA skipped as unsupported: {[s.reason for s in decision.skipped]}")
    assert [e.path for e in decision.supported] == ["added.txt"]


def test_missing_stage3_read_degrades_to_empty(repo):
    """The marker path's tolerant stage reads (s27-67b: 'missing stage =
    empty') must actually degrade. The replayed arm assigned the bare name
    ``b`` — a NameError the moment the except fired, contradicting the
    tolerance the comment promises."""
    git_backend = GitBackend(repo)
    ex = ConflictExtractor(git_backend)

    class _E:
        path = "whatever.txt"
        mode = "UU"
        stages = {1: "x1", 2: "x2"}  # stage 3 missing — must not raise

    (repo / "whatever.txt").write_text("")
    units = ex.extract_file_units(
        "whatever.txt", step_index=1, session_id="s1", unmerged=_E())
    # No markers in the empty worktree file → no units, but NO raise.
    assert units == []
