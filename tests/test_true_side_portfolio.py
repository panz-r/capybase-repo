"""True-side portfolio — the cross-ordered-blocks pathology (protobuf-0063 class).

When git's line-aligned merge interleaves two differently-ordered expansions,
duplicate definitions land in the SHARED context outside every marker span —
per-region resolution is structurally insufficient. These tests pin the pure
helpers behind the true-stage-side fallback: stage reading, the splice-
divergence detector, and the churn heuristic validated on the corpus's four
known whole-file cases. No model, no network, no real git.
"""

from __future__ import annotations

from capybase.conflict_model import ConflictSide, ConflictUnit  # noqa: F401
from capybase.orchestrator import (
    _classify_build_error_lines,
    _shared_context_duplicate_definitions,
    _true_stage_sides,
    _whole_side_adjudication_prompt,
    _whole_side_heuristic,
)


class _FakeGit:
    """Reads stage blobs from an in-memory dict — {stage: text}."""

    def __init__(self, stages: dict[int, str]):
        self._stages = stages

    def read_stage_blob(self, path: str, stage: int) -> bytes:
        if stage not in self._stages:
            raise RuntimeError(f"no stage {stage}")
        return self._stages[stage].encode()


# ---------------------------------------------------------------------------
# _true_stage_sides
# ---------------------------------------------------------------------------

def test_true_stage_sides_reads_stages_2_and_3():
    git = _FakeGit({1: "base\n", 2: "cur\n", 3: "rep\n"})
    sides, base = _true_stage_sides(git, "f.cc")
    assert sides == {"current": "cur\n", "replayed": "rep\n"}
    assert base == "base\n"


def test_true_stage_sides_missing_stage_returns_none():
    assert _true_stage_sides(_FakeGit({2: "cur\n"}), "f.cc") is None


def test_true_stage_sides_crlf_normalized():
    git = _FakeGit({2: "a\r\nb\r\n", 3: "c\r\n"})
    sides, _ = _true_stage_sides(git, "f.cc")
    assert sides["current"] == "a\nb\n"


# ---------------------------------------------------------------------------
# _shared_context_duplicate_definitions — the pathology detector
# ---------------------------------------------------------------------------

def test_healthy_merge_no_duplicates():
    original = (
        "#include <x>\n"
        "void a() { }\n"
        "<<<<<<< A\n"
        "void b() { }\n"
        "=======\n"
        "void c() { }\n"
        ">>>>>>> B\n"
        "int main() { return 0; }\n"
    )
    assert _shared_context_duplicate_definitions(original, "cpp") == []


def test_interleaved_shared_context_duplicates_fire():
    # The 0063 shape: git placed BOTH sides' copies of the same definitions
    # into shared context — per-region resolution can't deduplicate them.
    original = (
        "void setup() {\n"
        "}\n"
        "<<<<<<< A\n"
        "void a() {\n"
        "=======\n"
        "void b() {\n"
        ">>>>>>> B\n"
        "void helper(int x) {\n"
        "}\n"
        "void cleanup() {\n"
        "}\n"
        "void helper(int x) {\n"
        "}\n"
    )
    dupes = _shared_context_duplicate_definitions(original, "cpp")
    assert len(dupes) == 1
    assert dupes[0] == "void helper(int x) {"


def test_overloads_do_not_fire():
    # Different parameters → different signatures → not duplicates.
    original = (
        "void helper(int x) { }\n"
        "void helper(double x) { }\n"
        "void helper(int x, int y) { }\n"
    )
    assert _shared_context_duplicate_definitions(original, "cpp") == []


def test_control_flow_not_treated_as_definitions():
    original = (
        "void f() {\n"
        "  if (x) { g(); }\n"
        "  while (y) { h(); }\n"
        "  for (int i = 0; i < 3; ++i) { k(); }\n"
        "  switch (z) { case 1: break; }\n"
        "}\n"
    )
    assert _shared_context_duplicate_definitions(original, "cpp") == []


def test_python_and_rust_families():
    py = "def f(x):\n    pass\ndef f(x):\n    pass\n"
    assert len(_shared_context_duplicate_definitions(py, "python")) == 1
    rs = "pub fn a() {\n}\npub fn b() {\n}\npub fn a() {\n}\n"
    assert len(_shared_context_duplicate_definitions(rs, "rust")) == 1
    assert _shared_context_duplicate_definitions("whatever", "brainfuck") == []


