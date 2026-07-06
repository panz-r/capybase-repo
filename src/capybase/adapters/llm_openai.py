"""OpenAI-compatible LLM adapter (local llama-server, etc.).

Posts to ``{base_url}/chat/completions`` with ``response_format`` JSON mode
and parses the structured resolution. Uses only stdlib ``urllib`` so capybase
has no hard network dependency. On any HTTP/parse failure the adapter returns
a candidate with ``needs_human=True`` and a parse warning — it never raises
into the orchestrator, so a flaky model degrades to escalation, not a crash.
"""

from __future__ import annotations

import json
import logging
import random
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Protocol, TypeVar

from capybase.adapters.parsers import parse_resolution_json
from capybase.config import ModelConfig

_log = logging.getLogger("capybase.llm")

T = TypeVar("T")


def _is_retryable(exc: BaseException) -> bool:
    """Should this raised exception trigger a transport retry?

    Retryable (transient): connection errors, socket timeouts, HTTP 5xx (server
    errors / overloaded / gateway timeouts), and the adapter's own
    stalled-connection / timed-out RuntimeErrors. NOT retryable: HTTP 4xx
    (caller errors — a retry fails identically), and the "unexpected response
    shape" RuntimeError (malformed non-error response — not a transport fault).
    """
    # Inspect the cause chain: the worker wraps the original network exception
    # in a RuntimeError via `raise RuntimeError(...) from got`, so the real
    # HTTPError/URLError/socket.timeout is on __cause__.
    chain: list[BaseException] = [exc]
    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        chain.append(cause)

    for e in chain:
        # HTTP 5xx is retryable; HTTP 4xx is not.
        if isinstance(e, urllib.error.HTTPError):
            return e.code >= 500
        # Connection refused, DNS failure, reset, etc.
        if isinstance(e, urllib.error.URLError):
            return True
        # Per-read socket timeout.
        if isinstance(e, socket.timeout):
            return True

    # Fall back to the adapter's own RuntimeError classification. The worker
    # raises these for stalled connections and timed-out reads (retryable) but
    # also for malformed responses (not retryable).
    if isinstance(exc, RuntimeError):
        msg = str(exc).lower()
        if "unexpected llm response shape" in msg:
            return False  # malformed non-error response — a retry won't help
        if (
            "request failed" in msg
            or "exceeded hard deadline" in msg
            or "timed out" in msg
        ):
            return True
    return False


def _is_response_format_400(exc: BaseException) -> bool:
    """True when an HTTP 400 rejection is specifically about ``response_format``.

    Some servers (LM Studio) reject ``response_format: {"type": "json_object"}``
    for models that only accept ``json_schema`` or ``text``, returning a 400
    whose body names ``response_format``. The worker wraps the underlying
    :class:`HTTPError` in a ``RuntimeError`` (via ``raise ... from got``), so we
    inspect the cause chain AND the message for the 400 + response_format signal.
    """
    chain: list[BaseException] = [exc]
    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        chain.append(cause)
    for e in chain:
        if isinstance(e, urllib.error.HTTPError) and e.code == 400:
            try:
                detail = e.read().decode("utf-8", errors="replace").lower()
            except Exception:  # noqa: BLE001
                detail = ""
            if "response_format" in detail:
                return True
        # Also match the wrapped message (some paths lose the HTTPError body).
        if isinstance(e, RuntimeError):
            msg = str(e).lower()
            if "400" in msg and "response_format" in msg:
                return True
    return False


def _with_retry(
    fn: Callable[[], T],
    *,
    attempts: int,
    base_delay: float,
    max_delay: float,
) -> T:
    """Call ``fn`` with exponential-backoff retries on transient failures.

    ``attempts`` is the total number of tries (1 = no retries). Between attempts
    we sleep ``random.uniform(0, min(max_delay, base_delay * 2**i))`` — full
    jitter, which spreads concurrent retries and bounds the worst-case delay by
    ``max_delay``. A non-retryable exception propagates immediately on the first
    occurrence; a retryable one is retried up to ``attempts`` times, after which
    the final exception propagates.

    :class:`Interrupted` (raised by the rebase/dry-run SIGTERM handler) is a
    BaseException, so it bypasses the ``except Exception`` here and propagates
    immediately — an interrupt must never be retried or swallowed.
    """
    last_exc: BaseException | None = None
    for i in range(max(1, attempts)):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - classified below
            last_exc = exc
            if not _is_retryable(exc):
                raise
            remaining = attempts - i - 1
            if remaining <= 0:
                _log.warning("LLM call failed after %d attempt(s): %s", attempts, exc)
                raise
            delay = random.uniform(0, min(max_delay, base_delay * (2 ** i)))
            _log.info(
                "LLM call failed (attempt %d/%d), retrying in %.1fs: %s",
                i + 1, attempts, delay, exc,
            )
            time.sleep(delay)
    assert last_exc is not None  # unreachable: loop runs >=1 and either returns or raises
    raise last_exc


