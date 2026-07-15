"""Embeddings client for the OpenAI-compatible ``/v1/embeddings`` endpoint.

A minimal, stdlib-only (urllib) client that mirrors the LLM adapter's HTTP
pattern. Used by :class:`~capybase.memory.retriever.EmbeddingRetriever` for
semantic RAG over past conflict resolutions, and by ``capybase calibrate`` to
detect whether the server supports embeddings.

The endpoint shape (llama-server with ``--embeddings`` loaded) is::

    POST /v1/embeddings  {model, input: str | list[str]}
    -> {data: [{embedding: [float, ...]}, ...], model, usage}

When no embedding model is loaded, llama-server returns HTTP 501 with
``{"error":{"code":501,"message":"...does not support embeddings..."}}``. The
client surfaces this as ``EmbeddingsNotSupportedError`` so callers can fall back
to the lexical (BM25) retriever gracefully.

Dependency-free and injectable: ``EmbeddingsClient.embed`` takes raw inputs and
returns vectors, so the retriever and tests can substitute a fake without HTTP.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Protocol

from capybase.config import ModelConfig


class EmbeddingsNotSupportedError(RuntimeError):
    """The server does not serve the embeddings endpoint (e.g. llama-server was
    not started with ``--embeddings``). Callers fall back to BM25 retrieval."""


class EmbeddingsClient(Protocol):
    """Minimal contract: embed one or more texts into equal-length vectors."""

    def embed(self, texts: str | list[str]) -> list[list[float]]: ...


def _embeddings_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/embeddings"


class OpenAIEmbeddingsClient:
    """Live embeddings client over the same ``ModelConfig`` the LLM uses.

    ``model`` is the embedding model name. On a local llama-server serving both a
    completion model and an embedding model, this is typically the embedding
    model's id (distinct from the completion model). When unsure, ``calibrate``
    probes the endpoint and records support in the profile.
    """

    def __init__(self, config: ModelConfig, *, timeout: float = 30.0) -> None:
        self.config = config
        self.timeout = timeout

    def embed(self, texts: str | list[str]) -> list[list[float]]:
        inputs = [texts] if isinstance(texts, str) else list(texts)
        body = json.dumps({"model": self.config.model, "input": inputs}).encode("utf-8")
        req = urllib.request.Request(
            _embeddings_url(self.config.base_url),
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload = _safe_read(exc)
            if exc.code == 501 or _looks_unsupported(payload):
                raise EmbeddingsNotSupportedError(
                    f"server does not support embeddings (start llama-server with --embeddings): "
                    f"{_err_message(payload) or exc}"
                ) from exc
            raise RuntimeError(f"embeddings request failed (HTTP {exc.code}): {exc}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"embeddings request failed: {exc}") from exc

        data = raw.get("data") or []
        # The server may return embeddings out of order; sort by the `index` field
        # (OpenAI spec) to preserve input alignment. llama-server preserves order,
        # but sorting is cheap insurance against a non-conformant server.
        ordered = sorted(data, key=lambda d: d.get("index", 0))
        vectors = [list(d.get("embedding") or []) for d in ordered]
        if len(vectors) != len(inputs):
            raise RuntimeError(
                f"embeddings count mismatch: requested {len(inputs)}, got {len(vectors)}"
            )
        return vectors


def _safe_read(exc: urllib.error.HTTPError) -> Any:
    try:
        return json.loads(exc.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _looks_unsupported(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    msg = (payload.get("error") or {})
    if isinstance(msg, dict):
        text = str(msg.get("message", "")).lower()
        return "not support embeddings" in text or "embedding" in text and "not" in text
    return False


def _err_message(payload: Any) -> str:
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            return str(err.get("message", ""))
    return ""


def probe_embeddings_support(client: EmbeddingsClient) -> bool:
    """One-call capability check: returns True iff the endpoint embeds a probe.

    Used by ``capybase calibrate`` (mirrors ``probe_logprobs``). Any failure —
    ``EmbeddingsNotSupportedError``, a request error, or an empty/garbage vector
    — means the endpoint can't be used for embedding RAG, and the caller keeps
    the lexical retriever. Never raises.
    """
    try:
        vectors = client.embed("capybase probe")
    except Exception:  # noqa: BLE001 - any failure = unsupported for our purposes
        return False
    if not vectors or not vectors[0]:
        return False
    return len(vectors[0]) > 0


# ---------------------------------------------------------------------------
# Body normalization for semantic entity matching 
# ---------------------------------------------------------------------------

import re as _re  # noqa: E402 - stdlib, kept local to avoid top-import noise

# Matches common string-literal quotes (single/double/triple, incl. f/r prefixes).
_STR_LITERAL = _re.compile(
    r"""(?:[rbfu]{0,2})(?:"""  # prefix + open-quote alternation
    r'''"""(?:.|\n)*?"""'''
    r"""|'''(?:.|\n)*?'''"""
    r"""|"(?:\\.|[^"\\])*\""""
    r"""|'(?:\\.|[^'\\])*\'"""
    r""")"""
)
# Line comments for the Family-A / common languages. Python ``#`` is handled by
# the same rule (the ``#`` to end-of-line); ``//`` covers Rust/JS/TS/Go/C++.
_LINE_COMMENT = _re.compile(r"#[^\n]*|//[^\n]*")


def normalize_body_for_embedding(text: str) -> str:
    """Normalize a code body so the embedding captures STRUCTURE not surface.

    Survey §2 "What to embed": strip comments, collapse whitespace, and replace
    string literals with a placeholder token so two functions that differ only
    in the text of a log message or a literal value still embed as similar.
    The body fingerprint's header-strip (``_split_header_body``) is the caller's
    responsibility — this operates on the body content only.

    Pure (no parse); language-agnostic. Never raises.
    """
    if not text:
        return ""
    # Order: literals first (so a ``#`` inside a string isn't stripped as a
    # comment), then comments, then whitespace collapse.
    out = _STR_LITERAL.sub("<STR>", text)
    out = _LINE_COMMENT.sub("", out)
    return " ".join(out.split())


# ---------------------------------------------------------------------------
# Batch chunking wrapper 
# ---------------------------------------------------------------------------


class BatchEmbeddingClient:
    """Wrap an :class:`EmbeddingsClient` to chunk large embed requests.

    llama-server /v1/embeddings accepts a list input but has a practical per-
    request cap (context length of the embedding model). The vector-cache build
     re-embeds the whole corpus on a cache miss, which can
    be thousands of texts. This wrapper splits the input into bounded batches,
    calls the underlying client per batch, and concatenates — preserving input
    alignment. Failed batches raise (the caller decides whether to skip the
    row); a fully-unavailable endpoint surfaces ``EmbeddingsNotSupportedError``
    on the first batch as before.
    """

    def __init__(self, client: EmbeddingsClient, *, batch_size: int = 32) -> None:
        self.client = client
        self.batch_size = max(1, int(batch_size))

    def embed(self, texts: str | list[str]) -> list[list[float]]:
        if isinstance(texts, str):
            return self.client.embed(texts)
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            out.extend(self.client.embed(texts[i : i + self.batch_size]))
        return out
