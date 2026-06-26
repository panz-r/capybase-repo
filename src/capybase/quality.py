"""Quality scoring for mechanism calibration.

The core problem calibration solves: there is no existing quality oracle. The
risk engine's score is self-referential (it learns from capybase's own
accept/reject decision), and the model's ``self_reported_confidence`` /
``preserved_*_side`` are unchecked self-claims. So calibration must build its
own signal.

This module provides it via a **blessed-output corpus** (see
:mod:`capybase.calibration_corpus`): correctness = does the candidate's
``resolved_text`` match the known-correct merge, after normalization. Validator
proxies (syntax/AST/splice/copied-one-side) are SECONDARY tie-breakers — they
flag structural soundness but a syntactically-valid merge can still be
semantically wrong, so they never override correctness.

Scoring is a lexicographic tuple ``(correctness_count, proxy_sum, -latency)``
so "more correct" always wins, with proxies then latency breaking ties. This
gives a stable, non-noisy ordering for the A/B comparisons in
``probe_mechanisms``.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from capybase.calibration_corpus import CalibrationConflict, conflicts_with_context
from capybase.conflict_model import CandidateResolution, VerificationResult
from capybase.config import ModelConfig


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")
# Spaces adjacent to structural punctuation. Collapsing whitespace alone leaves
# ``["a","b"]`` != ``["a", "b"]`` (no internal whitespace to collapse in the
# former), so we also strip spaces around punctuation that is purely structural
# in code/config merges: brackets, braces, commas, colons, equals. ``[`` and
# ``]`` are escaped to be literal inside the character class. Safe because such
# punctuation is never part of a string literal's meaningful *spacing* in a
# resolution block — only its position matters.
_PUNCT_SPACE_RE = re.compile(r"\s*([\[\]{}(),:=])\s*")


def normalize_resolved(text: str) -> str:
    """Normalize a resolved-text block for correctness comparison.

    Two steps, both formatting-only (no semantic change):
    1. Strip spaces adjacent to structural punctuation, so ``["a","b"]`` matches
       ``[ "a", "b" ]`` matches ``[ "a",\n "b" ]``.
    2. Collapse remaining whitespace runs to single spaces and strip, so
       newline/indent variance doesn't defeat the check.

    We do NOT lowercase or remove other characters — those are semantic.
    """
    t = _PUNCT_SPACE_RE.sub(r"\1", text or "")
    return _WHITESPACE_RE.sub(" ", t).strip()


def _is_correct(candidate_text: str, expected: str) -> bool:
    """Correct iff the normalized candidate equals the normalized expected OR
    contains it as a complete normalized block (the model sometimes wraps the
    merge with surrounding lines; containment handles that without rewarding
    extra junk when it doesn't actually include the blessed content)."""
    norm_cand = normalize_resolved(candidate_text)
    norm_exp = normalize_resolved(expected)
    if not norm_exp:
        return False
    if norm_cand == norm_exp:
        return True
    # Containment as a fallback: the merge must be present verbatim (normalized).
    return norm_exp in norm_cand


# ---------------------------------------------------------------------------
# Per-conflict score
# ---------------------------------------------------------------------------


@dataclass
class ConflictScore:
    """Score for one candidate on one conflict."""

    title: str
    correct: bool
    proxy: float  # sum of validator-proxy signals (higher = better)
    latency_ms: float
    # Human-readable detail (which expected text, whether it matched, proxies).
    detail: str = ""


def _proxy_from_features(features: dict[str, Any] | None) -> float:
    """Combine validator features into a single secondary score.

    Each positive structural signal adds 1; each negative subtracts 1. This is
    deliberately coarse — proxies only break correctness ties, so a precise
    weighting would imply more confidence than they warrant. Missing features
    (validator didn't run, e.g. no LSP) contribute 0.
    """
    if not features:
        return 0.0
    score = 0.0
    # Positive: the merge is structurally sound.
    for k in ("syntax_passed", "ast_preserved", "splice_scope_ok"):
        if features.get(k):
            score += 1.0
    # Negative: structural problems.
    if features.get("copied_one_side"):
        score -= 1.0
    new_err = features.get("lsp_new_error_count")
    if isinstance(new_err, (int, float)) and new_err > 0:
        score -= float(new_err)
    return score


def score_candidate(
    candidate: CandidateResolution,
    conflict: CalibrationConflict,
    verification: VerificationResult | None = None,
    latency_ms: float = 0.0,
) -> ConflictScore:
    """Score a single candidate against its blessed conflict.

    ``verification`` is optional — when the caller ran the validator pipeline
    (the orchestrator does; calibration's lightweight eval may not), its
    ``features`` feed the proxy tie-breaker. Correctness never depends on it.
    """
    correct = _is_correct(candidate.resolved_text or "", conflict.expected_text)
    proxy = _proxy_from_features(
        verification.features if verification else getattr(candidate, "_proxy_features", None)
    )
    matched_repr = normalize_resolved(candidate.resolved_text or "")[:40]
    return ConflictScore(
        title=conflict.title,
        correct=correct,
        proxy=proxy,
        latency_ms=latency_ms,
        detail=f"expected match={correct}; candidate~={matched_repr!r}; proxy={proxy}",
    )


# ---------------------------------------------------------------------------
# Per-setting score (one full corpus evaluation under a config)
# ---------------------------------------------------------------------------


@dataclass
class SettingScore:
    """Aggregate score for a ModelConfig (a candidate setting) over the corpus."""

    n_correct: int
    proxy_sum: float
    mean_latency_ms: float
    per_conflict: list[ConflictScore] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.per_conflict)

    def __repr__(self) -> str:
        return (
            f"SettingScore(correct={self.n_correct}/{self.total}, "
            f"proxy={self.proxy_sum:.1f}, mean_lat={self.mean_latency_ms:.0f}ms)"
        )


