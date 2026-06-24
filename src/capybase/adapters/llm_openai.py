"""OpenAI-compatible LLM adapter (local llama-server, etc.).

Posts to ``{base_url}/chat/completions`` with ``response_format`` JSON mode
and parses the structured resolution. Uses only stdlib ``urllib`` so capybase
has no hard network dependency. On any HTTP/parse failure the adapter returns
a candidate with ``needs_human=True`` and a parse warning — it never raises
into the orchestrator, so a flaky model degrades to escalation, not a crash.
"""

from __future__ import annotations

import json
import socket
import time
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
        # Run the blocking stream read in a daemon thread with a HARD join
        # deadline. This is the only fully robust guard against a stalled
        # socket: a connection can block inside urlopen()/header-read in a
        # half-open state where neither socket.timeout nor our in-loop deadline
        # fires. The thread is abandoned (daemon) if it doesn't return in time;
        # the partial connection leaks but the orchestrator never hangs.
        import queue
        import threading

        result_q: queue.Queue = queue.Queue()

        def _worker():
            try:
                result_q.put(self._read_stream(req, url))
            except Exception as exc:  # noqa: BLE001
                result_q.put(exc)

        worker = threading.Thread(target=_worker, daemon=True)
        worker.start()
        try:
            got = result_q.get(timeout=self.config.generation_timeout_seconds)
        except queue.Empty:
            raise RuntimeError(
                f"LLM request exceeded hard deadline "
                f"({self.config.generation_timeout_seconds}s); aborting stalled connection"
            )
        if isinstance(got, Exception):
            if isinstance(got, urllib.error.URLError):
                raise RuntimeError(f"LLM request failed: {got}") from got
            raise RuntimeError(f"LLM request failed: {got}") from got
        return got

    def _read_stream(self, req: urllib.request.Request, url: str) -> LLMResponse:
        """Consume an SSE stream, accumulate content, and capture finish_reason.

        Behaviour:

        - **Early termination.** As soon as the accumulated text contains a
          complete, parseable ```json fenced block, we stop reading and close
          the connection. Reasoning models often babble on *after* emitting
          their answer; reading to the end wastes time and, over a flaky link,
          pushes the connection past a middlebox idle/duration limit. Closing
          early shortens the request from ~80s to the moment the answer lands.
        - **Timeouts.** A per-read socket timeout (``request_timeout_seconds``)
          and a hard wall-clock deadline (``generation_timeout_seconds``) guard
          against stalls. The deadline runs in the worker thread that wraps
          this method.

        Falls back to non-streaming if the server returns a non-SSE JSON body.
        """
        deadline = time.monotonic() + self.config.generation_timeout_seconds
        try:
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
                early_stop = False
                for line in resp:
                    if time.monotonic() > deadline:
                        raise RuntimeError(
                            "LLM generation exceeded total deadline "
                            f"({self.config.generation_timeout_seconds}s) without finishing"
                        )
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
                    # Early termination: as soon as a complete fenced JSON
                    # answer is present, stop reading. Closing the response
                    # context manager aborts the underlying connection.
                    if _has_complete_answer("".join(content_parts)):
                        early_stop = True
                        break
        except socket.timeout as exc:
            raise RuntimeError(
                f"LLM request failed: socket read timed out after "
                f"{self.config.request_timeout_seconds}s"
            ) from exc
        text = "".join(content_parts)
        meta = {"finish_reason": finish_reason, "early_terminated": early_stop}
        raw_meta.setdefault("_accumulated", meta)
        return LLMResponse(text=text, raw=raw_meta)


def _has_complete_answer(accumulated: str) -> bool:
    """True once ``accumulated`` contains a complete, parseable answer.

    Accepts either a fenced ```json block or bare JSON, as long as it parses
    to a dict with a ``resolved_text`` key. Partial JSON (still streaming)
    fails to parse and returns False, so this is safe to call on every chunk.
    The signal means the model has emitted its final answer and any further
    output is babble we can discard — letting the adapter close the
    connection immediately rather than reading trailing prose.
    """
    data, _ = parse_resolution_json(accumulated)
    return bool(data) and "resolved_text" in data


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
