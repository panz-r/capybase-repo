"""Loader for processed real-world merge-conflict test cases (Method D).

Reads JSON case files produced by ``scripts/fetch_mergeconflict_datasets.py``
from ``extracted-testdata/realworld/``. Each file is one real-world conflict
carrying the A/O/B/M → capybase mapping (base/current/replayed + the human merge
as ``expected_resolved`` + authentic ``marker_original`` from ``git merge-file``).

This is the committed bridge between the gitignored generated data and the test
suite: it performs NO download, only reads whatever the script has produced. When
the data dir is empty or absent (a fresh clone, or no Rust-bearing dataset found
yet), :func:`load_realworld_cases` returns ``[]`` and the parametrized tests in
``tests/test_realworld_conflicts.py`` skip cleanly.

The :class:`RealWorldCase` shape mirrors the synthetic catalog's ``RustConflict``
so the same ``verify_file`` harness drives both.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# The gitignored generated-data root (DATA_ROOT-overridable in the fetch script;
# here we resolve relative to the repo root regardless).
REPO_ROOT = Path(__file__).resolve().parent.parent
TESTDATA_DIR = Path(__import__("os").environ.get(
    "DATA_ROOT", str(REPO_ROOT),
)) / "extracted-testdata" / "realworld"


@dataclass(frozen=True)
class RealWorldCase:
    """One real-world conflict case loaded from a JSON file.

    Fields mirror the synthetic catalog so the harness is shared. ``marker_*``
    fields differ from the catalog: real-world cases carry the WHOLE marker-marked
    file (``marker_original``) rather than three bare sides, plus the human merge
    as ``expected_resolved`` (the oracle). ``language`` is the classifier's verdict
    (``"rust"`` for cases the fetch script kept).
    """

    id: str
    path: str
    language: str
    base: str
    current: str
    replayed: str
    expected_resolved: str
    marker_original: str
    dataset: str
    license: str
    source_url: str


def testdata_dir() -> Path:
    """The directory the fetch script writes JSON cases to."""
    return TESTDATA_DIR


def load_realworld_cases() -> list[RealWorldCase]:
    """Load every JSON case in the testdata dir, or ``[]`` if none/absent.

    Sorted by id for stable parametrization. Returns only well-formed cases
    (skips a malformed JSON file with a warning rather than failing collection —
    a single corrupt extracted file must not break the whole suite).
    """
    if not TESTDATA_DIR.is_dir():
        return []
    cases: list[RealWorldCase] = []
    for f in sorted(TESTDATA_DIR.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        # Require the fields the harness needs; skip incomplete cases silently.
        required = ("base", "current", "replayed", "expected_resolved", "marker_original")
        if not all(k in d for k in required):
            continue
        cases.append(
            RealWorldCase(
                id=d.get("id", f.stem),
                path=d.get("path", f"{f.stem}.rs"),
                language=d.get("language", "rust"),
                base=d["base"],
                current=d["current"],
                replayed=d["replayed"],
                expected_resolved=d["expected_resolved"],
                marker_original=d["marker_original"],
                dataset=d.get("dataset", ""),
                license=d.get("license", ""),
                source_url=d.get("source_url", ""),
            )
        )
    return cases


def has_realworld_data() -> bool:
    """True iff any real-world case has been generated (data downloaded)."""
    return bool(load_realworld_cases())
