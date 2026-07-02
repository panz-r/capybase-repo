"""Tests for the explicit ResolutionProvenance enum (#9 step 8).

Three concerns:
- ``capybase.provenance``: the label/validity helpers + the value set is stable
  and complete (metrics/dry-run rely on it being a closed enum).
- Every candidate construction site stamps the right value (no site is missed).
- The value survives serialization to ``Experience`` (so the corpus can be sliced
  by mechanism for #9 metrics) and round-trips through old JSONL lines that
  predate the field (backward compatibility).

The accept-report ``_via_label`` re-routing is covered in test_accept_report.py;
here we focus on the enum, construction, and persistence.
"""

from __future__ import annotations

import json

from capybase.conflict_model import (
    CandidateResolution,
    ConflictSide,
    ConflictUnit,
)
from capybase.memory.store import Experience, ExperienceStore
from capybase.provenance import (
    LEGACY_PROVENANCE,
    PROVENANCE_LABELS,
    PROVENANCE_VALUES,
    is_valid,
    provenance_label,
)


# ---------------------------------------------------------------------------
# enum surface (the closed set + labels)
# ---------------------------------------------------------------------------


def test_provenance_values_match_spec():
    """The seven spec values are present, in a stable order."""
    assert PROVENANCE_VALUES == (
        "deterministic_structural",
        "exact_history_reuse",
        "combination_search",
        "block_capture",
        "history_augmented_llm",
        "plain_llm",
        "manual",
    )


def test_every_value_has_a_human_label():
    """No value can be added without a label — metrics/reports depend on it."""
    for v in PROVENANCE_VALUES:
        assert v in PROVENANCE_LABELS, f"missing label for {v!r}"
        assert PROVENANCE_LABELS[v], f"empty label for {v!r}"


def test_is_valid_accepts_known_and_legacy_rejects_unknown():
    assert is_valid("plain_llm")
    assert is_valid(LEGACY_PROVENANCE)  # old data
    assert not is_valid("totally_made_up")
    assert not is_valid("Structural")  # case-sensitive


def test_provenance_label_falls_back_to_input_for_unknown():
    """An unknown value renders itself rather than crashing."""
    assert provenance_label("plain_llm") == "LLM"
    assert provenance_label("history_augmented_llm") == "history-augmented LLM"
    assert provenance_label(LEGACY_PROVENANCE) == "(legacy)"
    # Unknown future value: render the raw string, don't crash.
    assert provenance_label("future_mechanism") == "future_mechanism"


# ---------------------------------------------------------------------------
# candidate construction sites stamp the right value
# ---------------------------------------------------------------------------


def _unit() -> ConflictUnit:
    return ConflictUnit(
        session_id="s", step_index=0, path="cfg.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="a = 1"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="a = 1\nb = 2"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="a = 1\nc = 3"),
        original_worktree_text="a = 1", marker_span=(0, 0),
    )


def test_candidate_default_provenance_is_legacy_empty():
    """A candidate built without provenance (e.g. _failed_candidate, old code)
    defaults to the legacy empty string, not None or a guess."""
    cand = CandidateResolution(
        candidate_id="u:c", unit_id="u", model_name="m",
        prompt_version="p", resolved_text="x",
    )
    assert cand.provenance == LEGACY_PROVENANCE


def test_structural_candidate_carries_structural_provenance():
    """The structural resolver stamps deterministic_structural."""
    from capybase.orchestrator import Orchestrator
    import inspect

    # We assert the stamp exists in the source rather than spinning up a full
    # resolver pass: the stamp is a literal at the construction site.
    src = inspect.getsource(Orchestrator._try_structural_resolve)
    assert 'provenance="deterministic_structural"' in src


