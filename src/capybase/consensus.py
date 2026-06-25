"""Self-consistency aggregation over multiple sampled resolutions.

A 3B model produces highly predictable, localized mistakes. Sampling N times
at non-zero temperature and taking the *majority* of normalized resolutions
brute-forces reliability out of the model: if 4 of 5 samples agree, the odd
one out was almost certainly a local slip. This module is the aggregator that
sits between ``ResolutionEngine.propose`` (which already loops ``samples``
times) and the orchestrator (which takes ``candidates[0]``).

The contract is pure: ``cluster`` groups candidates, ``select`` picks a winner.
The engine reorders its candidate list so the winner lands at index 0 without
changing the ``list[CandidateResolution]`` return shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from capybase.conflict_model import CandidateResolution


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

_TRAILING_WS = re.compile(r"[ \t]+$")
_BLANK_LINE = re.compile(r"\n\s*\n+")
# A trailing inline comment: whitespace, then # or // (not inside a string).
# Naive — does not track string state — but sufficient for clustering where a
# false positive only merges two lines that differ solely by a trailing
# comment, which is the desired behavior for self-consistency.
_TRAILING_COMMENT = re.compile(r"[ \t]+(#|//).*$")


def normalize(text: str, language: str | None = None) -> str:
    """Canonicalize a resolution for clustering.

    Strips trailing whitespace, trailing inline comments, and full comment
    lines, then collapses blank-line runs, so that semantically-identical
    resolutions that differ only in formatting cluster together. Indentation
    is PRESERVED (it is structurally significant in Python and meaningful in
    Rust).
    """
    if not text:
        return ""
    lines = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        if _is_comment_line(stripped, language):
            continue
        # Remove trailing inline comments (e.g. "x = 1  # foo" -> "x = 1").
        line = _TRAILING_COMMENT.sub("", line)
        lines.append(_TRAILING_WS.sub("", line))
    out = "\n".join(lines)
    out = _BLANK_LINE.sub("\n", out)
    # Strip only trailing newlines/whitespace from the whole string — NOT
    # leading indentation, which is structurally significant (Python) and
    # distinguishes a top-level statement from a method body.
    out = out.rstrip()
    return out


def _is_comment_line(stripped: str, language: str | None) -> bool:
    """True if a line is entirely a comment (after stripping leading ws)."""
    if not stripped:
        return False
    if language == "python":
        return stripped.startswith("#")
    if language == "rust":
        return stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*")
    # Language-agnostic: treat # and // comment prefixes as comments.
    return stripped.startswith("#") or stripped.startswith("//")


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


@dataclass
class Cluster:
    """A group of semantically-identical candidates.

    ``agreement`` is the fraction of all samples in this cluster (size/N).
    The ``representative`` is the first member (the earliest sample), chosen
    so the winner's identity is stable across runs.
    """

    members: list[CandidateResolution] = field(default_factory=list)
    normalized: str = ""
    language: str | None = None

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def agreement(self) -> float:
        return self.size / len(self.members) if self.members else 0.0

    @property
    def representative(self) -> CandidateResolution | None:
        return self.members[0] if self.members else None


@dataclass
class ConsensusReport:
    """The outcome of a self-consistency vote."""

    winner: CandidateResolution | None
    clusters: list[Cluster]
    n_samples: int
    agreement_score: float  # winner cluster's agreement fraction
    cluster_count: int
    entropy: float = 0.0  # normalized Shannon entropy (0=unanimous, 1=max split)

    @property
    def has_majority(self) -> bool:
        """True if the winner cluster holds > 50% of samples."""
        return self.agreement_score > 0.5


def cluster(
    candidates: list[CandidateResolution], language: str | None = None
) -> list[Cluster]:
    """Group candidates by normalized resolved text.

    Candidates with empty resolved_text (failed/parsed-failed) are grouped
    into their own cluster keyed on the empty string, so a unanimous failure
    still produces a single cluster. Clusters are returned in descending size
    order (largest first); ties broken by earliest sample (stable).
    """
    buckets: dict[str, Cluster] = {}
    order: list[str] = []
    for cand in candidates:
        norm = normalize(cand.resolved_text, language)
        if norm not in buckets:
            buckets[norm] = Cluster(normalized=norm, language=language)
            order.append(norm)
        buckets[norm].members.append(cand)
    clusters = [buckets[k] for k in order]
    # Sort by size desc, preserving insertion order for ties (stable sort).
    clusters.sort(key=lambda c: -c.size)
    return clusters


def select(
    candidates: list[CandidateResolution],
    language: str | None = None,
    *,
    min_agreement: float = 0.0,
) -> ConsensusReport:
    """Pick the consensus winner by majority vote.

    The largest cluster wins. Ties (equal cluster sizes) are broken by highest
    ``self_reported_confidence`` among the cluster's representative, then by
    shortest ``resolved_text`` (prefer concise). ``min_agreement`` does NOT
    change the winner — it only affects whether ``has_majority`` reports
    confidence; callers use the agreement score in risk policy.
    """
    n = len(candidates)
    if n == 0:
        return ConsensusReport(
            winner=None, clusters=[], n_samples=0,
            agreement_score=0.0, cluster_count=0,
        )
    clusters_ = cluster(candidates, language)
    if not clusters_:
        return ConsensusReport(
            winner=None, clusters=[], n_samples=n,
            agreement_score=0.0, cluster_count=0,
        )
    # Tie-break among same-size clusters: confidence desc, then length asc.
    top_size = clusters_[0].size
    tied = [c for c in clusters_ if c.size == top_size]
    if len(tied) > 1:
        tied.sort(
            key=lambda c: (
                -(c.representative.self_reported_confidence if c.representative else 0.0),
                len(c.representative.resolved_text) if c.representative else 0,
            )
        )
    winner_cluster = tied[0]
    agreement = winner_cluster.size / n
    # Normalized Shannon entropy over cluster sizes: 0 = all samples agree
    # (one cluster), 1 = maximally split (every sample different). High entropy
    # means no candidate is trustworthy — the risk engine escalates.
    entropy = _entropy([c.size for c in clusters_], n)
    return ConsensusReport(
        winner=winner_cluster.representative,
        clusters=clusters_,
        n_samples=n,
        agreement_score=agreement,
        cluster_count=len(clusters_),
        entropy=entropy,
    )


def _entropy(sizes: list[int], n: int) -> float:
    """Normalized Shannon entropy of a partition.

    Returns 0..1 where 0 = unanimous (one cluster) and 1 = maximally split
    (each sample in its own cluster). Computed as H/log2(n).
    """
    if n <= 1:
        return 0.0
    import math

    h = 0.0
    for s in sizes:
        if s > 0:
            p = s / n
            h -= p * math.log2(p)
    return h / math.log2(n)


def rank_by_consensus(
    candidates: list[CandidateResolution], language: str | None = None
) -> tuple[list[CandidateResolution], ConsensusReport]:
    """Reorder ``candidates`` so the consensus winner is first.

    Returns the reordered list (winner at index 0, then the rest of its
    cluster, then other clusters in descending size order) plus the report.
    This lets ``ResolutionEngine`` keep its ``list[CandidateResolution]``
    return contract while the orchestrator's ``candidates[0]`` takes the
    majority winner.
    """
    report = select(candidates, language)
    if report.winner is None or not candidates:
        return list(candidates), report
    ordered: list[CandidateResolution] = []
    seen: set[str] = set()
    for cl in report.clusters:
        for m in cl.members:
            if m.candidate_id not in seen:
                ordered.append(m)
                seen.add(m.candidate_id)
    # Fallback: include any candidates not present in clusters (shouldn't
    # happen, but guards against dedup drift).
    for c in candidates:
        if c.candidate_id not in seen:
            ordered.append(c)
    return ordered, report
