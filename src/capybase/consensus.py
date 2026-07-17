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
_TRAILING_COMMENT = re.compile(r"[ \t]+(#|//).*$")


def _strip_trailing_comment(line: str, *, language: str | None = None) -> str:
    """Remove a trailing ``#``/``//`` comment, respecting string literals.

    Finds the comment on a string-blanked copy (so a ``#`` inside a string
    literal is not mistaken for a comment), then strips from that position on
    the ORIGINAL line. Blanking is length-preserving so positions map 1:1.

    ``language`` selects the comment marker: ``//`` is a comment only in brace
    languages (Rust/JS/Go/Java/C++/...), NOT in Python/Ruby where ``//`` is floor
    division. ``#`` is a comment in Python/Ruby (and Rust attributes at line
    start, but trailing ``#`` is not a comment in Rust — only Python/Ruby use
    trailing ``#`` comments in practice).
    """
    from capybase.adapters.abstract_parser import _STRING_LIT_RE
    blanked = _STRING_LIT_RE.sub(lambda mm: " " * len(mm.group(0)), line)
    # ``//`` is a line comment only in brace languages.
    slash_is_comment = language not in (None, "python", "ruby")
    # ``#`` is a line comment in Python/Ruby (and shell).
    hash_is_comment = language in (None, "python", "ruby", "php")
    # Find the FIRST comment marker in the blanked line that is preceded by
    # whitespace (a trailing comment) — not inside a string (which is now blanked).
    for i, ch in enumerate(blanked):
        if hash_is_comment and ch == "#" and i > 0 and blanked[i - 1] in " \t":
            return line[:i].rstrip()
        if slash_is_comment and ch == "/" and i + 1 < len(blanked) and blanked[i + 1] == "/" and i > 0 and blanked[i - 1] in " \t":
            return line[:i].rstrip()
    return line


def normalize(text: str, language: str | None = None) -> str:
    """Canonicalize a resolution for clustering.

    Strips trailing whitespace, trailing inline comments (string-aware,
    language-aware), and full comment lines, then collapses blank-line runs, so
    that semantically-identical resolutions that differ only in formatting
    cluster together. Indentation is PRESERVED (it is structurally significant
    in Python and meaningful in Rust).
    """
    if not text:
        return ""
    lines = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        if _is_comment_line(stripped, language):
            continue
        # Remove trailing inline comments (e.g. "x = 1  # foo" -> "x = 1"),
        # respecting string boundaries so a # inside a string isn't stripped.
        line = _strip_trailing_comment(line, language=language)
        lines.append(_TRAILING_WS.sub("", line))
    out = "\n".join(lines)
    out = _BLANK_LINE.sub("\n", out)
    # Strip only trailing newlines/whitespace from the whole string — NOT
    # leading indentation, which is structurally significant (Python) and
    # distinguishes a top-level statement from a method body.
    out = out.rstrip()
    return out


