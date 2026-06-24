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


def _sse_chunks(text: str, finish: str = "stop") -> list[bytes]:
    """Build SSE data lines that stream `text` char-by-char then a finish."""
    lines: list[bytes] = []
    # Send content in small slices to simulate token streaming.
    step = 4
    for i in range(0, len(text), step):
        delta = {"choices": [{"delta": {"content": text[i : i + step]}}]}
        lines.append(b"data: " + json.dumps(delta).encode() + b"\n")
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
