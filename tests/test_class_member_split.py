"""Class-with-methods splitting, journal-only stage — sprint-19 P5.

The v3 entity splitter's top-level parser sees a C++ class as ONE
entity, so an oversized region dominated by a single class yields one
giant fragment and the unit escalates as oversized before any candidate
exists (protobuf-0055: 15.5-16.3K-token prompt vs the 8K window). This
stage MEASURES the depth-2 alternative — member-function boundaries
inside the class body — and journals it at the oversized-skip sites.
No behavior change; enabling awaits corpus calibration (the flag
``future.enable_class_member_splitting`` defaults OFF).
"""

from __future__ import annotations

from types import SimpleNamespace

from capybase.conflict_extractor import (
    _class_member_split_points,
    _split_unit_at_entities,
    _stamp_class_member_candidate,
)
from capybase.conflict_model import ConflictSide, ConflictUnit


_CLASS = """class Widget {
public:
  Widget() : x_(0) {}

  int get() const { return x_; }

  void set(int x);

  void render(Graphics& g) {
    draw(g);
    flush(g);
  }

private:
  int x_;
  Buffer buf_;
};
"""

_PLAIN_FUNCTIONS = """int a(int x) {
  return x + 1;
}

int b(int y) {
  return y * 2;
}
"""


def _unit(cur: str, rep: str, *, span=(0, 200), base: str = "") -> ConflictUnit:
    return ConflictUnit(
        session_id="s", step_index=1, path="w.cc", language="cpp",
        unit_id="w.cc:0:0", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=cur),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=rep),
        original_worktree_text="x\n" * 400,
        marker_span=span,
    )


# ---------------------------------------------------------------------------
# _class_member_split_points — the depth-2 boundary detector
# ---------------------------------------------------------------------------

def test_detector_finds_members_and_specifiers():
    pts = _class_member_split_points(_CLASS, "cpp")
    lines = _CLASS.split("\n")
    got = {lines[p].strip() for p in pts}
    assert "public:" in got
    assert "private:" in got
    assert "Widget() : x_(0) {}" in got
    assert "int get() const { return x_; }" in got
    assert "void render(Graphics& g) {" in got


def test_detector_skips_declarations_and_data_members():
    pts = _class_member_split_points(_CLASS, "cpp")
    lines = _CLASS.split("\n")
    got = {lines[p].strip() for p in pts}
    assert "void set(int x);" not in got    # declaration, no body
    assert "int x_;" not in got             # data member
    assert "Buffer buf_;" not in got


def test_detector_ignores_non_cpp():
    assert _class_member_split_points(_CLASS, "rust") == []
    assert _class_member_split_points("", "cpp") == []


def test_detector_plain_functions_no_class_depth():
    # top-level functions are depth-0/1-relative-to-file, not class members
    pts = _class_member_split_points(_PLAIN_FUNCTIONS, "cpp")
    assert pts == []


# ---------------------------------------------------------------------------
# _stamp_class_member_candidate — the measurement stamp
# ---------------------------------------------------------------------------

def test_stamp_records_measurement():
    u = _unit(_CLASS, "stale comment")
    _stamp_class_member_candidate(u, _CLASS, "stale comment", "cpp",
                                  "no_top_level_boundary")
    cand = u.structural_metadata["class_member_split_candidate"]
    assert cand["decline_reason"] == "no_top_level_boundary"
    assert cand["current_member_points"] >= 4
    assert cand["replayed_member_points"] == 0
    assert cand["region_lines"] == 201


def test_stamp_declines_when_no_members():
    u = _unit(_PLAIN_FUNCTIONS, _PLAIN_FUNCTIONS)
    _stamp_class_member_candidate(u, _PLAIN_FUNCTIONS, _PLAIN_FUNCTIONS,
                                  "cpp", "no_top_level_boundary")
    assert "class_member_split_candidate" not in u.structural_metadata


def test_stamp_ignores_rust():
    u = _unit(_CLASS, "")
    _stamp_class_member_candidate(u, _CLASS, "", "rust",
                                  "no_top_level_boundary")
    assert "class_member_split_candidate" not in u.structural_metadata


# ---------------------------------------------------------------------------
# Splitter decline path stamps the unit (integration)
# ---------------------------------------------------------------------------

