"""Docs-union (S27-08): changelog-shaped conflicts resolve to the ordered
union. Oracle policy: 48/51 corpus cases are the union; offline the
mechanism fires 36/36 at sim >= 0.998 (the 15 declines are all
removal-carrying release cuts — correct declines).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from capybase.docs_union import (  # noqa: E402
    propose_docs_union, is_changelog_path,
)


BASE = """What's New
=========

- Fixed the parser crash. (issue 1)
- Sped up the reader. (issue 2)
"""


def _cur():
    return BASE + "- Added the writer option. (issue 3)\n"


def _rep():
    return BASE + "- Added the reader option. (issue 4)\n"


def _oracle():
    return BASE + "- Added the writer option. (issue 3)\n- Added the reader option. (issue 4)\n"


def test_fires_on_changelog_path_and_unions():
    r = propose_docs_union("doc/whats_new/v0.20.rst", BASE, _cur(), _rep())
    assert r.resolved, r.reason
    assert "issue 3" in r.text and "issue 4" in r.text
    # both base entries survive
    assert "issue 1" in r.text and "issue 2" in r.text
    # the union IS the corpus oracle shape (current canonical + appended)
    assert "issue 3)" in r.text.split("issue 2")[1].split("issue 4")[0]


def test_path_gate():
    assert is_changelog_path("docs/whats_new/v1.rst")
    assert is_changelog_path("CHANGELOG.md")
    assert is_changelog_path("src/news.txt")
    assert not is_changelog_path("src/main.rs")
    assert not is_changelog_path("docs/tutorial.rst")  # docs ext, wrong name
    assert not is_changelog_path("src/changelog.py")   # right name, wrong ext


def test_content_gate_random_rst_declines():
    prose = "A Tutorial\n==========\n\nSome prose without entries.\n"
    r = propose_docs_union("doc/whats_new.rst", prose,
                           prose + "More prose A\n", prose + "More prose B\n")
    # headings but no entry-list -> decline (not an append-only log)
    assert not r.resolved


def test_removal_carrying_sides_decline():
    cur = BASE.replace("- Fixed the parser crash. (issue 1)\n", "") \
        + "- Added the writer option. (issue 3)\n"
    r = propose_docs_union("CHANGELOG.md", BASE, cur, _rep())
    assert not r.resolved
    assert "removes existing entries" in r.reason


def test_one_sided_additions_decline():
    r = propose_docs_union("CHANGELOG.md", BASE, BASE, _rep())
    assert not r.resolved
    assert "one-sided" in r.reason


def test_duplicate_entries_skipped():
    both = BASE + "- Added the same entry. (issue 9)\n"
    r = propose_docs_union("CHANGELOG.md", BASE, both, both)
    assert r.resolved
    assert r.text.count("issue 9") == 1
