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