def _extract_call_sites(text: str, name: str) -> list[str]:
    """Yield the balanced-arg body of each ``name(...)`` call in ``text``.

    Walks matching parens (respecting strings) so a ``)`` inside an f-string or
    a nested call doesn't prematurely close the match — a naive non-greedy regex
    trips on e.g. ``f"...balance={bal:.2f})"``.
    """
    sites: list[str] = []
    i = 0
    needle = name + "("
    while True:
        start = text.find(needle, i)
        if start == -1:
            break
        depth = 1
        j = start + len(needle)
        in_str: str | None = None
        while j < len(text) and depth > 0:
            c = text[j]
            if in_str:
                if c == "\\":
                    j += 2
                    continue
                if c == in_str:
                    in_str = None
            elif c in ('"', "'"):
                in_str = c
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            j += 1
        sites.append(text[start:j])
        i = j
    return sites


def test_each_resolution_site_stamps_a_known_provenance():
    """Guard: every CandidateResolution(...) in the orchestrator and
    resolution_engine sets a provenance that is a valid enum value. Catches a
    future construction site that forgets the field."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "capybase"
    files = [root / "orchestrator.py", root / "resolution_engine.py"]
    for f in files:
        text = f.read_text()
        for site in _extract_call_sites(text, "CandidateResolution"):
            body = site
            # _failed_candidate builds technical-failure candidates that are
            # never accepted/recorded — they may omit provenance (legacy "").
            is_failed = "needs_human=True" in body and "failure_kind=" in body
            pm = re.search(r'provenance\s*=\s*"([^"]*)"', body)
            if pm:
                assert is_valid(pm.group(1)), f"{f.name}: invalid provenance {pm.group(1)!r}"
            else:
                assert is_failed, (
                    f"{f.name}: CandidateResolution site missing provenance and is "
                    f"not the _failed_candidate path:\n{body[:160]}"
                )


# ---------------------------------------------------------------------------
# Experience persistence + backward compatibility
# ---------------------------------------------------------------------------


def test_experience_round_trips_provenance(tmp_path):
    """Provenance survives to_dict/from_dict so the corpus can be sliced."""
    store = ExperienceStore(tmp_path / "exp.jsonl")
    store.append(
        Experience(
            example=__import__(
                "capybase.conflict_model", fromlist=["HistoricalExample"]
            ).HistoricalExample(
                summary="cfg.py:u", base="a", current="b", replayed="c",
                resolved="bc", source="s",
            ),
            outcome="accepted",
            language="python",
            path="cfg.py",
            provenance="exact_history_reuse",
        )
    )
    loaded = list(store)
    assert len(loaded) == 1
    assert loaded[0].provenance == "exact_history_reuse"
    assert loaded[0] in store.accepted()


def test_old_jsonl_line_without_provenance_loads_as_legacy(tmp_path):
    """A pre-step-8 JSONL line (no provenance key) loads as the legacy empty
    string — no KeyError, no crash. Backward compatibility for existing repos."""
    p = tmp_path / "exp.jsonl"
    # An old-style line: every field except provenance.
    old = {
        "example": {
            "summary": "cfg.py:u", "base": "a", "current": "b",
            "replayed": "c", "resolved": "bc", "source": "s",
        },
        "outcome": "accepted",
        "language": "python",
        "path": "cfg.py",
        "session_id": "s",
        "unit_id": "u",
        "validator_features": {},
        "risk_score": None,
        "retry_count": 0,
        "history_features": {},
    }
    p.write_text(json.dumps(old) + "\n")
    store = ExperienceStore(p)
    loaded = list(store)
    assert len(loaded) == 1
    assert loaded[0].provenance == LEGACY_PROVENANCE


def test_to_dict_includes_provenance_key():
    """to_dict always emits the key (so a fresh store is uniformly shaped)."""
    e = Experience(
        example=__import__(
            "capybase.conflict_model", fromlist=["HistoricalExample"]
        ).HistoricalExample(
            summary="x", base="a", current="b", replayed="c", resolved="d", source="s",
        ),
        outcome="accepted",
    )
    d = e.to_dict()
    assert "provenance" in d
    assert d["provenance"] == LEGACY_PROVENANCE
