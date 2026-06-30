"""A non-scrolling progress line with an animated spinner.

During ``capybase rebase`` the progress line holds the bottom terminal line and
updates in place (carriage-return + erase) at up to 10fps, while the normal
colored output scrolls above it. A background daemon thread animates the spinner
frame so it keeps spinning during silent waits (e.g. a long LLM call with no
output).

Design:
- The spinner thread is the ONLY writer of the bottom line when active.
- ``flush_line(text)`` is the coordination primitive for scrolling output: it
  clears the bottom line, writes ``text + \\n`` (which scrolls above), and
  re-paints the bottom line. The orchestrator routes ``self.out`` through it so a
  scrolling line never garbles the sticky spinner.
- ``pause()``/``resume()`` suppress painting while the terminal belongs to the
  human (interactive prompts); on pause the bottom line is cleared so the next
  ``print``/prompt starts clean.
- **TTY-guarded**: if the stream isn't a TTY, ``start()`` is a no-op and
  ``flush_line`` falls back to plain ``print`` — so piped/CI output is unaffected
  and all existing tests (no real TTY) pass unchanged.

This is the only module in capybase that uses terminal cursor control
(carriage-return / CSI erase). All other output is plain (possibly color-styled)
text via ``self.out``.
"""

from __future__ import annotations

import shutil
import sys
import threading
from typing import Callable

from capybase.color import BLUE, BOLD, style

# Unicode braille spinner frames — the modern de-facto spinner (cargo/rich/yarn).
# Rotates smoothly at 10fps; reads as "working". 10 frames for a full spin.
_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

# CSI: erase from cursor to end of line. Used after a carriage return to repaint.
_ERASE_EOL = "\x1b[K"


