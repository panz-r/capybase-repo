"""Minimal ANSI color styling (stdlib only, no dependencies).

Color is applied at the *output boundary*: the orchestrator's render methods wrap
tokens with a ``styler`` returned by :func:`make_styler`. The single switch is
whether that styler emits SGR escape codes or is a passthrough — so when color is
disabled, every call returns the plain string unchanged and existing string
assertions hold without modification.

Detection (:func:`color_enabled`) honors the de-facto conventions:
- ``NO_COLOR`` (any value) → disable, even on a TTY (https://no-color.org)
- ``FORCE_COLOR`` (any value) → enable, even when piped/redirected
- otherwise → enable iff ``stream.isatty()``

Best-effort Windows VT-processing enablement (ctypes call to the kernel console
API) is attempted once on import; harmless on non-Windows / no-op on failure.
"""

from __future__ import annotations

import os
import sys
from typing import Callable

# SGR (Select Graphic Rendition) escape sequences. CSI = "\x1b[".
RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
BLUE = "\x1b[34m"
MAGENTA = "\x1b[35m"
CYAN = "\x1b[36m"

Styler = Callable[[str, str], str]


def style(text: str, *codes: str) -> str:
    """Wrap ``text`` in the given SGR ``codes``, terminated by RESET.

    ``codes`` are the escape-sequence constants above (e.g. ``RED``, ``BOLD``).
    Multiple codes concatenate (e.g. ``style("err", BOLD, RED)`` → bold red).
    RESET is always appended so the style never bleeds past the token.
    """
    if not codes:
        return text
    return f"{''.join(codes)}{text}{RESET}"


def make_styler(enabled: bool) -> Styler:
    """Return a styler: :func:`style` when enabled, a passthrough otherwise.

    The passthrough ignores all codes and returns the text unchanged — so a
    disabled styler adds zero escape sequences. This is the single gate: tests
    and ``--no-color`` runs construct the orchestrator with ``color=False``, and
    every ``self.style(...)`` call is a no-op, leaving rendered strings identical
    to the un-colored baseline (existing assertions hold unchanged).
    """
    if enabled:
        return style
    return lambda text, *_codes: text


def color_enabled(stream: object | None = None) -> bool:
    """Whether color should be emitted on ``stream`` (default stdout).

    Precedence: ``NO_COLOR`` disables unconditionally; ``FORCE_COLOR`` enables
    unconditionally; otherwise enable iff ``stream`` is a TTY. Streams without an
    ``isatty`` (e.g. a StringIO in tests) are treated as non-TTY → no color.
    """
    # NO_COLOR (any non-empty value) wins — disable even on a real TTY.
    if os.environ.get("NO_COLOR"):
        return False
    # FORCE_COLOR (any non-empty value) enables even when piped/redirected.
    if os.environ.get("FORCE_COLOR"):
        return True
    s = stream if stream is not None else sys.stdout
    return bool(getattr(s, "isatty", lambda: False)())


# --- best-effort Windows console VT enablement (harmless on non-Windows) ------

_win_vt_enabled = False


def _enable_windows_vt() -> None:
    """Enable ANSI escape processing on the Windows console (no-op elsewhere).

    Modern Windows 10+ consoles support ANSI but require virtual-terminal
    processing to be enabled via ``SetConsoleMode``. On Unix/other platforms this
    is a no-op. Any failure (non-Windows, older Windows, no console) is swallowed
    — color simply won't render, which is the safe degradation.
    """
    global _win_vt_enabled
    if _win_vt_enabled or os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        for handle in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            h = kernel32.GetStdHandle(handle)
            if not h:
                continue
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
                kernel32.SetConsoleMode(h, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        _win_vt_enabled = True
    except Exception:  # noqa: BLE001 - best effort; failure → no color on Windows
        _win_vt_enabled = False


_enable_windows_vt()
