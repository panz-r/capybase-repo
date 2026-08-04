"""Tests for the LLM transport retry layer (_with_retry + _is_retryable).

The retry sits BELOW the application-level CEGIS re-prompt loop: a single
generation gets up to ``retry_attempts`` transport retries on transient failures
(connection reset, socket timeout, HTTP 5xx, timed-out reads), then CEGIS takes
over. It must NOT retry on HTTP 4xx (caller errors), the "unexpected response
shape" error (a retry fails identically), or the hard-deadline stall — when the
model itself stalled for the full generation budget on this exact prompt, a
retry on the same prompt stalls again, so retrying just burns
``generation_timeout_seconds`` per attempt (3 x 240s = 720s in the eval config)
and stacks toward the case timeout.
"""

from __future__ import annotations

import socket
import urllib.error
from unittest.mock import patch

import pytest

from capybase.adapters import llm_openai
from capybase.adapters.llm_openai import _is_retryable, _with_retry


def _runtime(cause: BaseException | None, msg: str = "LLM request failed: x") -> RuntimeError:
    exc = RuntimeError(msg)
    if cause is not None:
        exc.__cause__ = cause
    return exc


# ---------------------------------------------------------------------------
# Retryability classification.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exc", [
    _runtime(urllib.error.HTTPError("u", 500, "ISE", {}, None)),   # 5xx
    _runtime(urllib.error.HTTPError("u", 503, "unavail", {}, None)),
    _runtime(urllib.error.URLError("refused")),                     # connection
    _runtime(socket.timeout("timed out")),                          # socket timeout
    _runtime(None, "LLM request failed: socket read timed out after 600s"),
])
def test_is_retryable_transient(exc):
    assert _is_retryable(exc) is True


@pytest.mark.parametrize("exc", [
    _runtime(urllib.error.HTTPError("u", 400, "Bad Request", {}, None)),   # 4xx
    _runtime(urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)),
    _runtime(urllib.error.HTTPError("u", 422, "Unprocessable", {}, None)),
    _runtime(None, "unexpected LLM response shape: KeyError('choices')"),  # malformed
    # Hard-deadline stall: the model itself stalled for the full generation
    # budget on this prompt — retrying the same prompt stalls again.
    _runtime(None, "LLM request exceeded hard deadline (180s); aborting stalled connection"),
])
def test_is_not_retryable(exc):
    assert _is_retryable(exc) is False


# ---------------------------------------------------------------------------
# Retry behaviour: transient-then-ok, exhaustive, no-retry-on-non-retryable.
# ---------------------------------------------------------------------------


def test_retry_then_success():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _runtime(urllib.error.HTTPError("u", 500, "ISE", {}, None))
        return "ok"

    with patch("capybase.adapters.llm_openai.time.sleep") as sleep:
        res = _with_retry(fn, attempts=3, base_delay=1.0, max_delay=5.0)
    assert res == "ok"
    assert calls["n"] == 3
    assert sleep.call_count == 2  # two backoffs before the successful 3rd try


def test_retry_exhausts_then_raises():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _runtime(urllib.error.URLError("down"))

    with patch("capybase.adapters.llm_openai.time.sleep"):
        with pytest.raises(RuntimeError):
            _with_retry(fn, attempts=3, base_delay=1.0, max_delay=5.0)
    assert calls["n"] == 3


def test_no_retry_on_non_retryable():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _runtime(None, "unexpected LLM response shape: boom")

    with pytest.raises(RuntimeError):
        _with_retry(fn, attempts=3, base_delay=1.0, max_delay=5.0)
    assert calls["n"] == 1  # immediate raise, no retries


def test_no_retry_on_hard_deadline_stall():
    """A hard-deadline stall (model stalled the full budget on this prompt) must
    NOT be retried — the same prompt stalls again, so each retry just burns
    another generation_timeout_seconds. Fail fast after exactly one attempt."""
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _runtime(
            None, "LLM request exceeded hard deadline (240s); aborting stalled connection"
        )

    with pytest.raises(RuntimeError):
        _with_retry(fn, attempts=3, base_delay=1.0, max_delay=5.0)
    assert calls["n"] == 1  # immediate raise, no retries


def test_retry_attempts_one_means_no_retry():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _runtime(urllib.error.URLError("down"))

    with pytest.raises(RuntimeError):
        _with_retry(fn, attempts=1, base_delay=1.0, max_delay=5.0)
    assert calls["n"] == 1


def test_retry_backoff_bounded_by_max_delay():
    """Backoff delays never exceed max_delay, even after many attempts."""
    delays: list[float] = []

    def fake_sleep(d):
        delays.append(d)

    def fn():
        raise _runtime(urllib.error.URLError("down"))

    with patch("capybase.adapters.llm_openai.time.sleep", fake_sleep):
        with patch("capybase.adapters.llm_openai.random.uniform", lambda lo, hi: hi):
            with pytest.raises(RuntimeError):
                _with_retry(fn, attempts=5, base_delay=2.0, max_delay=5.0)
    # With jitter disabled (uniform returns hi == the computed cap), each delay
    # is min(max_delay=5, base_delay*2**i) → 2, 4, 5, 5.
    assert delays == [2.0, 4.0, 5.0, 5.0], delays