class Spinner:
    """A sticky bottom-line spinner, animated by a daemon thread.

    Construct with a stream (default stdout). ``start()`` begins animation iff
    the stream is a TTY; otherwise the spinner is inert (all methods are safe
    no-ops and ``flush_line`` behaves as plain ``print``).
    """

    def __init__(
        self,
        stream=None,
        *,
        fps: int = 10,
        isatty: Callable[[], bool] | None = None,
        get_width: Callable[[], int] | None = None,
    ) -> None:
        self.stream = stream if stream is not None else sys.stdout
        # TTY detection is overridable for tests (a real StringIO isn't a TTY;
        # tests that want to exercise the terminal path inject isatty=lambda:True).
        self._isatty = isatty if isatty is not None else (
            lambda: bool(getattr(self.stream, "isatty", lambda: False)())
        )
        self._get_width = get_width or (lambda: shutil.get_terminal_size().columns)
        self._fps = max(1, fps)
        self._interval = 1.0 / self._fps

        self._msg: str = ""
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Guards the "currently painting the bottom line" state so the main
        # thread's pause()/flush_line and the spinner thread's repaint don't
        # interleave. Held while painting; checked under the lock for pause.
        self._paint_lock = threading.Lock()
        self._painting = False  # bottom line currently has spinner content
        self._paused = False
        self._frame_idx = 0

    # ------------------------------------------------------------------ lifecycle

    @property
    def active(self) -> bool:
        """Whether the spinner thread is running (and thus the bottom line is
        managed). False for non-TTY streams or before start()/after stop()."""
        return self._thread is not None and self._thread.is_alive()

    def start(self, msg: str = "") -> None:
        """Begin animating. No-op if the stream isn't a TTY."""
        if not self._isatty():
            return  # inert: flush_line will fall back to plain print
        self._msg = msg
        self._stop.clear()
        self._paused = False
        self._painting = False
        self._frame_idx = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def set(self, msg: str) -> None:
        """Update the status text (picked up on the next frame)."""
        self._msg = msg

    def stop(self, final_msg: str | None = None) -> None:
        """Stop the thread and clear the bottom line (or leave ``final_msg``)."""
        if not self.active:
            return
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=self._interval * 3 + 1.0)
        self._thread = None
        # Clear the sticky line so the next print starts clean, or paint final.
        with self._paint_lock:
            if self._painting:
                self._clear_line_locked()
                if final_msg:
                    self.stream.write(final_msg + "\n")
                self.stream.flush()
                self._painting = False

    # ------------------------------------------------------- pause/resume (human)

    def pause(self) -> None:
        """Suppress painting and clear the bottom line.

        Call before the terminal belongs to the human (interactive prompt,
        direct print). The spinner thread stops touching the bottom line until
        ``resume()``. On pause the bottom line is erased so the human's prompt
        starts at column 0 on a clean line.
        """
        self._paused = True
        if not self.active:
            return
        with self._paint_lock:
            if self._painting:
                self._clear_line_locked()
                self._painting = False

    def resume(self) -> None:
        """Re-enable painting (reclaim the bottom line)."""
        self._paused = False

    # ------------------------------------------------------- scrolling output

    def flush_line(self, text: str) -> None:
        """Write a scrolling line above the spinner, coordinating the bottom line.

        When the spinner is active and painting: clear the bottom line, write
        ``text + \\n`` (scrolling it up), then let the spinner thread reclaim the
        new bottom line on its next frame. When inactive (non-TTY, stopped, or
        paused): a plain ``print(text)``. This is the route for ``self.out`` so a
        scrolling colored line never garbles the sticky spinner.
        """
        if not self.active:
            print(text)
            return
        with self._paint_lock:
            if self._painting:
                # Clear the sticky spinner line, then print the scrolling line
                # (which advances to a fresh line), leaving the bottom line free
                # for the spinner to reclaim next frame.
                self._clear_line_locked()
                self.stream.write(text + "\n")
                self.stream.flush()
                self._painting = False
            else:
                # Not currently painting (paused, or between frames): just print.
                # The spinner thread will reclaim when it next paints.
                self.stream.write(text + "\n")
                self.stream.flush()

    # ------------------------------------------------------- internals (thread)

    def _run(self) -> None:
        """The animation loop: repaint the bottom line every interval.

        Each iteration is wrapped so a single paint failure (e.g. a transient
        terminal-size or write error) doesn't kill the thread — killing it would
        make the spinner vanish permanently for the rest of the run (no resume).
        The daemon thread is the only animator; once dead, nothing repaints.
        """
        while not self._stop.is_set():
            try:
                if not self._paused:
                    self._paint_frame()
            except Exception:  # noqa: BLE001 - never let a frame kill animation
                pass
            self._stop.wait(self._interval)

    def _paint_frame(self) -> None:
        """Paint one spinner frame on the bottom line."""
        frame = _FRAMES[self._frame_idx % len(_FRAMES)]
        self._frame_idx += 1
        line = self._format(frame)
        with self._paint_lock:
            if self._paused or not self.active:
                return
            # Carriage return to column 0, erase the rest, write the frame line
            # WITHOUT a trailing newline (so it stays on the bottom line).
            self.stream.write("\r" + _ERASE_EOL + line)
            self.stream.flush()
            self._painting = True

    def _format(self, frame: str) -> str:
        """The full bottom-line text: blue spinner + status, truncated to width."""
        prefix = style(frame, BLUE, BOLD)
        msg = self._msg
        if not msg:
            return prefix
        text = f"{prefix} {msg}"
        # Truncate to terminal width so the line never wraps (wrapping would
        # break the in-place repaint). Account for ANSI escape bytes (invisible).
        width = self._get_width()
        if width > 0:
            visible = self._visible_len(text)
            if visible > width:
                # Truncate the message portion only; keep the spinner prefix.
                overflow = visible - width
                if len(msg) > overflow + 1:
                    msg = msg[: len(msg) - overflow - 1] + "…"
                    text = f"{prefix} {msg}"
        return text

    def _clear_line_locked(self) -> None:
        """Erase the current line (caller holds the paint lock)."""
        self.stream.write("\r" + _ERASE_EOL)
        self.stream.flush()

    @staticmethod
    def _visible_len(text: str) -> int:
        """Length of ``text`` excluding ANSI escape sequences (for truncation)."""
        import re

        return len(re.sub(r"\x1b\[[0-9;]*m", "", text))