class Interrupted(BaseException):
    """Raised when capybase is interrupted by a terminate signal (SIGTERM/SIGHUP)
    during a rebase/dry-run. Subclasses BaseException (NOT Exception) so the LLM
    retry wrapper's ``except Exception`` can't swallow it — an interrupt must
    propagate to the rebase path's abort handler immediately."""


class LLMResponse:
    """Normalized model response.

    ``mean_token_entropy`` is the logit-free, black-box uncertainty signal
    (survey §4.1 TECP): the mean negative log-probability over the generated
    content tokens, reduced from per-token logprobs the API emits in each SSE
    delta. It is ``None`` when the API returned no logprobs (the default when
    ``capture_token_entropy`` is off, or the server doesn't support them).
    """

    def __init__(
        self,
        text: str,
        raw: dict[str, Any] | None = None,
        mean_token_entropy: float | None = None,
    ) -> None:
        self.text = text
        self.raw = raw
        self.mean_token_entropy = mean_token_entropy


class LLMClient(Protocol):
    def complete(self, messages: list[dict[str, str]], *, model: str, temperature: float,
                 max_tokens: int, json_mode: bool) -> LLMResponse: ...

    def complete_many(self, messages: list[dict[str, str]], *, model: str, temperature: float,
                      max_tokens: int, json_mode: bool, n: int) -> list[LLMResponse]: ...


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
        try:
            return self._complete_with_json_mode(
                messages, model=model, temperature=temperature,
                max_tokens=max_tokens, json_mode=json_mode,
            )
        except RuntimeError as exc:
            # Some servers reject ``response_format: {"type": "json_object"}``
            # with HTTP 400 (e.g. LM Studio requires ``json_schema`` or ``text``
            # for certain models). That's a permanent per-request shape error,
            # not a transient fault — retrying identically won't help. Fall back
            # to a plain (text-mode) request: the tolerant JSON parser
            # (parse_resolution_json) handles prose-prefixed / fenced JSON, so
            # dropping server-side JSON constraining is safe. Only do this when
            # the failure looks like the response_format 400 (not a generic error).
            if json_mode and _is_response_format_400(exc):
                return self._complete_with_json_mode(
                    messages, model=model, temperature=temperature,
                    max_tokens=max_tokens, json_mode=False,
                )
            raise

    def _complete_with_json_mode(
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
        _maybe_request_logprobs(body, self.config)
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
        #
        # The whole attempt (build request → launch worker → fetch result) is
        # wrapped in _with_retry so a transient failure (connection reset,
        # 5xx, hard-deadline stall) re-runs with a fresh request and worker
        # thread. Each retry is a brand-new connection; a stalled daemon thread
        # from a failed attempt is simply abandoned.
        return _with_retry(
            lambda: self._attempt(req, url, self._read_stream),
            attempts=self.config.retry_attempts,
            base_delay=self.config.retry_base_delay_seconds,
            max_delay=self.config.retry_max_delay_seconds,
        )

    def _attempt(
        self,
        req: urllib.request.Request,
        url: str,
        reader: Callable[[urllib.request.Request, str], Any],
    ) -> Any:
        """One generation attempt: launch a daemon-thread reader with a hard
        deadline and re-raise its result/exception. Shared by complete and
        complete_many so both get identical retry semantics.

        ``reader`` is ``_read_stream`` (single) or ``_read_many`` (batch); both
        take ``(req, url)``. The wall-clock deadline
        (``generation_timeout_seconds``) caps this single attempt; the caller's
        :func:`_with_retry` adds retries on top, each as a fresh connection.
        """
        import queue
        import threading

        result_q: queue.Queue = queue.Queue()

        def _worker() -> None:
            try:
                result_q.put(reader(req, url))
            except Exception as exc:  # noqa: BLE001 - tunnel to main thread
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
            raise RuntimeError(f"LLM request failed: {got}") from got
        return got

    def complete_many(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
        n: int,
    ) -> list[LLMResponse]:
        """Draw ``n`` samples in a SINGLE request via the server's ``n`` param.

        Step 2 (parallel sampling): instead of firing ``n`` concurrent HTTP
        requests (which serialize to one batch slot on a single-GPU
        llama-server and pay ``n``× scheduling overhead), this issues one
        non-streaming request with ``"n": n``. The server draws all ``n``
        samples internally and returns them in one ``choices`` list — one
        network round-trip, server-side batch scheduling.

        Non-streaming is used deliberately: the early-termination optimization
        in ``_read_stream`` is per-completion, and interleaving ``n`` SSE
        choice streams adds complexity for little gain when the goal is simply
        to batch. The whole response is awaited once.

        Raises ``RuntimeError`` on HTTP/parse failure (same contract as
        ``complete``); callers fall back to the thread-pool path on error.
        """
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "n": n,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        _maybe_request_logprobs(body, self.config)
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
        # Same deadline + retry wrapping as complete(). _read_many doesn't need
        # the url, but _attempt normalizes the call arity, so pass it anyway.
        return _with_retry(
            lambda: self._attempt(req, url, self._read_many),
            attempts=self.config.retry_attempts,
            base_delay=self.config.retry_base_delay_seconds,
            max_delay=self.config.retry_max_delay_seconds,
        )

    def _read_many(self, req: urllib.request.Request, url: str = "") -> list[LLMResponse]:
        """Non-streaming read returning one ``LLMResponse`` per choice.

        ``url`` is accepted for signature parity with :meth:`_read_stream` so a
        single :meth:`_attempt` path can call either reader; it is unused here.
        """
        try:
            with urllib.request.urlopen(
                req, timeout=self.config.generation_timeout_seconds
            ) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except socket.timeout as exc:
            raise RuntimeError(
                f"LLM request failed: socket read timed out after "
                f"{self.config.generation_timeout_seconds}s"
            ) from exc
        choices = raw.get("choices") or []
        out: list[LLMResponse] = []
        for ch in choices:
            try:
                text = ch["message"]["content"]
            except (KeyError, TypeError):
                continue
            # Non-streaming logprobs: choices[i].logprobs.content[] (OpenAI shape).
            mte: float | None = None
            ch_lp = (ch.get("logprobs") or {})
            if isinstance(ch_lp, dict):
                mte = _mean_token_entropy_from_logprobs(ch_lp.get("content") or [])
            out.append(LLMResponse(text=text or "", raw=raw, mean_token_entropy=mte))
        return out

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
                token_nlls: list[float] = []
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
                        # Streaming per-token logprobs: delta.logprobs.content[].
                        if self.config.capture_token_entropy:
                            dlp = delta.get("logprobs")
                            if isinstance(dlp, dict):
                                mte = _mean_token_entropy_from_logprobs(
                                    dlp.get("content") or []
                                )
                                if mte is not None:
                                    token_nlls.append(mte)
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
        mean_token_entropy = (
            sum(token_nlls) / len(token_nlls) if token_nlls else None
        )
        return LLMResponse(
            text=text, raw=raw_meta, mean_token_entropy=mean_token_entropy
        )


def _maybe_request_logprobs(body: dict[str, Any], config: ModelConfig) -> None:
    """When ``capture_token_entropy`` is on, ask the API for per-token logprobs.

    Mutates ``body`` in place. With ``top_logprobs=1`` each delta's logprobs
    entry carries the realized token's own logprob, whose negation is the
    per-token negative log-likelihood — the standard TECP "token-entropy"
    surrogate. No weights are read; this is purely the black-box API output.
    Omitted entirely from the body when the flag is off, so deployments that
    don't use entropy capture see an unchanged request shape.
    """
    if getattr(config, "capture_token_entropy", False):
        body["logprobs"] = True
        body["top_logprobs"] = 1


def _mean_token_entropy_from_logprobs(entries: list[Any]) -> float | None:
    """Reduce a sequence of per-token logprob entries to mean NLL.

    Each entry follows the OpenAI shape::

        {"token": "...", "logprob": -0.8, "bytes": [...], "top_logprobs": [...]}

    Returns the mean of ``-logprob`` over all entries, or ``None`` when the
    list is empty / malformed (e.g. the server didn't actually emit logprobs).
    """
    nlls: list[float] = []
    for ent in entries:
        if not isinstance(ent, dict):
            continue
        lp = ent.get("logprob")
        if lp is None:
            continue
        try:
            nlls.append(-float(lp))
        except (TypeError, ValueError):
            continue
    if not nlls:
        return None
    return sum(nlls) / len(nlls)


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