def test_conflict_region_content_ignored():
    # Duplicates that live INSIDE conflict regions are resolvable — the
    # detector must only look at shared context.
    original = (
        "void a() { }\n"
        "<<<<<<< A\n"
        "void dup() { }\n"
        "=======\n"
        "void dup() { }\n"
        ">>>>>>> B\n"
    )
    assert _shared_context_duplicate_definitions(original, "cpp") == []


# ---------------------------------------------------------------------------
# _classify_build_error_lines — sibling-vs-merge attribution
# ---------------------------------------------------------------------------

def test_fatal_error_in_sibling_file_is_environmental():
    # The protobuf-0063 regression: gcc's "fatal error:" phrasing (missing
    # includes) must parse, or the line falls into the conservative fallback
    # and a pre-existing sibling failure rejects the merge.
    lines = [
        "/var/tmp/capy-rw-x/r/upb/decode.c:7:10: fatal error: "
        "upb/decode.int.h: No such file or directory",
        "make[2]: *** [Makefile:512: upb/decode.lo] Error 1",
    ]
    merge, env = _classify_build_error_lines(lines, "tests/benchmark.cc")
    assert merge == []
    assert env == 2


def test_conflict_file_error_is_merge_relevant():
    lines = [
        "tests/benchmark.cc:120:6: error: redefinition of "
        "'void BM_ParseDescriptor_Upb(int)'",
        "make[1]: *** [Makefile:30: benchmark.o] Error 1",
    ]
    merge, env = _classify_build_error_lines(lines, "tests/benchmark.cc")
    assert len(merge) == 1
    assert "redefinition" in merge[0]
    assert env == 1


def test_unparseable_lines_stay_merge_relevant():
    # Opaque failures keep the conservative behavior (no environmental pass).
    merge, env = _classify_build_error_lines(
        ["something went wrong"], "tests/benchmark.cc")
    assert merge == ["something went wrong"]
    assert env == 0


# ---------------------------------------------------------------------------
# _whole_side_heuristic — validated on the four known whole-file cases
# ---------------------------------------------------------------------------

def _churny(base_n, cur_add, rep_add):
    base = "\n".join(f"b{i}" for i in range(base_n)) + "\n"
    cur = "\n".join(f"b{i}" for i in range(base_n - 2)) + "\n" + "\n".join(f"c{i}" for i in range(cur_add)) + "\n"
    rep = "\n".join(f"b{i}" for i in range(base_n - 2)) + "\n" + "\n".join(f"r{i}" for i in range(rep_add)) + "\n"
    return base, {"current": cur, "replayed": rep}


def test_heuristic_symmetric_refinement_prefers_replayed():
    # 0063 shape: both sides did same-sized work on the same block — the
    # replayed commit is the newer pass.
    base, sides = _churny(20, 30, 30)
    assert _whole_side_heuristic(base, sides) == "replayed"


def test_heuristic_massive_asymmetry_prefers_higher_churn_side():
    # 0067/0073 shape: current rewrote/deleted massively, replayed ~base.
    base, sides = _churny(20, 120, 4)
    assert _whole_side_heuristic(base, sides) == "current"
    base, sides = _churny(20, 4, 120)
    assert _whole_side_heuristic(base, sides) == "replayed"


def test_heuristic_ignores_marker_noise():
    # Identical sides (no churn anywhere) → replayed by default.
    base, sides = _churny(20, 0, 0)
    assert _whole_side_heuristic(base, sides) == "replayed"


# ---------------------------------------------------------------------------
# _whole_side_adjudication_prompt
# ---------------------------------------------------------------------------

def test_adjudication_prompt_contains_both_sides_and_strict_json():
    prompt = _whole_side_adjudication_prompt(
        "a.cc", "cpp", "base\ntext\n",
        {"current": "int cur;\n", "replayed": "int rep;\n"})
    assert '"choice": "current" or "replayed"' in prompt
    assert "int cur;" in prompt and "int rep;" in prompt
    assert "CURRENT" in prompt and "REPLAYED" in prompt
