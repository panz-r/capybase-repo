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
