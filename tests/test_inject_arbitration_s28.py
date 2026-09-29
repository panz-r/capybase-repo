"""S28-365 D3 — the starved-census arbitration.

The census (38 starved_by_inject events, t38-t42): every event on the
duckdb era family, three symbols, each mapping to EXACTLY ONE failure
shape — ParserCache type-shape ("does not name a type"), cache/state
undeclared-shape ("was not declared"); zero rename hints. The
arbitration: when the round's FIRST absent-symbol message is the type
shape, the symbol-injection declines (visibly) and the tree-absent
deletion rung — later in the same beam — gets the round. A type the
tree erased cannot be declared back into existence.
"""

from __future__ import annotations

from types import SimpleNamespace

from capybase.orchestrator import _failures_shape_is_type_only


def _f(msg):
    return SimpleNamespace(message=msg)


def test_type_shape_routes_to_deletion():
    assert _failures_shape_is_type_only([
        _f("src/x.cpp:40:1: error: 'ParserCache' does not name a type"),
    ])


def test_undeclared_shape_routes_to_injection():
    assert not _failures_shape_is_type_only([
        _f("src/x.cpp:115:20: error: 'cache' was not declared in this scope"),
    ])


def test_first_named_symbol_wins_the_rotation():
    # the era rotation: different symbols, different shapes, one list —
    # the FIRST absent-symbol message is the round's routing fact
    mixed = [
        _f("src/x.cpp:1935:1: error: 'cache' was not declared in this scope"),
        _f("src/x.cpp:40:1: error: 'ParserCache' does not name a type"),
    ]
    assert not _failures_shape_is_type_only(mixed)
    reordered = list(reversed(mixed))
    assert _failures_shape_is_type_only(reordered)


def test_typographic_quotes_normalized():
    assert _failures_shape_is_type_only([
        _f("src/x.cpp:40:1: error: \u2018ParserCache\u2019 does not name a type"),
    ])


def test_no_absent_symbol_message_is_neutral():
    assert not _failures_shape_is_type_only([
        _f("src/x.cpp:1:1: error: expected ';' before '}' token"),
    ])
    assert not _failures_shape_is_type_only([])


def test_wiring_flag_exists():
    from capybase.config import Config
    assert Config().future.enable_inject_arbitration is False
