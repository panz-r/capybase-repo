"""Tests for the OpenAI-compatible adapter, including SSE streaming.

The streaming path is exercised with a fake urllib that yields SSE data lines,
simulating how llama-server emits token-by-token chunks. This locks in the
behavior that keeps long generations alive on flaky links.
"""

from __future__ import annotations

import io
import json

import pytest

from capybase.adapters.llm_openai import OpenAICompatibleClient
from capybase.config import ModelConfig


def _cfg() -> ModelConfig:
    return ModelConfig(base_url="http://x/v1", model="m")


class FakeResp:
    """Mimics an http.client.HTTPResponse for SSE."""

    def __init__(self, lines: list[bytes], content_type: str = "text/event-stream"):
        self._buf = io.BytesIO(b"".join(lines))
        self.headers = {"Content-Type": content_type}

    def __iter__(self):
        # iterate line by line, preserving trailing newlines
        for line in self._buf:
            yield line

    def read(self) -> bytes:
        return self._buf.getvalue()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _sse_chunks(
    text: str,
    finish: str = "stop",
    logprobs: list[dict] | None = None,
) -> list[bytes]:
    """Build SSE data lines that stream `text` char-by-char then a finish.

    ``logprobs``, when given, is a list of per-token logprob entry dicts aligned
    to the content slices (each entry: ``{"token": ..., "logprob": ...}``). The
    delta then carries ``logprobs.content`` in the OpenAI streaming shape, which
    the adapter reduces to mean token-entropy.
    """
    lines: list[bytes] = []
    # Send content in small slices to simulate token streaming.
    step = 4
    for i in range(0, len(text), step):
        delta: dict = {"content": text[i : i + step]}
        if logprobs is not None:
            # Pair each slice with its logprob entry (cycle if fewer entries).
            entry = logprobs[(i // step) % len(logprobs)]
            delta["logprobs"] = {"content": [entry]}
        lines.append(b"data: " + json.dumps({"choices": [{"delta": delta}]}).encode() + b"\n")
    lines.append(
        b"data: "
        + json.dumps({"choices": [{"delta": {}, "finish_reason": finish}]}).encode()
        + b"\n"
    )
    lines.append(b"data: [DONE]\n")
    return lines


def _patch_urlopen(monkeypatch, resp: FakeResp):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["timeout"] = timeout
        captured["url"] = req.full_url
        return resp

    monkeypatch.setattr("capybase.adapters.llm_openai.urllib.request.urlopen", fake_urlopen)
    return captured


def test_streaming_accumulates_content(monkeypatch):
    payload = '{"resolved_text": "    return 1", "needs_human": false}'
    cap = _patch_urlopen(monkeypatch, FakeResp(_sse_chunks(payload)))
    client = OpenAICompatibleClient(_cfg())
    resp = client.complete(
        [{"role": "user", "content": "hi"}],
        model="m", temperature=0.2, max_tokens=8192, json_mode=True,
    )
    assert resp.text == payload
    assert cap["timeout"] == 600  # default request_timeout_seconds


def test_streaming_records_finish_reason(monkeypatch):
    cap = _patch_urlopen(monkeypatch, FakeResp(_sse_chunks("abc", finish="length")))
    client = OpenAICompatibleClient(_cfg())
    resp = client.complete(
        [{"role": "user", "content": "hi"}],
        model="m", temperature=0.2, max_tokens=8192, json_mode=True,
    )
    assert resp.text == "abc"
    assert resp.raw["_accumulated"]["finish_reason"] == "length"


def test_non_stream_fallback(monkeypatch):
    """If the server ignores stream=true, fall back to single JSON object."""
    raw = {"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}]}
    resp_obj = FakeResp(
        [json.dumps(raw).encode()],
        content_type="application/json",
    )
    _patch_urlopen(monkeypatch, resp_obj)
    client = OpenAICompatibleClient(_cfg())
    resp = client.complete(
        [{"role": "user", "content": "hi"}],
        model="m", temperature=0.2, max_tokens=8192, json_mode=True,
    )
    assert resp.text == "hello"


def test_urlopen_error_becomes_runtime_error(monkeypatch):
    import urllib.error

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("boom")

    monkeypatch.setattr("capybase.adapters.llm_openai.urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatibleClient(_cfg())
    with pytest.raises(RuntimeError, match="LLM request failed"):
        client.complete(
            [{"role": "user", "content": "hi"}],
            model="m", temperature=0.2, max_tokens=8192, json_mode=True,
        )


def test_early_termination_on_complete_answer(monkeypatch):
    """Once a complete ```json block arrives, stop reading even if more would come."""
    payload = '{"resolved_text": "    return 1", "needs_human": false}'
    babble = "This is trailing prose the model emits after answering. " * 50
    # Build SSE chunks: the JSON block (with closing fence), then babble.
    json_lines = _sse_chunks(payload)
    babble_lines = _sse_chunks(babble, finish="stop")
    resp = FakeResp(json_lines + babble_lines)
    _patch_urlopen(monkeypatch, resp)
    client = OpenAICompatibleClient(_cfg())
    out = client.complete(
        [{"role": "user", "content": "hi"}],
        model="m", temperature=0.2, max_tokens=8192, json_mode=True,
    )
    # The answer is present...
    assert json.loads(out.text)["resolved_text"] == "    return 1"
    # ...and early termination fired (no babble read, finish_reason absent).
    assert out.raw["_accumulated"]["early_terminated"] is True
    assert babble not in out.text


def test_no_early_termination_without_complete_answer(monkeypatch):
    """No fenced JSON -> keep reading until [DONE]."""
    lines = _sse_chunks("just thinking, no json yet", finish="stop")
    resp = FakeResp(lines)
    _patch_urlopen(monkeypatch, resp)
    client = OpenAICompatibleClient(_cfg())
    out = client.complete(
        [{"role": "user", "content": "hi"}],
        model="m", temperature=0.2, max_tokens=8192, json_mode=True,
    )
    assert out.raw["_accumulated"]["early_terminated"] is False
    assert out.raw["_accumulated"]["finish_reason"] == "stop"


# ---------------------------------------------------------------------------
# TECP token-entropy capture (survey §4.1): the adapter reduces the API's
# per-token logprobs to a scalar mean token-entropy (mean negative log-prob).
# ---------------------------------------------------------------------------


def _entropy_cfg() -> ModelConfig:
    cfg = ModelConfig(base_url="http://x/v1", model="m")
    cfg.capture_token_entropy = True
    return cfg


def test_entropy_captured_as_mean_negative_logprob(monkeypatch):
    """When capture_token_entropy is on, logprobs in the stream reduce to the
    mean of -logprob over the emitted tokens."""
    payload = '{"resolved_text": "    return 1", "needs_human": false}'
    # Two logprob entries: -0.5 (NLL 0.5) and -1.5 (NLL 1.5) → mean NLL 1.0.
    lps = [{"token": "a", "logprob": -0.5}, {"token": "b", "logprob": -1.5}]
    cap = _patch_urlopen(monkeypatch, FakeResp(_sse_chunks(payload, logprobs=lps)))
    client = OpenAICompatibleClient(_entropy_cfg())
    resp = client.complete(
        [{"role": "user", "content": "hi"}],
        model="m", temperature=0.2, max_tokens=8192, json_mode=True,
    )
    assert resp.text == payload
    assert resp.mean_token_entropy is not None
    assert abs(resp.mean_token_entropy - 1.0) < 1e-6


def test_entropy_none_when_flag_off(monkeypatch):
    """capture_token_entropy off → adapter never requests/reads logprobs, so
    mean_token_entropy stays None (the default, zero-cost path)."""
    payload = '{"resolved_text": "    return 1", "needs_human": false}'
    lps = [{"token": "a", "logprob": -0.5}, {"token": "b", "logprob": -1.5}]
    _patch_urlopen(monkeypatch, FakeResp(_sse_chunks(payload, logprobs=lps)))
    client = OpenAICompatibleClient(_cfg())  # flag off
    resp = client.complete(
        [{"role": "user", "content": "hi"}],
        model="m", temperature=0.2, max_tokens=8192, json_mode=True,
    )
    assert resp.mean_token_entropy is None


def test_entropy_none_when_stream_has_no_logprobs(monkeypatch):
    """Even with the flag on, a server that emits no logprobs yields None — the
    system degrades gracefully rather than crashing."""
    payload = '{"resolved_text": "    return 1", "needs_human": false}'
    _patch_urlopen(monkeypatch, FakeResp(_sse_chunks(payload)))  # no logprobs
    client = OpenAICompatibleClient(_entropy_cfg())
    resp = client.complete(
        [{"role": "user", "content": "hi"}],
        model="m", temperature=0.2, max_tokens=8192, json_mode=True,
    )
    assert resp.mean_token_entropy is None


def test_request_body_omits_logprobs_when_flag_off(monkeypatch):
    """The logprobs keys are absent from the request body unless opted in — so
    deployments that don't capture entropy see an unchanged request shape."""
    import urllib.request as ur

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResp(_sse_chunks('{"resolved_text": "x", "needs_human": false}'))

    monkeypatch.setattr("capybase.adapters.llm_openai.urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatibleClient(_cfg())
    client.complete(
        [{"role": "user", "content": "hi"}],
        model="m", temperature=0.2, max_tokens=8192, json_mode=True,
    )
    assert "logprobs" not in captured["body"]
    assert "top_logprobs" not in captured["body"]


def test_request_body_includes_logprobs_when_flag_on(monkeypatch):
    """Opting in adds the logprobs keys to the request body."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResp(_sse_chunks('{"resolved_text": "x", "needs_human": false}'))

    monkeypatch.setattr("capybase.adapters.llm_openai.urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatibleClient(_entropy_cfg())
    client.complete(
        [{"role": "user", "content": "hi"}],
        model="m", temperature=0.2, max_tokens=8192, json_mode=True,
    )
    assert captured["body"]["logprobs"] is True
    assert captured["body"]["top_logprobs"] == 1


def test_request_body_omits_response_format_when_json_mode_off(monkeypatch):
    """``json_mode=False`` (set by ``capybase calibrate`` when a server rejects
    ``response_format``) must drop the key from the body so resolution can fall
    back to the fenced-JSON parser."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResp(_sse_chunks('{"resolved_text": "x", "needs_human": false}'))

    monkeypatch.setattr("capybase.adapters.llm_openai.urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatibleClient(_cfg())
    client.complete(
        [{"role": "user", "content": "hi"}],
        model="m", temperature=0.2, max_tokens=8192, json_mode=False,
    )
    assert "response_format" not in captured["body"]


def test_non_stream_many_carries_per_choice_entropy(monkeypatch):
    """The server-side-n (complete_many) non-streaming path surfaces logprobs
    per choice via choices[i].logprobs.content[]."""
    raw = {
        "choices": [
            {
                "message": {"content": "first"},
                "logprobs": {"content": [{"token": "f", "logprob": -0.2}]},
                "finish_reason": "stop",
            },
            {
                "message": {"content": "second"},
                # No logprobs on this choice → None.
                "finish_reason": "stop",
            },
        ]
    }
    resp_obj = FakeResp(
        [json.dumps(raw).encode()],
        content_type="application/json",
    )
    _patch_urlopen(monkeypatch, resp_obj)
    client = OpenAICompatibleClient(_entropy_cfg())
    out = client.complete_many(
        [{"role": "user", "content": "hi"}],
        model="m", temperature=0.2, max_tokens=8192, json_mode=True, n=2,
    )
    assert len(out) == 2
    assert out[0].text == "first"
    assert abs(out[0].mean_token_entropy - 0.2) < 1e-6
    assert out[1].text == "second"
    assert out[1].mean_token_entropy is None