def test_split_decline_stamps_class_candidate():
    # A C++ region whose sides carry NO top-level entity boundaries (the
    # class is one entity) — the splitter declines and stamps.
    u = _unit(_CLASS, "int boring;\n")
    out = _split_unit_at_entities(u, min_region_lines=10, min_sub_lines=8)
    assert out == [u]
    cand = u.structural_metadata.get("class_member_split_candidate")
    assert cand and cand["current_member_points"] >= 4


def test_split_normal_path_unstamped():
    # Multiple top-level entities split as before; no member stamp.
    u = _unit(_PLAIN_FUNCTIONS + "\nint c() { return 3; }\n", "")
    out = _split_unit_at_entities(u, min_region_lines=5, min_sub_lines=2)
    assert len(out) >= 2
    assert "class_member_split_candidate" not in u.structural_metadata


# ---------------------------------------------------------------------------
# Orchestrator journaling at the oversized-skip sites
# ---------------------------------------------------------------------------

class _RecJournal:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, payload, **_kw):
        self.events.append((event, payload))


def test_journal_class_member_candidate_emits_with_flag_state():
    from capybase.orchestrator import Orchestrator

    orch = object.__new__(Orchestrator)
    orch.journal = _RecJournal()
    orch.step = 1
    orch.config = SimpleNamespace(future=SimpleNamespace(
        enable_class_member_splitting=False))
    u = _unit(_CLASS, "")
    _stamp_class_member_candidate(u, _CLASS, "", "cpp",
                                  "no_top_level_boundary")
    orch._journal_class_member_candidate(u)
    events = [p for e, p in orch.journal.events
              if e == "class_member_split_candidate"]
    assert events and events[0]["enabled"] is False
    assert events[0]["current_member_points"] >= 4


def test_journal_noop_without_stamp():
    from capybase.orchestrator import Orchestrator

    orch = object.__new__(Orchestrator)
    orch.journal = _RecJournal()
    orch.step = 1
    orch.config = SimpleNamespace(future=SimpleNamespace(
        enable_class_member_splitting=False))
    orch._journal_class_member_candidate(_unit("int x;", "int y;"))
    assert orch.journal.events == []


# --- sprint-21 S21.5: the enabling composition ---


def test_member_split_enabled_rescues_the_decline_class():
    """Flag ON + member points on either side → the decline class splits
    into proportional sub-units (the S20.10 build); flag OFF → the
    sprint-19 journal-only behavior (unit returned unsplit, stamped)."""
    from capybase.conflict_extractor import (
        _split_unit_at_entities,
        _class_member_split_points,
    )
    cls_cur = (
        "class C {\n public:\n"
        + "".join(f"  int m{i}() {{ return {i}; }}\n" for i in range(30))
        + "  void big() {\n" + "".join(f"    s{i};\n" for i in range(60))
        + "  }\n};\n"
    )
    cls_rep = "class C {\n public:\n  int other() { return 2; }\n};\n"
    unit = _unit(cls_cur, cls_rep)
    pts = _class_member_split_points(cls_cur, "cpp")
    assert len(pts) >= 3  # the fixture has member boundaries

    out_off = _split_unit_at_entities(
        unit, min_region_lines=5, min_sub_lines=8, member_split=False)
    assert out_off == [unit]  # decline, journal-only (stamped)

    out_on = _split_unit_at_entities(
        unit, min_region_lines=5, min_sub_lines=8, member_split=True)
    assert len(out_on) == len(pts) + 1  # member-count proportional split
    # spans are contiguous and cover the marker region (no reordering)
    spans = [u.marker_span for u in out_on]
    assert spans[0][0] == unit.marker_span[0]
    assert spans[-1][1] == unit.marker_span[1]
    for (a1, a2), (b1, b2) in zip(spans, spans[1:]):
        assert a2 < b1  # non-overlapping, ascending (separator gaps by design)


def test_member_split_no_points_still_declines():
    from capybase.conflict_extractor import _split_unit_at_entities
    # no member points on either side (plain C-style text)
    cur = "int a; // " + "x" * 200 + "\n" * 5
    unit = _unit(cur * 8, cur * 8)
    out = _split_unit_at_entities(
        unit, min_region_lines=5, min_sub_lines=8, member_split=True)
    assert out == [unit]
