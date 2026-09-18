"""CaseResult.model_involved and _CallCountingClient — the README llm
column's source of truth (2026-09-18): any model call during the case's
whole resolution process, counted at the client surface so every call
path (candidates, repairs, ballots, comment reconciliation) is caught.
"""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path


def _load_mod():
    spec = importlib.util.spec_from_file_location(
        "live_eval_realworld",
        Path(__file__).resolve().parent.parent / "scripts"
        / "live_eval_realworld.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["live_eval_realworld"] = mod
    spec.loader.exec_module(mod)  # type: ignore[arg-type]
    return mod


_mod = _load_mod()


class _FakeClient:
    def __init__(self):
        self.config = type("C", (), {"max_tokens": 4096})()

    def complete(self, *a, **k):
        return "one"

    def complete_many(self, *a, **k):
        return ["many"]

    def raw_complete(self, *a, **k):
        return "raw"

    def never_called(self):
        return "attr"


def test_counter_counts_all_three_surfaces():
    c = _mod._CallCountingClient(_FakeClient())
    c.complete()
    c.raw_complete()
    c.complete_many()
    assert c.calls == 3


def test_counter_zero_without_calls_and_delegates():
    inner = _FakeClient()
    c = _mod._CallCountingClient(inner)
    assert c.calls == 0
    # non-call attributes delegate transparently (orchestrator reads
    # client.config etc.)
    assert c.config.max_tokens == 4096
    assert c.never_called() == "attr"
    assert c.calls == 0  # delegation must not count


def test_caseresult_field_defaults_and_serializes():
    r = _mod.CaseResult(id="x", language="c", dataset="d")
    assert r.model_involved is None  # predates-flag sentinel
    d = dict(r.__dict__)  # the results-file serialization path
    assert d["model_involved"] is None
    # resume-path filter: the field is a dataclass field, so a row
    # carrying it deserializes; a row without it keeps the default.
    r2 = _mod.CaseResult(**{k: v for k, v in {
        "id": "y", "language": "c", "dataset": "d",
        "model_involved": True}.items()
        if k in _mod.CaseResult.__dataclass_fields__})
    assert r2.model_involved is True
