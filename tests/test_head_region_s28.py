"""S28-176(a) — terminal gcc diagnostics, attributed (eval-only).

The batch-19 finding: 11 of the 19 near-oracle REPAIR_FAILUREs died on
file-level gcc errors in the include/type-visibility HEAD region the
conflict units occupy (libuv-0089's uv_loop_t at line 3, duckdb-0099's
optional_ptr at 1:1). The attribution field parses the LAST gcc
diagnostic from the captured session events and flags the head shape,
so the rerun census sizes the class corpus-wide.
"""

from __future__ import annotations

from types import SimpleNamespace

import importlib.util
import sys
from pathlib import Path


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "live_eval_realworld_hr",
        Path(__file__).resolve().parent.parent / "scripts" / "live_eval_realworld.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["live_eval_realworld_hr"] = mod
    spec.loader.exec_module(mod)  # type: ignore[arg-type]
    return mod


_M = _load_module()


def _ev(t, **payload):
    return SimpleNamespace(event_type=t, payload=payload)


def _unit_ev(line):
    return _ev("conflict_unit_extracted",
               unit_id=f"src/f.cpp:{line}:0", language="cpp")


def test_head_error_with_head_units_is_flagged():
    events = [
        _unit_ev(1),
        _ev("build_probe", cmd="cmake --build build", duration_s=2.0,
            outcome="fail",
            errors="src/f.cpp:3:14: error: unknown type name 'uv_loop_t'"),
    ]
    line, head = _M._terminal_error_attribution(events)
    assert line == 3 and head is True


def test_body_error_is_not_head_region():
    events = [
        _unit_ev(1),
        _ev("build_probe", cmd="make", duration_s=1.0, outcome="fail",
            errors="src/f.cpp:914:87: error: 'orders' is private within "
                   "this context"),
    ]
    line, head = _M._terminal_error_attribution(events)
    assert line == 914 and head is False


def test_units_far_from_head_break_the_head_shape():
    """An error at line 40 of a file whose units start at line 300 is a
    body interaction, not head-region visibility damage."""
    events = [
        _unit_ev(300),
        _ev("tests_finished", timed_out=False,
            stderr_tail="src/g.cpp:40:5: error: 'X' does not name a type"),
    ]
    line, head = _M._terminal_error_attribution(events)
    assert line == 40 and head is False


def test_last_diagnostic_wins():
    events = [
        _ev("build_probe", outcome="fail",
            errors="src/a.cpp:2:1: error: first one\nsrc/b.cpp:700:2: error: last one"),
    ]
    line, _head = _M._terminal_error_attribution(events)
    assert line == 700


def test_no_diagnostics_is_none():
    assert _M._terminal_error_attribution([]) == (None, False)
    assert _M._terminal_error_attribution(
        [_ev("build_probe", outcome="pass", duration_s=1.0)]) == (None, False)


def test_non_string_payload_fields_ignored():
    events = [_ev("build_probe", errors=None, outcome="fail")]
    assert _M._terminal_error_attribution(events) == (None, False)