def compare_scores(a: SettingScore, b: SettingScore) -> int:
    """Lexicographic ordering: correctness (more is better) → proxy (higher
    better) → latency (lower better). Returns negative if a < b, 0 if equal,
    positive if a > b — standard comparator semantics for ``sorted``/``max``."""
    if a.n_correct != b.n_correct:
        return (a.n_correct > b.n_correct) - (a.n_correct < b.n_correct)
    if a.proxy_sum != b.proxy_sum:
        return (a.proxy_sum > b.proxy_sum) - (a.proxy_sum < b.proxy_sum)
    # Lower latency wins.
    if a.mean_latency_ms != b.mean_latency_ms:
        return (a.mean_latency_ms < b.mean_latency_ms) - (a.mean_latency_ms > b.mean_latency_ms)
    return 0


def evaluate_setting(
    resolve_one: Callable[[CalibrationConflict, ContextBundle, ModelConfig], tuple[CandidateResolution, VerificationResult | None, float]],
    model_cfg: ModelConfig,
) -> SettingScore:
    """Resolve every corpus conflict under ``model_cfg`` and aggregate.

    ``resolve_one`` is injected by the caller (probe_mechanisms): it takes a
    conflict, its context, and a ModelConfig, and returns
    ``(candidate, verification_or_None, latency_ms)``. This keeps the quality
    module independent of the ResolutionEngine/client plumbing — the caller
    decides HOW to resolve (single-sample, consensus, two-pass), this module
    only scores the result.
    """
    per: list[ConflictScore] = []
    latencies: list[float] = []
    for conflict, context in conflicts_with_context():
        try:
            candidate, verification, latency_ms = resolve_one(conflict, context, model_cfg)
        except Exception as exc:  # noqa: BLE001 - a failed resolution is a miss
            per.append(ConflictScore(conflict.title, False, 0.0, 0.0, f"error: {exc}"))
            continue
        latencies.append(latency_ms)
        per.append(score_candidate(candidate, conflict, verification, latency_ms))
    return SettingScore(
        n_correct=sum(1 for s in per if s.correct),
        proxy_sum=sum(s.proxy for s in per),
        mean_latency_ms=sum(latencies) / len(latencies) if latencies else 0.0,
        per_conflict=per,
    )
