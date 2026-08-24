"""C4 (sprint-22): repair-diversity rotation in _whole_file_repair.

A deterministic repair that already failed for a failure signature never
re-runs for the same signature — the round falls through to the next
strategy instead of repeating itself (axum-0013: two whole-file repair
rounds ran the identical failed brace repair to the identical failure).
"""

from types import SimpleNamespace

from capybase.orchestrator import Orchestrator

from tests.test_micro_cegis import _FakeGit, _RecJournal


def _c4_orch(tmp_path):
    orch = object.__new__(Orchestrator)
    orch.journal = _RecJournal()
    orch.step = 1
    repo = tmp_path / "r"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "f.c").write_text("int f() {\n    return 1;\n")
    orch.git = _FakeGit(repo, {
        1: "int f() {\n    return 1;\n",
        2: "int f() {\n    return 1;\n",
        3: "int f() {\n    return 1;\n",
    })
    orch.verification = None
    orch.config = SimpleNamespace(
        policy=SimpleNamespace(max_whole_file_repair_seconds=0),
        future=SimpleNamespace(enable_micro_cegis=True),
    )
    orch._write_worktree_only = lambda *a, **k: None
    return orch


# A brace-shaped failure the deterministic repair cannot fix (two
# unclosed braces — the rung handles a single imbalance only).
def _failure(msg: str) -> SimpleNamespace:
    return SimpleNamespace(validator="syntax", message=msg, detail={})


_BRACE_FAILURES = [_failure(
    "splice coherence: unbalanced braces at line 4 "
    "(missing closing brace — 2 unclosed '{' (add 2 '}' before line 4))")]


def test_failed_repair_never_repeats_for_same_signature(tmp_path):
    orch = _c4_orch(tmp_path)
    accepted: list = []
    original = "int f() {\n    return 1;\n"

    # Round 1: the brace repair runs and fails (journaled), the symbol
    # injection declines (nothing to inject for this signature).
    orch._whole_file_repair(
        "src/f.c", accepted, original, list(_BRACE_FAILURES),
        deterministic_only=True)
    kinds1 = [e[0] for e in orch.journal.events]
    assert "brace_repair_skipped" in kinds1

    # Round 2, identical signature: the brace repair is SKIPPED (rotation),
    # and the symbol injection is skipped too (already declined for this
    # signature). No mechanism repeats itself.
    orch._whole_file_repair(
        "src/f.c", accepted, original, list(_BRACE_FAILURES),
        deterministic_only=True)
    rotations = [e for e in orch.journal.events if e[0] == "repair_rotation"]
    skipped = {e[1].get("skipped") for e in rotations}
    assert "brace" in skipped
    assert "symbol_inject" in skipped

    # A DIFFERENT failure signature re-arms both repairs (a new defect
    # shape deserves fresh deterministic attempts).
    orch.journal.events.clear()
    orch._whole_file_repair(
        "src/f.c", accepted, original,
        [_failure("splice coherence: unbalanced braces at "
                  "line 9 (extra closing brace)")],
        deterministic_only=True)
    kinds3 = [e[0] for e in orch.journal.events]
    assert "repair_rotation" not in kinds3
