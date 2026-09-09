"""List-union (S27-48): name-list conflicts resolve to the current
side's lines + the replayed side's additions (target dedups respected).
Offline policy across all libuv list blocks: 104/141 exact, the rest
within ~1 line; zero blocks drop a replayed addition.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from capybase.list_union import (  # noqa: E402
    propose_list_union, is_list_file_path,
)


BASE = "Ann A <a@example.com>\nBob B <b@example.com>\nCara C <c@example.com>\n"


def test_path_gate():
    assert is_list_file_path("AUTHORS")
    assert is_list_file_path("doc/AUTHORS.txt")
    assert is_list_file_path(".mailmap")
    assert is_list_file_path("Mailmap.md")
    assert is_list_file_path("CONTRIBUTORS")
    assert not is_list_file_path("README.md")        # prose, not a list
    assert not is_list_file_path("src/main.rs")
    assert not is_list_file_path("AUTHORS.old.txt")  # unknown extension


def test_fires_and_takes_replayed_additions():
    cur = BASE + "Dee D <d@example.com>\n"
    rep = BASE + "Eli E <e@example.com>\n"
    r = propose_list_union("AUTHORS", BASE, cur, rep)
    assert r.resolved, r.reason
    lines = r.text.splitlines()
    assert "Dee D <d@example.com>" in lines
    assert "Eli E <e@example.com>" in lines
    # sorted sides → sorted output
    keys = [l.casefold() for l in lines]
    assert keys == sorted(keys)


def test_current_removals_are_respected_not_unioned_back():
    # The target deduped Cara out; the replayed side never saw that edit.
    cur = "Ann A <a@example.com>\nBob B <b@example.com>\nDee D <d@example.com>\n"
    rep = BASE + "Eli E <e@example.com>\n"
    r = propose_list_union("AUTHORS", BASE, cur, rep)
    assert r.resolved
    assert "Cara" not in r.text  # the target's removal survives
    assert "Eli E <e@example.com>" in r.text


def test_declines_when_replayed_adds_nothing():
    cur = BASE + "Dee D <d@example.com>\n"
    r = propose_list_union("AUTHORS", BASE, cur, BASE)
    assert not r.resolved
    assert "adds nothing" in r.reason


def test_declines_non_list_path():
    r = propose_list_union("README.md", BASE, BASE + "x <x@x>\n", BASE + "y <y@y>\n")
    assert not r.resolved
    assert r.reason == "path is not a list file"


def test_unsorted_sides_keep_current_order_and_append():
    cur = "Zed Z <z@example.com>\nAnn A <a@example.com>\n"
    rep = "Bob B <b@example.com>\nNew N <n@example.com>\n"
    base = "Bob B <b@example.com>\n"
    r = propose_list_union(".mailmap", base, cur, rep)
    assert r.resolved
    lines = r.text.splitlines()
    # current order preserved; the replayed addition appended (not sorted)
    assert lines[0].startswith("Zed")
    assert lines[-1] == "New N <n@example.com>"


def test_duplicate_additions_collapse():
    # Both sides added the same name (differing whitespace) — one entry.
    cur = BASE + "Dee  D <d@example.com>\n"
    rep = BASE + "Dee D <d@example.com>\n"
    r = propose_list_union("AUTHORS", BASE, cur, rep)
    # replayed's add duplicates current's → nothing new to union → decline
    assert not r.resolved
