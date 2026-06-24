"""OpenAI-compatible LLM adapter (local llama-server, etc.).

Posts to ``{base_url}/chat/completions`` with ``response_format`` JSON mode
and parses the structured resolution. Uses only stdlib ``urllib`` so capybase
has no hard network dependency. On any HTTP/parse failure the adapter returns
a candidate with ``needs_human=True`` and a parse warning — it never raises
into the orchestrator, so a flaky model degrades to escalation, not a crash.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Protocol

from capybase.adapters.parsers import parse_resolution_json
from capybase.config import ModelConfig


class LLMResponse:
    """Normalized model response."""

    def __init__(self, text: str, raw: dict[str, Any] | None = None) -> None:
        self.text = text
        self.raw = raw


class LLMClient(Protocol):
    def complete(self, messages: list[dict[str, str]], *, model: str, temperature: float,
                 max_tokens: int, json_mode: bool) -> LLMResponse: ...


class OpenAICompatibleClient:
    def __init__(self, config: ModelConfig) -> None:
        self.config = config

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> LLMResponse:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            # Stream so a long generation keeps the socket active. Reasoning
            # models can take 60s+ to answer; a single blocking read on a flaky
            # link hits the kernel TCP timeout (ETIMEDOUT) mid-generation,
            # whereas streaming keeps the connection alive token-by-token.
            "stream": True,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        data = json.dumps(body).encode("utf-8")
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
            },
            method="POST",
        )
        try:
            return self._read_stream(req, url)
        except urllib.error.URLError as exc:
            raise RuntimeError(f"LLM request failed: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"LLM request failed: {exc}") from exc

    def _read_stream(self, req: urllib.request.Request, url: str) -> LLMResponse:
        """Consume an SSE stream, accumulate content, and capture finish_reason.

        We set the socket read timeout generously but rely on streaming to keep
        the connection live: each token chunk resets the idle window. Falls
        back to non-streaming if the server returns a non-SSE JSON body.
        """
        with urllib.request.urlopen(
            req, timeout=self.config.request_timeout_seconds
        ) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if "text/event-stream" not in ctype and "application/x-ndjson" not in ctype:
                # Server ignored stream=true and returned a single JSON object.
                raw = json.loads(resp.read().decode("utf-8"))
                return _from_non_stream(raw)
            content_parts: list[str] = []
            finish_reason: str | None = None
            raw_meta: dict[str, Any] = {}
            for line in resp:
                line = line.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    if "content" in delta and delta["content"]:
                        content_parts.append(delta["content"])
                    if choices[0].get("finish_reason"):
                        finish_reason = choices[0]["finish_reason"]
                raw_meta = chunk
        text = "".join(content_parts)
        raw_meta.setdefault("_accumulated", {"finish_reason": finish_reason})
        return LLMResponse(text=text, raw=raw_meta)


def _from_non_stream(raw: dict[str, Any]) -> LLMResponse:
    try:
        text = raw["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"unexpected LLM response shape: {exc}") from exc
    return LLMResponse(text=text, raw=raw)


def coerce_candidate_dict(raw_text: str) -> tuple[dict, list[str]]:
    """Parse + lightly normalize the model's JSON into candidate fields."""
    data, warnings = parse_resolution_json(raw_text)
    if not data:
        return data, warnings
    # Normalize common alternate spellings.
    aliases = {
        "resolved": "resolved_text",
        "resolution": "resolved_text",
        "merged": "resolved_text",
        "confidence": "self_reported_confidence",
        "needsHuman": "needs_human",
        "preserved_current": "preserved_current_side",
        "preserved_replayed": "preserved_replayed_commit_side",
    }
    for src, dst in aliases.items():
        if src in data and dst not in data:
            data[dst] = data[src]
    return data, warnings