def _is_comment_line(stripped: str, language: str | None) -> bool:
    """True if a line is entirely a comment (after stripping leading ws).

    Delegates to the language adapter (#5) so the comment-prefix decision has a
    single home; the registry's NullAdapter handles unknown languages with the
    conservative `#`/`//` prefixes (matching the prior language-agnostic fallthrough).
    """
    if not stripped:
        return False
    from capybase.adapters.language import adapter_for
    return stripped.startswith(adapter_for(language).comment_line_prefixes)


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
    # FactSelfCheck rationale-consistency: agreement over the
    # candidates' OWN intent claims (preserved_* booleans + intent-list items),
    # NOT over their code text. 1.0 = every candidate made the same claims; low
    # = candidates disagree about what they did — a hallucination/unstable-claim
    # signal that text-consensus cannot see (identical code, contradictory
    # intents). Defaults to 1.0 (no claims extracted → not penalized).
    intent_agreement: float = 1.0
    # Count of distinct facts with a meaningful dissent (minority ≥ ~1/3 of
    # candidates). High → several candidates rely on contested claims. A
    # calibration/risk feature, like entropy.
    low_consistency_fact_count: int = 0

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

    The largest cluster wins. Ties (equal cluster sizes) are broken by:

    1. highest **fact-consistency** of the cluster's representative
       (FactSelfCheck): prefer the candidate whose rationale claims are most
       shared across all samples — down-weights a candidate that relies on a
       low-consistency/minority claim),
    2. then highest ``self_reported_confidence``,
    3. then shortest ``resolved_text`` (prefer concise).

    ``min_agreement`` does NOT change the winner — it only affects whether
    ``has_majority`` reports confidence; callers use the agreement score in
    risk policy.
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
    # FactSelfCheck rationale-consistency, computed once over all candidates.
    fc = fact_consistency(candidates)
    # Tie-break among same-size clusters: fact-consistency desc, then confidence
    # desc, then length asc.
    top_size = clusters_[0].size
    tied = [c for c in clusters_ if c.size == top_size]
    if len(tied) > 1:
        tied.sort(
            key=lambda c: (
                -(fc.per_candidate.get(c.representative.candidate_id, 1.0)
                  if c.representative else 1.0),
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
        intent_agreement=fc.aggregate,
        low_consistency_fact_count=fc.low_consistency_count,
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


# ---------------------------------------------------------------------------
# FactSelfCheck rationale-consistency
#
# The resolve prompt already asks the model for structured rationale fields
# (current_side_intent, replayed_commit_intent, preserved_*_side booleans) and
# they are captured on every CandidateResolution — but consensus keys only on
# normalized resolved_text, so this signal is discarded. FactSelfCheck turns it
# into a consistency score: how often do the candidates' OWN claims agree?
# Orthogonal to text-consensus: two candidates with identical code but
# contradictory intent claims still score low here. Pure post-hoc aggregation
# over rationales that already exist — no new LLM calls, no training.
# ---------------------------------------------------------------------------

_WS_RUN = re.compile(r"\s+")


def _canonicalize_fact(text: str) -> str:
    """Canonicalize a rationale string for cross-candidate comparison.

    Lowercase, collapse whitespace runs to single spaces, strip leading/trailing
    whitespace and trailing punctuation (periods, commas, semicolons). Naive
    but sufficient: the goal is to merge near-identical claims that differ only
    in phrasing/casing, not to do semantic NLI.
    """
    if not text:
        return ""
    out = _WS_RUN.sub(" ", str(text)).strip().lower()
    return out.rstrip(".,;:")


def _extract_facts(candidate: CandidateResolution) -> dict[str, str]:
    """Canonicalize a candidate's rationale fields into comparable facts.

    Returns a dict of ``fact_key -> canonical_value``. Each key is unique per
    fact so candidates can be compared fact-by-fact:

    - ``preserve:current`` / ``preserve:replayed``: the preserved_* booleans as
      the strings ``"true"`` / ``"false"`` (already clean, just stringified).
    - ``intent:current:<i>`` / ``intent:replayed:<i>``: each non-empty intent
      list item, canonicalized, keyed by its position so the i-th claim of one
      candidate lines up with the i-th claim of another.

    Candidates that produced no intent lists (failed parse, model omitted them)
    contribute only the boolean facts (if any) — they never drag the score down
    for facts they simply didn't assert. Returns ``{}`` for a candidate with no
    rationale at all (a technical failure).
    """
    facts: dict[str, str] = {}
    facts["preserve:current"] = "true" if candidate.preserved_current_side else "false"
    facts["preserve:replayed"] = (
        "true" if candidate.preserved_replayed_commit_side else "false"
    )
    for label, items in (
        ("current", candidate.current_side_intent),
        ("replayed", candidate.replayed_commit_intent),
    ):
        for i, item in enumerate(items or []):
            canon = _canonicalize_fact(item)
            if canon:
                facts[f"intent:{label}:{i}"] = canon
    return facts


@dataclass
class FactConsistency:
    """Per-fact and per-candidate rationale-consistency scores."""

    # fact_key -> fraction of candidates asserting that exact value. 1.0 =
    # unanimous on that fact; low = a minority claim (possible hallucination).
    per_fact: dict[str, float] = field(default_factory=dict)
    # candidate_id -> mean consistency of the facts that candidate asserts. A
    # candidate whose every claim is shared by most peers scores high; one
    # relying on a low-consistency claim scores low. Missing when a candidate
    # asserted no facts.
    per_candidate: dict[str, float] = field(default_factory=dict)
    # Mean per-fact consistency across all observed facts (0..1). The aggregate
    # rationale-agreement signal analogous to text agreement_score.
    aggregate: float = 1.0
    # Distinct facts with a meaningful dissent (minority ≥ ~1/3 of candidates).
    low_consistency_count: int = 0


def fact_consistency(
    candidates: list[CandidateResolution],
) -> FactConsistency:
    """Compute FactSelfCheck-style consistency over candidate rationales.

    Two complementary views:

    - **per_fact[key]**: the share of the *majority* value for that fact key
      (1.0 = all candidates agree; < 1.0 = some disagree). A global signal.
    - **per_candidate[id]**: the mean, over the facts a candidate asserts, of
      the share of *that candidate's own value* — so an outlier whose value is
      the minority one scores low, while a candidate on the majority side scores
      high. This is what isolates a candidate relying on a low-consistency
      (possibly hallucinated) claim.

    Facts are position-aligned (the i-th intent claim of each candidate is
    compared), measuring whether candidates agree on the same numbered claim.
    """
    n = len(candidates)
    if n == 0:
        return FactConsistency()
    # Collect, per fact key, the multiset of asserted values across candidates.
    # A candidate that omits a key (e.g. didn't list a 3rd intent) does not
    # count toward that key's denominator.
    values_by_key: dict[str, dict[str, int]] = {}
    facts_by_candidate: list[dict[str, str]] = []
    for cand in candidates:
        facts = _extract_facts(cand)
        facts_by_candidate.append(facts)
        for key, val in facts.items():
            values_by_key.setdefault(key, {})
            values_by_key[key][val] = values_by_key[key].get(val, 0) + 1

    def _value_share(key: str, val: str) -> float:
        """Share of candidates asserting ``val`` for ``key`` (0..1)."""
        counts = values_by_key.get(key, {})
        denom = sum(counts.values())
        if denom == 0:
            return 0.0
        return counts.get(val, 0) / denom

    per_fact: dict[str, float] = {}
    for key, counts in values_by_key.items():
        denom = sum(counts.values())
        if denom == 0:
            continue
        # Key consistency = the largest value's share (how agreed-upon the key
        # is overall). If all candidates asserted the same value → 1.0.
        per_fact[key] = max(counts.values()) / denom

    per_candidate: dict[str, float] = {}
    for cand, facts in zip(candidates, facts_by_candidate):
        if not facts:
            continue
        # Score each fact by the share of THIS candidate's own value — so an
        # outlier whose value is the minority one scores low, while a candidate
        # on the majority side scores high. This isolates low-consistency
        # (possibly hallucinated) claims to the candidate that made them.
        scores = [_value_share(k, v) for k, v in facts.items()]
        per_candidate[cand.candidate_id] = sum(scores) / len(scores)

    aggregate = sum(per_fact.values()) / len(per_fact) if per_fact else 1.0
    # A fact is "low consistency" when there is a meaningful dissent: not
    # unanimous AND the minority holds at least ~1/3 of candidates. This flags
    # genuine 2-vs-1 style disagreements (the signal FactSelfCheck targets)
    # without noising up the count when 4-of-5 agree and one sample flaked.
    low = 0
    for key, counts in values_by_key.items():
        if len(counts) < 2:
            continue
        denom = sum(counts.values())
        if denom == 0:
            continue
        minority_share = (denom - max(counts.values())) / denom
        if minority_share >= 1 / 3:
            low += 1
    return FactConsistency(
        per_fact=per_fact,
        per_candidate=per_candidate,
        aggregate=aggregate,
        low_consistency_count=low,
    )


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
