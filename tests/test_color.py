"""Tests for the ANSI color styling module.

The core contract: a *disabled* styler is a passthrough (no escape codes), so
color is opt-in and existing string assertions hold when it's off. An *enabled*
styler wraps text in SGR codes terminated by RESET.
"""

from __future__ import annotations

import io

import pytest

from capybase.color import (
    BOLD,
    CYAN,
    GREEN,
    MAGENTA,
    RED,
    RESET,
    color_enabled,
    make_styler,
    style,
)


# ---------------------------------------------------------------------------
# style()
# ---------------------------------------------------------------------------


def test_style_wraps_text_with_reset():
    out = style("err", RED)
    assert out.startswith(RED)
    assert out.endswith(RESET)
    assert "err" in out


def test_style_multiple_codes_concatenate():
    out = style("x", BOLD, RED)
    assert out.startswith(BOLD + RED)
    assert out.endswith(RESET)


def test_style_no_codes_returns_text_unchanged():
    assert style("plain") == "plain"


# ---------------------------------------------------------------------------
# make_styler() — the single gate
# ---------------------------------------------------------------------------


def test_disabled_styler_is_passthrough():
    """A disabled styler returns the text unchanged, ignoring all codes.

    This is what keeps existing tests green: orchestrators are constructed with
    color=False (the default), so every self.style(...) call is a no-op and
    rendered strings are byte-identical to the un-colored baseline."""
    styler = make_styler(False)
    assert styler("err", RED) == "err"
    assert styler("x", BOLD, GREEN) == "x"
    # No escape sequences leak.
    assert "\x1b" not in styler("err", RED)


def test_enabled_styler_emits_codes():
    styler = make_styler(True)
    out = styler("err", RED)
    assert out.startswith(RED)
    assert out.endswith(RESET)


# ---------------------------------------------------------------------------
# color_enabled() — env conventions
# ---------------------------------------------------------------------------


def test_color_enabled_no_color_disables(monkeypatch):
    """NO_COLOR (any value) disables even on a TTY."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    tty = io.TextIOWrapper(io.BytesIO())  # not a real TTY but NO_COLOR wins anyway
    assert color_enabled(tty) is False


def test_color_enabled_force_color_enables(monkeypatch):
    """FORCE_COLOR enables even when not a TTY (piped/redirected)."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    not_a_tty = type("S", (), {"isatty": lambda self: False})()
    assert color_enabled(not_a_tty) is True


def test_color_enabled_follows_tty(monkeypatch):
    """Without NO_COLOR/FORCE_COLOR, color follows stream.isatty()."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    tty = type("S", (), {"isatty": lambda self: True})()
    not_tty = type("S", (), {"isatty": lambda self: False})()
    assert color_enabled(tty) is True
    assert color_enabled(not_tty) is False


def test_color_enabled_no_isattr_is_safe(monkeypatch):
    """A stream without isatty (e.g. a StringIO) → treated as non-TTY → no color."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    assert color_enabled(io.StringIO()) is False


def test_color_enabled_no_color_overrides_force(monkeypatch):
    """NO_COLOR takes precedence over FORCE_COLOR."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FORCE_COLOR", "1")
    tty = type("S", (), {"isatty": lambda self: True})()
    assert color_enabled(tty) is False


def test_color_enabled_empty_env_values_ignored(monkeypatch):
    """Empty NO_COLOR/FORCE_COLOR values are treated as unset (any *non-empty*
    value is the convention, but empty strings shouldn't spuriously trigger)."""
    monkeypatch.setenv("NO_COLOR", "")
    monkeypatch.setenv("FORCE_COLOR", "")
    tty = type("S", (), {"isatty": lambda self: True})()
    assert color_enabled(tty) is True
