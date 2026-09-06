"""Corpus admission filters (s27 integration) — the s27-prelim discoveries
made operational: binary/non-source paths (prusaslicer's .mo), empty
marker_original, exact-duplicate tuples, and python-2-era oracles whose
gates can never pass (scikit-0090's oracle fails py_compile).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.fetch_mergeconflict_datasets import admit_case  # noqa: E402


def _run(kwargs, seen):
    return admit_case(seen_hashes=seen, **kwargs)


def _mk(**over):
    defaults = dict(
        conflict_path="src/main.py", base="a = 1\n", current="a = 2\n",
        replayed="a = 3\n", merged="a = 4\n",
        marker_original="<<<<<<< A\na = 2\n=======\na = 3\n>>>>>>> B\n",
        language="python",
    )
    defaults.update(over)
    return defaults


def test_admits_plain_source_conflict():
    ok, why = _run(_mk(), set())
    assert ok and why is None


def test_rejects_non_source_paths():
    # The prusaslicer find: gettext .mo catalogs content-classified as python.
    ok, why = _run(_mk(conflict_path="resources/localization/be/PrusaSlicer.mo"), set())
    assert not ok and why == "non_source"
    ok, why = _run(_mk(conflict_path="docs/schema.json", language="unknown"), set())
    assert not ok and why == "non_source"


def test_rejects_binary_content():
    ok, why = _run(_mk(merged="x\x00y"), set())
    assert not ok and why == "binary"


def test_rejects_clean_merge_and_empty_markers():
    ok, why = _run(_mk(marker_original=None), set())
    assert not ok and why == "clean_merge"
    # prusaslicer-0016: git merge-file on binary content returned "" (not
    # None) and slipped the old None guard.
    ok, why = _run(_mk(marker_original=""), set())
    assert not ok and why == "empty_markers"


def test_rejects_exact_duplicates():
    seen: set[str] = set()
    ok1, _ = _run(_mk(), seen)
    ok2, why2 = _run(_mk(), seen)
    assert ok1 and not ok2 and why2 == "duplicate"


def test_rejects_py2_oracle_for_py_paths():
    ok, why = _run(_mk(merged="print 'hello'\n"), set())
    assert not ok and why == "py2_oracle"


def test_keeps_pyx_oracles_unparsed():
    # Cython .pyx content (cdef etc.) cannot ast-parse as python; the
    # language-era filter applies to .py only.
    ok, why = _run(_mk(conflict_path="cython/foo.pyx",
                             merged="cdef int x = 1\n"), set())
    assert ok and why is None


def test_py3_oracle_admitted():
    ok, why = _run(_mk(merged="print('hello')\n"), set())
    assert ok and why is None


def test_rejects_duplicate_definitions_in_texts():
    # tikv-0024: the human oracle kept both sides' identical decode_data
    # (a true positive — rustc E0201; a broken oracle).
    dup_src = ("pub fn decode_data(r: &mut T) -> Result<u64> {\n"
               "    1\n}\n\npub fn decode_data(r: &mut T) -> Result<u64> {\n"
               "    1\n}\n")
    ok, why = _run(_mk(conflict_path="src/util/codec/rpc.rs", language="rust",
                       merged=dup_src), set())
    assert not ok and why == "dup_definitions"
    # and pristine sides fire too
    ok, why = _run(_mk(conflict_path="src/util/codec/rpc.rs", language="rust",
                       replayed=dup_src), set())
    assert not ok and why == "dup_definitions"
