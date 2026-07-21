"""Claim atomization for the shadow jury (Part SJ1, design §5A).

The atomizer splits a comment into atomic propositions and records modality.
It does NOT vote — it's the non-voting foundation the jurors build on.

A claim like:

    "Retries all transient errors three times and logs each failure."

becomes:

    C1: The function retries transient errors.       (universal, error_behavior)
    C2: The function retries every transient error.   (universal)
    C3: The retry count is exactly three.             (existential, exact count)
    C4: Each failed attempt is logged.                (universal, side_effect)

The origin classification (§5A) drives the design's asymmetric rule (§2):
newly synthesized claims require positive grounding; inherited claims require
positive contradiction before automatic removal.

Pure data model + deterministic origin classification here; the prompt-based
atomization (atomize_comment) is the LLM entry point. The deterministic parts
are unit-testable without a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# The Claim dataclass (SJ1a)
# ---------------------------------------------------------------------------


#: Claim kinds (design §5A classification taxonomy).
CLAIM_KINDS = frozenset({
    "implementation_description", "public_contract", "invariant",
    "error_behavior", "side_effect", "ordering", "concurrency",
    "security", "performance", "complexity", "rationale",
    "external_dependency", "historical_statement", "other",
})

#: Claim modalities. "universal" = all/never/always; "conditional" = if/when;
#: "existential" = exactly N / at most N; "non_checkable" = rationale/history.
CLAIM_MODALITIES = frozenset({
    "universal", "conditional", "existential", "non_checkable",
})

#: Claim origins (design §5C provenance classification).
CLAIM_ORIGINS = frozenset({
    "inherited_exact",        # identical text in a source variant
    "inherited_paraphrase",   # same meaning, different words
    "inherited_narrowed",     # a weaker version of a source claim
    "inherited_strengthened", # a stronger version (e.g. may→must)
    "merged_from_sources",    # combines two source claims
    "synthesized",            # not traceable to any source
    "origin_uncertain",       # can't determine
})


@dataclass
class Claim:
    """One atomic proposition extracted from a comment (SJ1a).

    ``claim_id`` is ``"<lineage_id>.<index>"`` (e.g. "LC17.2"). ``origin`` is
    classified by :func:`classify_claim_origin` comparing against source variants.
    ``kind`` and ``modality`` guide the jurors' evaluation strategy.
    """
    claim_id: str
    lineage_id: str
    text: str
    origin: str = "origin_uncertain"
    kind: str = "implementation_description"
    modality: str = "universal"
    text_span: tuple[int, int] = (0, 0)


# ---------------------------------------------------------------------------
# Origin classification (SJ1c) — deterministic, no LLM
# ---------------------------------------------------------------------------


#: Modality strength ordering for strengthen/narrow detection. A claim that
#: moves UP this scale (e.g. "may" → "always") is "inherited_strengthened".
#: A claim that moves DOWN (e.g. "always" → "may") is "inherited_narrowed".
_MODALITY_STRENGTH = {
    "non_checkable": 0,  # rationale/history — not a strength claim
    "existential": 1,    # "exactly 3" / "at most 3" — bounded
    "conditional": 2,    # "if X then Y" — scoped
    "universal": 3,      # "all" / "never" / "always" — strongest
}

#: Universal-quantifier keywords (signal modality=universal).
_UNIVERSAL_RE = re.compile(
    r"\b(?:all|every|each|any|never|always|none|no\s+\w+)\b", re.IGNORECASE,
)
#: Existential/exact-count keywords (signal modality=existential).
_EXISTENTIAL_RE = re.compile(
    r"\b(?:exactly|at most|at least|up to|\d+\s*times?|\d+\s*retries?)\b",
    re.IGNORECASE,
)
#: Conditional keywords (signal modality=conditional).
_CONDITIONAL_RE = re.compile(
    r"\b(?:if|when|unless|except|in case|provided that|assuming)\b",
    re.IGNORECASE,
)
#: Rationale/history keywords (signal modality=non_checkable).
_RATIONALE_RE = re.compile(
    r"\b(?:because|historically|previously|rationale|reason|legacy|originally)\b",
    re.IGNORECASE,
)
#: Modality-strengthening keywords (may→always, could→must, etc.).
_STRENGTHENING_RE = re.compile(
    r"\b(?:must|shall|required|always|never|guaranteed)\b", re.IGNORECASE,
)


def detect_modality(text: str) -> str:
    """Heuristic modality detection from claim text (SJ1c helper).

    Returns one of CLAIM_MODALITIES. Conservative: rationale/history →
    non_checkable; universal quantifiers → universal; exact counts →
    existential; conditionals → conditional; default → universal (the safest
    assumption for a claim that doesn't match a weaker pattern).
    """
    if _RATIONALE_RE.search(text):
        return "non_checkable"
    if _EXISTENTIAL_RE.search(text):
        return "existential"
    if _CONDITIONAL_RE.search(text):
        return "conditional"
    if _UNIVERSAL_RE.search(text):
        return "universal"
    return "universal"  # default: treat as universal (strongest check)


def detect_kind(text: str) -> str:
    """Heuristic kind detection from claim text (SJ1c helper).

    Returns one of CLAIM_KINDS. Keyword-based; defaults to
    ``implementation_description``. Ordering matters: check the more specific
    kinds (invariant, concurrency, security) BEFORE error_behavior, since a
    claim like "MUST not retry auth" is both error-related AND an invariant —
    the invariant classification is more useful for the jury.
    """
    low = text.lower()
    # Invariant/contract first: MUST/SHALL/NEVER/ALWAYS override error_behavior.
    if any(k in low for k in ("must", "shall", "invariant", "guarantee", "required", "never", "always")):
        return "invariant"
    if any(k in low for k in ("thread-safe", "atomic", "lock", "mutex", "concurrent")):
        return "concurrency"
    if any(k in low for k in ("security", "encrypt", "vulnerable")):
        return "security"
    if any(k in low for k in ("o(", "complexity", "linear", "constant time", "logarithmic")):
        return "complexity"
    if any(k in low for k in ("performance", "fast", "slow", "latency", "throughput")):
        return "performance"
    if any(k in low for k in ("retr", "raise", "throw", "exception", "error", "fail")):
        return "error_behavior"
    if any(k in low for k in ("side effect", "mutates", "writes", "logs", "prints")):
        return "side_effect"
    if any(k in low for k in ("before", "after", "order", "sequence", "then")):
        return "ordering"
    if any(k in low for k in ("param", "argument", "return", "returns", "signature", "public")):
        return "public_contract"
    if any(k in low for k in ("because", "rationale", "reason", "why", "historically")):
        return "rationale"
    if any(k in low for k in ("external", "remote", "protocol", "service", "api")):
        return "external_dependency"
    return "implementation_description"


def _tokenize(text: str) -> set[str]:
    """Lowercase word-token set (for set-containment comparison)."""
    return set(re.findall(r"[a-z_][a-z0-9_]*", text.lower()))


def classify_claim_origin(
    claim_text: str, source_variants: list[str],
) -> str:
    """Classify a claim's origin against source comment variants (SJ1c).

    Returns one of CLAIM_ORIGINS. The design's asymmetric rule (§2) keys off
    this: synthesized claims need positive grounding; inherited claims need
    positive contradiction.

    - ``inherited_exact``: the claim text matches a source variant verbatim
      (after normalization).
    - ``inherited_paraphrase``: high token overlap (≥0.7 Jaccard) with a
      source variant but not verbatim.
    - ``inherited_narrowed``: the claim is a subset (weaker) of a source.
    - ``inherited_strengthened``: the claim is stronger (e.g. adds MUST).
    - ``merged_from_sources``: combines tokens from ≥2 source variants.
    - ``synthesized``: low overlap with ALL sources (<0.3 Jaccard).
    - ``origin_uncertain``: moderate overlap (0.3–0.7), can't classify.
    """
    if not source_variants:
        return "origin_uncertain"
    claim_tokens = _tokenize(claim_text)
    if not claim_tokens:
        return "origin_uncertain"
    # Normalize source variants the same way.
    norm_claim = " ".join(sorted(claim_tokens))
    overlaps: list[tuple[float, str, set[str]]] = []  # (jaccard, norm_source, src_tokens)
    for src in source_variants:
        src_tokens = _tokenize(src)
        if not src_tokens:
            continue
        norm_src = " ".join(sorted(src_tokens))
        union = claim_tokens | src_tokens
        jaccard = len(claim_tokens & src_tokens) / len(union) if union else 0.0
        overlaps.append((jaccard, norm_src, src_tokens))
    if not overlaps:
        return "origin_uncertain"
    # Exact match (after normalization).
    if any(norm_claim == norm_src for _, norm_src, _ in overlaps):
        return "inherited_exact"
    best_jaccard = max(o[0] for o in overlaps)
    if best_jaccard < 0.3:
        # Low overlap with all → check if it's a merge of ≥2 sources.
        sources_covering: set[str] = set()
        for _j, _ns, st in overlaps:
            # Does this source cover >50% of the claim's tokens?
            if len(claim_tokens & st) >= len(claim_tokens) * 0.5:
                sources_covering.add(_ns)
        if len(sources_covering) >= 2:
            return "merged_from_sources"
        return "synthesized"
    # Find the best-matching source for strengthen/narrow detection.
    best = max(overlaps, key=lambda o: o[0])
    best_tokens = best[2]
    if best_jaccard >= 0.4:
        # Moderate-high overlap — paraphrase, narrowed, or strengthened.
        if _STRENGTHENING_RE.search(claim_text) and not _STRENGTHENING_RE.search(
            source_variants[overlaps.index(best)]
        ):
            return "inherited_strengthened"
        # Is the claim a SUBSET of the source (fewer tokens → narrowed)?
        if claim_tokens < best_tokens:
            return "inherited_narrowed"
        return "inherited_paraphrase"
    # Moderate overlap (0.3–0.7) — uncertain.
    return "origin_uncertain"


# ---------------------------------------------------------------------------
# Claim atomization prompt (SJ1b) — the LLM entry point
# ---------------------------------------------------------------------------


def build_atomize_prompt(
    comment_text: str, lineage_id: str, source_variants: list[str], lang: str,
) -> str:
    """Build the claim-atomization prompt (design §5A).

    The model splits the comment into atomic propositions, preserving important
    terms (all/only/never/may/must, exact vs upper-bound quantities, conditions,
    units, ordering, side effects, API guarantees). Returns structured JSON.
    """
    import json
    sources_block = ""
    if source_variants:
        sources_block = "\n\nSource comment variants (for origin classification):\n"
        for i, v in enumerate(source_variants):
            sources_block += f"  [{i}]: {v!r}\n"
    return f"""You are a claim atomizer. Split the comment into atomic propositions.

Comment (lineage {lineage_id}, language {lang}):
{comment_text!r}
{sources_block}

Rules:
1. Split on conjunctions and separate assertions. Each claim should be ONE
   testable proposition.
2. Preserve important terms exactly: all, only, never, may, must, shall,
   required, always, exactly, at most, up to, before, after, if, unless.
3. Distinguish exact counts ("exactly 3") from upper bounds ("up to 3").
4. Record the modality of each claim.
5. Record the kind of each claim.
6. Classify the origin of each claim against the source variants.

Return JSON:
{{
  "claims": [
    {{
      "index": 1,
      "text": "The function retries transient errors.",
      "kind": "error_behavior",
      "modality": "universal",
      "origin": "inherited_paraphrase"
    }}
  ]
}}

Kind values: implementation_description, public_contract, invariant,
error_behavior, side_effect, ordering, concurrency, security, performance,
complexity, rationale, external_dependency, historical_statement, other.

Modality values: universal, conditional, existential, non_checkable.

Origin values: inherited_exact, inherited_paraphrase, inherited_narrowed,
inherited_strengthened, merged_from_sources, synthesized, origin_uncertain.
"""


def parse_atomized_claims(
    raw_response: str, lineage_id: str,
) -> list[Claim]:
    """Parse the atomizer's JSON response into Claim objects.

    Lenient: reuses the canonical JSON parser (handles small-model breakage).
    Falls back to deterministic kind/modality/origin classification when the
    model omits a field. Returns [] on unparseable input.
    """
    from capybase.adapters.parsers import parse_resolution_json
    data, _warns = parse_resolution_json(raw_response, layout="json_v6")
    if not isinstance(data, dict):
        return []
    claims_raw = data.get("claims", [])
    if not isinstance(claims_raw, list):
        return []
    out: list[Claim] = []
    for c in claims_raw:
        if not isinstance(c, dict):
            continue
        idx = c.get("index", len(out) + 1)
        text = str(c.get("text", "")).strip()
        if not text:
            continue
        kind = str(c.get("kind", ""))
        if kind not in CLAIM_KINDS:
            kind = detect_kind(text)
        modality = str(c.get("modality", ""))
        if modality not in CLAIM_MODALITIES:
            modality = detect_modality(text)
        origin = str(c.get("origin", ""))
        if origin not in CLAIM_ORIGINS:
            origin = "origin_uncertain"
        out.append(Claim(
            claim_id=f"{lineage_id}.{idx}",
            lineage_id=lineage_id,
            text=text,
            origin=origin,
            kind=kind,
            modality=modality,
        ))
    return out


__all__ = [
    "Claim",
    "CLAIM_KINDS",
    "CLAIM_MODALITIES",
    "CLAIM_ORIGINS",
    "detect_modality",
    "detect_kind",
    "classify_claim_origin",
    "build_atomize_prompt",
    "parse_atomized_claims",
]
