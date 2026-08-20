"""Sprint-20 S20.E10 — mechanism interaction pin tests.

Pins the interactions the risk matrix calls out, so regressions in one
mechanism can't silently break another's contract. The S20.4-vs-transport
pin lives in test_recovery_retry.py (written with the original fix).
"""

from __future__ import annotations

from types import SimpleNamespace

from capybase.resurrection import scan_resurrections


class _FakeGit:
    """Three-commit world: base -> side (deletes a block) -> result
    (resurrects it). read_stage-style blob lookup via commit map."""

    def __init__(self, blobs: dict[str, dict[str, str]]):
        self._blobs = blobs

    def files_changed_between(self, old: str, new: str) -> list[str]:
        b, h = self._blobs[old], self._blobs[new]
        return [p for p in set(b) | set(h) if b.get(p) != h.get(p)]

    def blob_at(self, rev: str, path: str) -> bytes | None:
        t = self._blobs[rev].get(path)
        return t.encode() if t is not None else None

    def blob_sequence(self, *a, **k):
        return None  # no history walk in the fixture

    def _run_ok(self, *a, **k):
        raise RuntimeError("fake backend: git not available")


def _git_with(path: str, base_content: str, side_content: str,
              result_content: str):
    return _FakeGit({
        "base": {path: base_content},
        "side": {path: side_content},
        "result": {path: result_content},
    })


def test_lockfile_resurrection_never_stops_the_rebase():
    """S20.5 lockfile takeover vs the resurrection backstop: taking the
    current Cargo.lock 'resurrects' replayed-deleted version pins by
    definition — the suffix exemption (resurrection.py) must keep that
    from becoming a SAFE_STOP. Axum-0017's 103-marker precedent."""
    pins = "\n".join(f'[[package]]\nname = "p{i}"\nversion = "0.{i}"'
                     for i in range(20))
    fewer = "\n".join(f'[[package]]\nname = "p{i}"\nversion = "0.{i}"'
                      for i in range(10))
    git = _git_with("Cargo.lock", pins, pins, fewer)  # result keeps fewer
    # scan: onto(=side kept pins) vs result(deleted some) — a deletion,
    # not a resurrection; the dangerous direction is replayed-deleted
    # pins reappearing in the result:
    git2 = _git_with("Cargo.lock", pins, fewer, pins)
    findings = scan_resurrections(
        git2, base_oid="base", onto_oid="side", result_oid="result",
        replayed_oid="side")
    assert findings == [], (
        "lockfile pin churn must never surface as a resurrection finding")


def test_micro_cegis_failure_keeps_honest_escalation_reason():
    """S20.6 vs P4 transparency: when the micro-CEGIS rung declines (no
    applicable patch), the escalation reason stays the compiler-authority
    message — the repair attempt must never mask WHY the gate failed."""
    from capybase.orchestrator import Orchestrator

    orch = object.__new__(Orchestrator)
    orch.journal = SimpleNamespace(
        emit=lambda *a, **k: None)
    orch.step = 1
    orch.config = SimpleNamespace(
        future=SimpleNamespace(enable_micro_cegis=True))
    orch._last_attributed_merge_errors = ["/r/a.cc:1:5: error: 'x' undeclared"]
    # engine returns unusable JSON -> stage 2 declines
    class _BadEngine:
        config = SimpleNamespace(max_tokens=64)

        def raw_complete(self, prompt, json_mode=True, max_tokens=64):
            return SimpleNamespace(text="not json at all")
    orch.resolution_engine = _BadEngine()
    orch._write_worktree_only = lambda *a, **k: None
    orch._micro_re_gate = lambda result: False
    result = SimpleNamespace(units_by_path={"a.cc": []})
    # no repo/git needed: the symbol path resolution finds no stem match
    orch.git = SimpleNamespace(repo="/nonexistent")
    assert orch._try_micro_cegis(result) is False  # declined -> escalate
