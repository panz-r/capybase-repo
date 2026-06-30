"""Tests for the progress-line spinner.

The spinner manages the bottom terminal line via a daemon thread. These exercise
its state machine (start/pause/resume/stop), frame rotation, the flush_line
coordination primitive, the non-TTY no-op, and message truncation — without
needing a real terminal (isatty/get_width are injected).
"""

from __future__ import annotations

import io
import re
import time

import pytest

from capybase.spinner import Spinner, _FRAMES


def _capture_spinner(*, isatty: bool = True, width: int = 80) -> tuple[Spinner, list]:
    """A Spinner writing to a capture buffer with injected TTY/width.

    ``writes`` collects every raw write to the stream so tests can inspect the
    exact escape sequences and frame sequence.
    """
    writes: list[str] = []

    class _Stream:
        def write(self, s: str) -> int:
            writes.append(s)
            return len(s)

        def flush(self) -> None:
            pass

    s = Spinner(
        _Stream(), isatty=lambda: isatty, get_width=lambda: width
    )
    return s, writes


def _join(writes: list[str]) -> str:
    return "".join(writes)


# ---------------------------------------------------------------------------
# Non-TTY: inert (the test safety guarantee)
# ---------------------------------------------------------------------------


def test_non_tty_start_is_noop():
    """A non-TTY stream never starts the thread; the spinner is inert."""
    s, writes = _capture_spinner(isatty=False)
    s.start("working")
    assert not s.active
    assert writes == []


def test_non_tty_flush_line_uses_plain_print(capsys):
    """flush_line falls back to plain print() when inactive (non-TTY)."""
    s = Spinner(io.StringIO(), isatty=lambda: False)
    s.flush_line("a scrolling line")
    captured = capsys.readouterr()
    assert "a scrolling line" in captured.out


# ---------------------------------------------------------------------------
# Frame rotation
# ---------------------------------------------------------------------------


def test_spinner_paints_multiple_frames_over_time():
    """Over a few intervals the painted frames advance through the rotation."""
    s, writes = _capture_spinner()
    s.start("working")
    # Let it paint ~5 frames at 10fps (interval 0.1s).
    time.sleep(0.55)
    s.stop()
    text = _join(writes)
    # At least two distinct frames were painted (animation is advancing).
    frames_painted = [f for f in _FRAMES if f in text]
    assert len(frames_painted) >= 2


def test_spinner_message_is_on_the_line():
    """The painted line carries the status message alongside the spinner."""
    s, writes = _capture_spinner()
    s.start("resolving conflicts")
    time.sleep(0.15)
    s.stop()
    text = _join(writes)
    assert "resolving conflicts" in text


def test_set_updates_message():
    """set() updates the status; subsequent frames carry the new message."""
    s, writes = _capture_spinner()
    s.start("first")
    time.sleep(0.15)
    s.set("second")
    time.sleep(0.15)
    s.stop()
    text = _join(writes)
    assert "second" in text


# ---------------------------------------------------------------------------
# pause/resume
# ---------------------------------------------------------------------------


def test_pause_clears_bottom_line_and_suppresses():
    """pause() clears the bottom line and stops painting until resume()."""
    s, writes = _capture_spinner()
    s.start("working")
    time.sleep(0.2)  # paint a few frames
    s.pause()
    # Drain any frame in flight at pause time, then snapshot the write count.
    time.sleep(0.35)
    after_pause = len(writes)
    # While paused, no new frames should be written. Wait several intervals.
    time.sleep(0.4)
    during_pause = len(writes) - after_pause
    # Now resume and confirm painting restarts (sanity).
    s.resume()
    time.sleep(0.2)
    after_resume = len(writes)
    s.stop()
    # While paused, no frames were written.
    assert during_pause == 0, (
        f"{during_pause} frames painted while paused — pause() must suppress"
    )
    # After resume, frames resumed (the spinner recovered).
    assert after_resume > after_pause, "resume() should restart painting"
    # A clear (\r + erase) was emitted by pause().
    text = _join(writes)
    assert "\r\x1b[K" in text


def test_resume_restarts_painting():
    s, writes = _capture_spinner()
    s.start("working")
    time.sleep(0.1)
    s.pause()
    time.sleep(0.2)
    before_resume = len(writes)
    s.resume()
    time.sleep(0.2)
    s.stop()
    # Frames were painted after resume (len grew).
    assert len(writes) > before_resume


# ---------------------------------------------------------------------------
# flush_line coordination
# ---------------------------------------------------------------------------


def test_flush_line_clears_then_prints_when_painting():
    """When the spinner is painting, flush_line clears the bottom line first,
    then writes the scrolling line (so they never garble)."""
    s, writes = _capture_spinner()
    s.start("working")
    time.sleep(0.15)  # ensure painting
    s.flush_line("a scrolling line")
    s.stop()
    # The scrolling line is present, preceded by a clear.
    text = _join(writes)
    assert "a scrolling line\n" in text
    assert "\r\x1b[K" in text


def test_flush_line_works_when_paused():
    """flush_line during pause just prints (no clear needed; not painting)."""
    s, writes = _capture_spinner()
    s.start("working")
    time.sleep(0.1)
    s.pause()
    s.flush_line("scrolled while paused")
    s.stop()
    text = _join(writes)
    assert "scrolled while paused\n" in text


# ---------------------------------------------------------------------------
# stop / cleanup
# ---------------------------------------------------------------------------


def test_stop_clears_bottom_line():
    s, writes = _capture_spinner()
    s.start("working")
    time.sleep(0.15)
    s.stop()
    # After stop, a clear was emitted (the bottom line is clean).
    text = _join(writes)
    assert "\r\x1b[K" in text


def test_stop_final_msg_printed():
    """stop(final_msg) leaves a final message after clearing the bottom line."""
    s, writes = _capture_spinner()
    s.start("working")
    time.sleep(0.15)
    s.stop(final_msg="done")
    text = _join(writes)
    assert text.rstrip().endswith("done")


def test_stop_is_idempotent():
    s, writes = _capture_spinner()
    s.start("working")
    time.sleep(0.1)
    s.stop()
    s.stop()  # second stop is a safe no-op
    assert not s.active


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


def test_long_message_truncated_to_width():
    """A message longer than the terminal width is truncated with … so the line
    never wraps (wrapping would break the in-place repaint)."""
    s, writes = _capture_spinner(width=30)
    s.start("x" * 200)
    time.sleep(0.15)
    s.stop()
    text = _join(writes)
    # Find a painted frame line (starts with \r). Strip BOTH SGR and erase CSI.
    line = next((p for p in text.split("\r") if p.strip()), "")
    visible = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line)  # strip all CSI (SGR + erase)
    assert "…" in visible
    assert len(visible.rstrip()) <= 30


def test_spinner_thread_survives_paint_exception():
    """A transient paint failure (e.g. get_terminal_size error) must NOT kill the
    thread — if it did, the spinner would vanish permanently and never resume
    after a pause (the reported bug: no progress line after a manual edit)."""
    call_count = {"n": 0}

    def boom_then_width():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError("transient terminal error")
        return 80  # subsequent calls succeed

    writes: list[str] = []

    class _Stream:
        def write(self, s: str) -> int:
            writes.append(s)
            return len(s)

        def flush(self) -> None:
            pass

    s = Spinner(_Stream(), isatty=lambda: True, get_width=boom_then_width)
    s.start("working")
    # Let several frames attempt (first throws, rest should paint).
    time.sleep(0.45)
    s.stop()
    text = _join(writes)
    # Despite the first-frame exception, frames DID paint after it (the thread
    # survived and recovered — animation isn't permanently dead).
    frames_painted = sum(1 for f in _FRAMES if f in text)
    assert frames_painted >= 1, "spinner thread died on a paint exception"
