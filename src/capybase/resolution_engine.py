"""Resolution engine: candidate generator over the model adapter.

``propose`` returns a *list* of ``CandidateResolution`` even in the MVP
(samples=1) so that self-consistency is a parameter change rather than an
architectural one. Every prompt has a stable version string so prompt
versions can be compared in offline eval and recorded in training data.

Prompt versions::

    resolve_text_block.v1   — initial resolution request
    cegis_retry.v1          — retry with concrete validator feedback
"""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Iterable

from capybase.adapters.llm_openai import (
    LLMClient,
    LLMResponse,
    OpenAICompatibleClient,
    coerce_candidate_dict,
)
from capybase.conflict_model import (
    CandidateResolution,
    ConflictUnit,
    ContextBundle,
    TokenBudget,
    VerificationFailure,
    estimate_tokens,
)
from capybase.config import ModelConfig
from capybase.consensus import ConsensusReport, rank_by_consensus

PROMPT_RESOLVE = "resolve_text_block.v5"
PROMPT_RETRY = "cegis_retry.v5"
# Two-pass prompting (Step 2): intent extraction then code generation.
PROMPT_INTENT = "intent.v1"
PROMPT_CODE = "code_from_intent.v1"
# PlanSearch (survey §1): multi-plan sampling. Each candidate is tagged
# code_from_intent.v1#plan{i} so offline eval can attribute outcomes per plan.
PROMPT_PLAN = "plan_search.v1"
# Targeted repair (Step 4): send back the broken candidate for surgical fixing.
PROMPT_REPAIR = "cegis_repair.v1"
# Block-capture (large modify/delete): the model picks keep/accept_deletion/
# needs_human; the chosen side's text is spliced mechanically — the model never
# reproduces the (large) block, eliminating escaping + placeholder-collapse.
PROMPT_BLOCK_CAPTURE = "block_capture.v1"


def _prompt_sides(unit: ConflictUnit) -> tuple[str, str, str]:
    """Return the conflict sides to show in the prompt.

    Prefers the diff3-minimized sides (``unit.refined_sides``) so the model
    sees the smallest possible conflict window — adjacent non-conflicting lines
    that the worktree markers still wrap are stripped. Falls back to the raw
    marker sides when no refinement is recorded. Returns
    ``(current, base, replayed)``.
    """
    refined = unit.refined_sides
    if refined is not None:
        return refined
    return unit.current.text, unit.base.text, unit.replayed.text


def _side_intent_block(unit: ConflictUnit) -> str:
    """A short 'what each side DID' annotation + obligations contract for the prompt.

    Two parts, both pure and folded into the budget-protected ``sides_text``:

    1. The conflict-shape label (survey "silent loss of intent"): without it, a
       side that's empty because it DELETED base content reads as merely
       'absent', and the model can't tell a deliberate deletion from a missing
       side. Surfaces :func:`merge_intent.direction`'s summary right above the
       sides (e.g. 'CURRENT_UPSTREAM_SIDE DELETED this block; the replayed side
       kept it').
    2. The side-obligation contract (#3): a compact "must preserve" block per
       side (the load-bearing added/changed content) so the model knows exactly
       what each side's edit IS. Grounds the model in the diff-derived
       obligations rather than just the raw side text.

    Returns "" when neither is available (un-enriched unit, no obligations), so
    the prompt is unchanged. Pure; reads only metadata + the side texts.
    """
    parts: list[str] = []
    md = unit.structural_metadata.get("merge_direction") or {}
    summary = md.get("summary")
    if summary:
        parts.append(f"Conflict shape (what each side did vs BASE):\n{summary}")
    # Obligation contract: derive per-side load-bearing edits. Wrapped so a
    # failure degrades to "no block" (the prompt must never crash on obligations).
    try:
        from capybase.obligations import extract_obligations, render_obligation_block

        parts.append(render_obligation_block(extract_obligations(unit)).rstrip("\n"))
    except Exception:  # noqa: BLE001 - obligations are advisory, never break the prompt
        pass
    parts = [p for p in parts if p]
    if not parts:
        return ""
    return "\n\n".join(parts) + "\n\n"


def _structural_context_block(unit: ConflictUnit) -> str:
    """3-way structural context annotation for the LLM prompt (Improvement #6).

    Shows which structural units exist in the file, which unit the conflict
    falls inside, and — when the sides are full-file versions — what each side
    changed and which units must survive. Returns "" when there's no useful
    structural signal.

    **Multi-hunk handling**: for a multi-hunk conflict, ``unit.base.text`` is the
    whole merge-base file but ``unit.current.text``/``unit.replayed.text`` are
    just the narrow conflict-block fragments. A 3-way diff across these mismatched
    inputs produces garbage ("deleted by both" for every unit). So we detect the
    multi-hunk case (base substantially larger than the sides) and fall back to a
    FILE-STRUCTURE-ONLY annotation: parse the base, show the unit list, and
    highlight which unit the conflict is inside. This is the key signal for a
    multi-hunk merge — the model needs to know the file has struct Config, fn
    new, fn label, and its conflict is inside fn new (not fn label).
    """
    try:
        from capybase.adapters.abstract_parser import (
            compute_structural_diff_3way, render_structural_context,
            parse_file, enclosing_unit, _all_units_flat,
        )
        base_text = unit.base.text or ""
        current_text = unit.current.text or ""
        replayed_text = unit.replayed.text or ""
        if not base_text and not current_text and not replayed_text:
            return ""

        # Detect the multi-hunk case: base is substantially larger than the
        # sides (the sides are conflict-block fragments, not full files).
        base_len = len(base_text)
        side_max = max(len(current_text), len(replayed_text))
        is_multi_hunk = base_len > side_max * 3 and base_len > 200

        if is_multi_hunk:
            # File-structure-only: parse the base, show units + enclosing unit.
            ir = parse_file(base_text, language=unit.language)
            if ir is None or not ir.units:
                return ""
            flat = _all_units_flat(ir)
            if not flat:
                return ""
            lines = [f"STRUCTURAL CONTEXT (language-family: {unit.language or ir.family}/{ir.family}):"]
            # Show the file's unit inventory.
            unit_lines = []
            for u in flat:
                if u.name:
                    unit_lines.append(f"  [{u.kind.upper()}] {u.name} lines {u.span[0]+1}-{u.span[1]+1}")
            if not unit_lines:
                return ""
            lines.append("File structure:")
            lines.extend(unit_lines)
            # Highlight which unit this conflict falls inside.
            if unit.marker_span is not None:
                enc = enclosing_unit(ir, unit.marker_span)
                if enc and enc.name:
                    lines.append(f"This conflict is inside: {enc.kind.upper()} {enc.name}")
                    lines.append(
                        f"Required: preserve ALL units listed above in the merged output "
                        f"(the file has {len(flat)} structural unit(s))."
                    )
            return "\n".join(lines) + "\n\n"

        # Single-hunk (or full-file sides): compute the full 3-way diff.
        diff = compute_structural_diff_3way(
            base_text, current_text, replayed_text, language=unit.language,
        )
        if diff is None:
            return ""
        annotation = render_structural_context(diff, conflict_span=unit.marker_span)
        if not annotation:
            return ""
        return annotation + "\n\n"
    except Exception:  # noqa: BLE001 - advisory; never break the prompt
        return ""


def _semantic_change_block(unit: ConflictUnit) -> str:
    """A compact 'what each side changed at the ENTITY level' annotation.

    Deterministic (tree-sitter ``semantic_diff``): classifies each side's
    entity-level changes vs BASE as added / removed / renamed / signature_changed
    / body_changed, and renders a one-line-per-change summary. This gives the
    model PRECISE change intent — e.g. "CURRENT side renamed `validate_token`→
    `check_token`" — that the raw side text + obligation lines convey only
    implicitly. The survey's Tier 5 finding: surfacing structured change types
    lifts a small LLM's merge quality by removing guesswork about what each side
    is doing.

    Folded into the budget-protected core alongside the side-intent block: it's
    short (a few lines), high-value, and directly helps the model read the sides
    it must merge. Returns "" when tree-sitter is unavailable, the language isn't
    supported, or neither side made an entity-level change (degrades gracefully).
    Pure; reads only the side texts.
    """
    lang = unit.language or ""
    if lang not in ("python", "rust"):
        return ""
    try:
        from capybase.adapters import structural
    except Exception:  # noqa: BLE001
        return ""
    if not structural.is_available(lang):
        return ""
    base = unit.base.text or ""
    cur = unit.current.text or ""
    rep = unit.replayed.text or ""
    try:
        cur_changes = structural.semantic_diff(base, cur, lang)
        rep_changes = structural.semantic_diff(base, rep, lang)
        # The replayed commit's semantic role (survey §5.2): tells the model what
        # "correct" means for this commit (bugfix = preserve behavior; feature =
        # new behavior acceptable; refactor = behavior-preserving). Read off the
        # feature spine when present; compute live as a pure fallback.
        cf = unit.structural_metadata.get("conflict_features")
        role = cf.get("commit_change_type") if isinstance(cf, dict) else None
        if not role:
            role = structural.classify_commit_change(base, rep, unit.path, lang)
        guidance = structural.COMMIT_ROLE_GUIDANCE.get(role)
    except Exception:  # noqa: BLE001 - advisory, never break the prompt
        return ""
    has_changes = bool(cur_changes or rep_changes)
    has_role = bool(guidance) and role != "unknown"
    if not has_changes and not has_role:
        return ""
    lines = []
    if has_changes:
        lines.append("Entity-level changes vs BASE (deterministic — use these to read the sides):")
        if cur_changes:
            lines.append("  CURRENT side: " + "; ".join(c.render() for c in cur_changes))
        if rep_changes:
            lines.append("  REPLAYED side: " + "; ".join(c.render() for c in rep_changes))
    if has_role:
        # Surface the commit role + its correctness guidance so the model knows
        # what this merge must satisfy (e.g. a bugfix must preserve behavior).
        lines.append(f"REPLAYED commit role: {role} — {guidance}")
    return "\n".join(lines) + "\n\n"


#: Guidance surfaced to the model when the conflict is a value resolution (both
#: sides preserved the same statement shape and only a value diverged). Tells the
#: model that picking either side OR writing a new combining expression are both
#: acceptable — so it doesn't self-report ``needs_human`` on a resolvable value
#: conflict (the failure mode where a reasoning model concludes "two return
#: values can't both be preserved" and gives up).
_VALUE_RESOLUTION_GUIDANCE = (
    "VALUE-RESOLUTION conflict: both sides preserved the same {kind} "
    "({target}) and only the value/expression diverged. A correct merge PRESERVES\n"
    "the {kind} and resolves the value. Either is acceptable:\n"
    "  - pick one side's value (the operation is preserved; either is correct), OR\n"
    "  - write a new expression combining both values when that is meaningful for\n"
    "    the operation (e.g. a concatenation, sum, tuple). Prefer this only when the\n"
    "    combination is semantically sound for the surrounding code.\n"
    "Do NOT report needs_human merely because both literal values cannot coexist on\n"
    "one line — picking one side is a valid resolution here.\n\n"
)


def _value_resolution_block(unit: ConflictUnit) -> str:
    """Surface value-resolution guidance in the resolve prompt.

    When the conflict is a value resolution (both sides preserved the same
    ``return`` / assignment target and only the value diverged), tells the model
    that picking either side OR writing a new combining expression is acceptable.
    This counters the failure mode where a reasoning model sees two mutually-
    exclusive values, concludes "they can't both be preserved," and self-reports
    ``needs_human`` on a conflict that has a perfectly valid one-sided resolution.

    Reads the ``value_resolution`` feature off the conflict-features spine
    (computed at extraction). Returns "" when the conflict is NOT a value
    resolution (the guidance doesn't apply). Pure; never breaks the prompt.
    """
    cf = unit.structural_metadata.get("conflict_features")
    if not isinstance(cf, dict):
        return ""
    vr = cf.get("value_resolution")
    if not vr:
        return ""
    # vr is "return" / "assignment:<target>" / "augassign:<target>".
    kind = vr.split(":", 1)[0]
    target = vr.split(":", 1)[1] if ":" in vr else ""
    target_desc = f"target `{target}`" if target else "return statement"
    return _VALUE_RESOLUTION_GUIDANCE.format(kind=kind, target=target_desc)


def _fit_to_budget(
    *,
    budget: TokenBudget | None,
    intro: str,
    contract: str,
    rules: str,
    sides_text: str,
    structural_anchor: str,
    siblings_block: str,
    deps: str,
    few_shot: str,
    primary_text: str,
    history: str = "",
    obligations: str = "",
) -> tuple[str, str, str, str, str, str, str, list[dict]]:
    """Trim the prompt's AUGMENTATION sections to fit ``budget``, protecting the
    essential conflict sides + the JSON contract.

    The essential content (``intro``, ``contract``, ``rules`` — the fixed JSON
    schema boilerplate — and ``sides_text`` — the three conflict sides) is NEVER
    trimmed. The augmentations are trimmed in priority order (lowest-value
    first) until the assembled prompt fits the budget's ``available`` tokens, or
    all augmentations are exhausted:

    1. ``history`` (replay-position facts) — dropped wholesale. [lowest priority]
    2. ``primary_text`` (surrounding file context) — truncated, 1-line floor.
    3. ``few_shot`` (similar-past-merge examples) — dropped wholesale.
    4. ``deps`` (cross-file dependency snippets) — dropped wholesale.
    5. ``siblings_block`` — dropped.
    6. ``structural_anchor`` (the enclosing AST node text) — dropped.
    7. ``obligations`` (future obligations + branch intent) — dropped LAST.

    This ordering (#idea 9) ensures history-critical information (what later
    commits expect of the resolution) survives trimming before generic context
    (few-shot, surrounding code). Small local models are sensitive to prompt
    noise, so the highest-value signal stays longest.

    Returns ``(anchor, siblings, deps, few_shot, primary_text, history, obligations, trims)``.
    When ``budget`` is None/disabled, all sections pass through unchanged.
    """
    trims: list[dict] = []
    # No budget / disabled → unbounded (current behavior).
    if budget is None or not budget.enabled:
        return (structural_anchor, siblings_block, deps, few_shot,
                primary_text, history, obligations, trims)

    # System message is a fixed ~12 tokens; account for it once.
    system_tokens = 12
    overhead = estimate_tokens(intro + contract + rules) + system_tokens
    essential = estimate_tokens(sides_text)
    available_for_augmentation = budget.available - overhead - essential

    # If the essential content alone blows the window, drop ALL augmentations
    # and flag it. The sides still go (protect-the-conflict policy).
    if available_for_augmentation <= 0:
        if structural_anchor or siblings_block or deps or few_shot or primary_text:
            trims.append({
                "section": "all_augmentations",
                "detail": (
                    f"essential content ({essential}t) + overhead ({overhead}t) "
                    f"already meets/exceeds window {budget.total}t; dropped all "
                    f"augmentation sections (sides protected)"
                ),
            })
        return "", "", "", "", "", "", "", trims

    # Otherwise fit the augmentations in. We measure the running token total of
    # the augmentation sections and trim lowest-value-first until it fits.
    anchor = structural_anchor
    siblings = siblings_block
    dep_block = deps
    shot = few_shot
    primary = primary_text
    hist = history
    obls = obligations

    def _aug_tokens() -> int:
        return estimate_tokens(anchor + siblings + dep_block + shot + primary + hist + obls)

    # 1. Drop history context (lowest value — nice-to-have replay facts).
    if _aug_tokens() > available_for_augmentation and hist:
        hist = ""
        trims.append({"section": "history", "detail": "dropped history context"})
    # 2. Truncate surrounding context (primary_text) to the lines nearest the
    #    conflict. Keep at least 1 line so the model has SOME surrounding frame.
    if _aug_tokens() > available_for_augmentation and primary:
        plines = primary.split("\n")
        kept = len(plines)
        while kept > 1 and estimate_tokens(anchor + siblings + dep_block + shot + hist + obls + "\n".join(plines[:kept])) > available_for_augmentation:
            kept -= 1
        primary = "\n".join(plines[:kept])
        trims.append({
            "section": "primary_text",
            "detail": f"truncated surrounding context to {kept}/{len(plines)} lines",
        })
    # 3. Drop few-shot examples.
    if _aug_tokens() > available_for_augmentation and shot:
        shot = ""
        trims.append({"section": "few_shot", "detail": "dropped similar-past-merge examples"})
    # 4. Drop cross-file deps.
    if _aug_tokens() > available_for_augmentation and dep_block:
        dep_block = ""
        trims.append({"section": "deps", "detail": "dropped cross-file dependency snippets"})
    # 5. Drop sibling entities.
    if _aug_tokens() > available_for_augmentation and siblings:
        siblings = ""
        trims.append({"section": "siblings", "detail": "dropped sibling entity signatures"})
    # 6. Drop the enclosing-node text of the structural anchor.
    if _aug_tokens() > available_for_augmentation and anchor:
        anchor = ""
        trims.append({"section": "structural_anchor", "detail": "dropped enclosing AST node text"})
    # 7. Drop obligations LAST (#idea 9) — history-critical info (what later
    #    commits expect of the resolution) survives trimming before generic
    #    context. This is the highest-priority augmentation.
    if _aug_tokens() > available_for_augmentation and obls:
        obls = ""
        trims.append({"section": "obligations", "detail": "dropped future obligations + branch intent"})

    return anchor, siblings, dep_block, shot, primary, hist, obls, trims


def _resolve_prompt_parts(
    unit: ConflictUnit,
    context: ContextBundle,
    budget: TokenBudget | None = None,
):
    """Build the reusable building blocks of the resolve prompt.

    The resolve prompt is composed of stable parts (intro, sides, contract,
    rules) so that prompt *variants* (``build_resolve_prompt_variants``) can
    re-order or re-frame them without re-deriving the data — guaranteeing the
    spliced-resolved_text contract is invariant across variants. Returns a dict
    of named string fragments plus the already-rendered baseline sections.

    ``budget`` (when enabled) caps the prompt to the model's context window:
    augmentation sections (few-shot, deps, anchor, surrounding context) are
    trimmed to fit, protecting the three sides + JSON contract. The trims are
    returned as ``trims`` for the caller to journal. When ``budget`` is None or
    disabled, this is a no-op (current behavior).
    """
    cur_lines, base_lines, rep_lines = _prompt_sides(unit)
    sv = context.structural_view
    enc_sig = sv.get("enclosing_node_signature") if sv else None
    enc_text = sv.get("enclosing_node_text") if sv else None
    structural_anchor = ""
    if enc_sig and enc_text:
        structural_anchor = f"""Logical block you are merging inside (tree-sitter AST):
{enc_sig}
{enc_text}

"""
    # Sibling entities (survey §4.1/§5.4 Rover): the OTHER methods/fields in the
    # same container — the entity neighborhood the merged result must stay
    # consistent with (shared conventions, in-file callers/callees). Signatures
    # only, to stay cheap; the survey's finding that *some* structured context
    # lifts a small LLM's output. Empty when no siblings were resolved.
    siblings_block = ""
    if sv and sv.get("sibling_entities"):
        joined = "\n".join(f"  - {sig}" for sig in sv["sibling_entities"])
        siblings_block = f"Other entities in this container (stay consistent with these):\n{joined}\n\n"
    few_shot = ""
    if context.retrieved_examples:
        blocks = []
        for i, ex in enumerate(context.retrieved_examples, 1):
            blocks.append(
                f"Example {i}:\n"
                f"  CURRENT: {ex.current}\n"
                f"  REPLAYED: {ex.replayed}\n"
                f"  RESOLVED: {ex.resolved}"
            )
        few_shot = "Similar past merges (for reference — match this style):\n" + "\n".join(blocks) + "\n\n"
    # Cross-file dependency neighborhood (survey §5.3 Rover): definitions of
    # symbols the conflict code references that live OUTSIDE the enclosing
    # block. The merged result must stay consistent with these; showing them
    # prevents the model from guessing at a helper's signature/behavior. Empty
    # when no external dependencies were resolvable.
    deps = ""
    if context.related_snippets:
        blocks = []
        for i, snip in enumerate(context.related_snippets, 1):
            blocks.append(f"[{i}] {snip.path} — {snip.reason}:\n{snip.text}")
        deps = "Definitions this conflict depends on (merged result must be consistent with these):\n" + "\n".join(blocks) + "\n\n"
    # History-aware context (#history step 7): compact replay-position + future-
    # commit relevance facts. Empty string when no history (the block is omitted).
    history = ""
    if context.history_context:
        history = f"History context:\n{context.history_context}\n\n"
    # High-priority obligations context (#idea 9): future obligations + branch
    # intent, a first-class budget section that trims AFTER structural context.
    obligations = ""
    if context.obligations_context:
        obligations = f"{context.obligations_context}\n\n"
    intro = (
        "Resolve ONE git merge conflict by merging BOTH sides into one coherent\n"
        "result preserving each side's intent. Be CONCISE: reason in a few sentences,\n"
        "then answer. Do not over-explain.\n\n"
        f"file: {unit.path}\n"
        f"language: {unit.language or 'unknown'}\n\n"
    )
    contract = (
        "Your resolved_text REPLACES the whole conflict marker block (``<<<<<<<``\n"
        "through ``>>>>>>>``) and is spliced in verbatim. End with ONE ```json fenced\n"
        "object having EXACTLY these keys:\n\n"
        "```json\n"
        "{\n"
        '  "resolved_text": "<merged replacement text>",\n'
        '  "current_side_intent": ["..."],\n'
        '  "replayed_commit_intent": ["..."],\n'
        '  "preserved_current_side": true,\n'
        '  "preserved_replayed_commit_side": true,\n'
        '  "dropped_current_side_details": [],\n'
        '  "dropped_replayed_commit_side_details": [],\n'
        '  "assumptions": [],\n'
        '  "needs_human": false,\n'
        '  "self_reported_confidence": 0.0,\n'
        '  "explanation": "one short sentence"\n'
        "}\n"
        "```\n\n"
    )
    rules = (
        "CRITICAL rules:\n"
        "- PRESERVE leading indentation. If the bodies start with 4 spaces, EVERY line\n"
        "  of resolved_text must start with 4 spaces. Getting this wrong causes a syntax\n"
        "  error and rejection.\n"
        "- No conflict markers (``<<<<<<<`` / ``=======`` / ``>>>>>>>``).\n"
        "- Do not add or change the enclosing def/class line.\n"
        "- Escape newlines as \\n and double quotes as \\\" inside resolved_text.\n"
        "- Output the ```json block last; nothing after it.\n"
        "- If you cannot merge safely, set needs_human=true and explain.\n"
    )
    # Token-window enforcement: the three sides + boilerplate (intro/contract/
    # rules) are ESSENTIAL and never trimmed; the augmentation sections (anchor,
    # siblings, deps, few-shot, surrounding context) are trimmed to fit. The
    # sides_text mirrors exactly what the data_block renders for the sides so
    # _fit_to_budget's "essential" accounting is accurate. The side-intent
    # annotation is also essential (it disambiguates a deletion from a missing
    # side — the model needs it to read the sides correctly), so it's folded
    # into sides_text for the budget accounting. The entity-level semantic-change
    # summary is folded in too (same reason — precise change intent helps the
    # model read the sides; short + high-value).
    side_intent = _side_intent_block(unit)
    semantic_change = _semantic_change_block(unit)
    value_resolution = _value_resolution_block(unit)
    # 3-way structural context annotation (Improvement #6): aligns the file's
    # structural units across base/left/right and renders a compact summary —
    # which units each side changed, whether there are structural conflicts,
    # and which units must survive the merge. Directly addresses the "dropped
    # replayed side" failure: the model sees unit boundaries explicitly.
    struct_ctx = _structural_context_block(unit)
    sides_text = (
        f"{struct_ctx}{side_intent}{semantic_change}{value_resolution}"
        f"CURRENT_UPSTREAM_SIDE body (exact, including leading spaces):\n{cur_lines}\n\n"
        f"REPLAYED_COMMIT_SIDE body (exact, including leading spaces):\n{rep_lines}\n\n"
        f"BASE (common ancestor) body, for context:\n{base_lines}\n\n"
    )
    anchor_t, siblings_t, deps_t, few_shot_t, primary_t, history_t, obls_t, trims = _fit_to_budget(
        budget=budget,
        intro=intro,
        contract=contract,
        rules=rules,
        sides_text=sides_text,
        structural_anchor=structural_anchor,
        siblings_block=siblings_block,
        deps=deps,
        few_shot=few_shot,
        primary_text=context.primary_text,
        history=history,
        obligations=obligations,
    )
    # The non-instruction sections (anchor, siblings, deps, obligations, history,
    # few-shot, three sides, context) form one contiguous block that variants keep
    # together. Obligations render early (high priority) so the model sees them.
    data_block = (
        f"{obls_t}{anchor_t}{siblings_t}{deps_t}{history_t}{few_shot_t}{struct_ctx}{side_intent}{semantic_change}{value_resolution}"
        f"CURRENT_UPSTREAM_SIDE body (exact, including leading spaces):\n{cur_lines}\n\n"
        f"REPLAYED_COMMIT_SIDE body (exact, including leading spaces):\n{rep_lines}\n\n"
        f"BASE (common ancestor) body, for context:\n{base_lines}\n\n"
        f"Surrounding file context:\n{primary_t}\n\n"
    )
    return {"intro": intro, "data": data_block, "contract": contract, "rules": rules, "trims": trims}


def build_resolve_prompt(
    unit: ConflictUnit,
    context: ContextBundle,
    budget: TokenBudget | None = None,
) -> str:
    """The baseline resolve prompt.

    Composes the canonical part ordering (intro → data → contract → rules) from
    ``_resolve_prompt_parts``. Prompt variants (``build_resolve_prompt_variants``)
    re-use these exact parts so the spliced-output contract is identical across
    phrasings. ``budget`` (when enabled) trims augmentation sections to fit the
    model's context window; disabled/None is a no-op (current behavior).
    """
    p = _resolve_prompt_parts(unit, context, budget=budget)
    return p["intro"] + p["data"] + p["contract"] + p["rules"]


def build_block_capture_prompt(unit: ConflictUnit, context: ContextBundle) -> str:
    """The block-capture decision prompt for a large modify/delete conflict.

    Instead of asking the model to REPRODUCE the (large) kept block as an escaped
    JSON string — which fails on big blocks (placeholder collapse: the model writes
    '// ... unchanged ...' instead of the real content; and escaping corruption:
    mixed real/literal ``\\n`` that breaks the splice) — this asks a small
    DECISION question. The model picks one of:

      - ``accept_deletion`` — the deletion should stand (the deleting side wins).
        capybase splices the deleting side's text (usually empty).
      - ``keep_block`` — the kept block should survive. capybase splices the
        keeper side's text VERBATIM, taken directly from the conflict side (never
        reproduced by the model — so no escaping, no truncation).
      - ``needs_human`` — escalate; neither option is clearly right.

    The prompt shows the disambiguation + a rich SUMMARY of the keeper (entity
    signatures — test/function names — plus first/last lines), not the full text.
    The entity names are the signal the model needs to judge "is this dead code
    or live": a block of ``#[test] fn brace_balance_*`` tests deleted by a
    "consolidate(tests)" commit reads very differently from dead ``fn old_impl``
    helpers. The actual block text always comes from the real conflict sides, so
    escaping and truncation are structurally impossible.
    """
    md = unit.structural_metadata.get("merge_direction") or {}
    summary = md.get("summary", "a modify/delete conflict")
    who = md.get("deleting_side")  # "current" | "replayed" | None
    # The keeper side = the side that did NOT delete. Its text is what we'd splice
    # on "keep_block"; we show a summary, never the full block.
    if who == "current":
        keeper_text = unit.replayed.text or ""
        keeper_label = "REPLAYED_COMMIT_SIDE"
    else:
        keeper_text = unit.current.text or ""
        keeper_label = "CURRENT_UPSTREAM_SIDE"
    keeper_lines = keeper_text.split("\n")
    keeper_n = len(keeper_lines)

    # Entity signatures: the test/function/struct names in the block. These are
    # the load-bearing signal for a keep-vs-delete decision — far more useful than
    # first/last lines for a 400-line block. Extracted cheaply by regex (no parser
    # needed): test names, fn defs, struct/enum/trait/impl headers.
    sigs = _extract_signatures(keeper_text)

    # The deleting commit's subject (why the block was removed). This is critical
    # context: "consolidate(tests): remove 44 verbose tests" tells the model the
    # deletion was a deliberate cleanup (the tests may be redundant); "remove dead
    # fn old_impl" tells it the block was dead. Sourced from provenance metadata.
    deleting_commit = ""
    prov = unit.structural_metadata.get("provenance") or {}
    deleter_key = who  # "current" | "replayed"
    deleter_prov = prov.get(deleter_key) or {}
    deleting_commit = deleter_prov.get("subject") or ""

    # Summary = signatures + first/last lines. Signatures lead because they're the
    # decision signal; the line window adds surrounding context.
    def _summarize(lines: list[str], head: int = 4, tail: int = 4) -> str:
        nonblank = [ln for ln in lines if ln.strip()]
        if len(nonblank) <= head + tail:
            return "\n".join(nonblank) or "(empty)"
        shown = nonblank[:head] + ["    ... [{} lines elided] ...".format(
            len(nonblank) - head - tail
        )] + nonblank[-tail:]
        return "\n".join(shown)
    keeper_window = _summarize(keeper_lines)

    sig_block = ""
    if sigs:
        sig_list = "\n".join(f"  - {s}" for s in sigs[:40])
        sig_block = (
            f"Entities in the KEPT block ({len(sigs)} total — these are what the "
            f"block IS; judge keep-vs-delete from them):\n{sig_list}\n\n"
        )
    commit_block = ""
    if deleting_commit:
        commit_block = (
            f"The DELETING commit (why the block was removed): "
            f"`{deleting_commit}`\n\n"
        )

    return f"""You are resolving a git merge conflict. Do NOT rewrite or reproduce the
code — you are making a DECISION, and capybase splices the chosen text verbatim.

file: {unit.path}
language: {unit.language or 'unknown'}

Conflict shape:
{summary}

This is a modify/delete: one side DELETED a block of {keeper_n} lines, the other
side ({keeper_label}) KEPT it. You must decide which intent wins. Do not attempt
to merge line-by-line — choose one of the three options below.

{commit_block}{sig_block}Window into the KEPT block ({keeper_label}, {keeper_n} lines — first/last
non-blank lines; capybase has the full text):
```
{keeper_window}
```

Decide ONE:
- "accept_deletion" — the deletion should stand. Use this when the removed code
  is dead/obsolete/superseded, OR when the deleting commit is a deliberate
  consolidation (e.g. the same coverage now lives in parameterized tests). capybase
  splices the deleting side's text.
- "keep_block" — the kept block must survive. Use this when the block is live
  coverage/functionality not duplicated elsewhere. capybase splices the kept block
  VERBATIM from the conflict side.
- "needs_human" — genuinely ambiguous (you can't tell if the coverage is
  duplicated); escalate.

Answer with a single ```json fenced object, nothing else:
```json
{{
  "decision": "accept_deletion" | "keep_block" | "needs_human",
  "reason": "one short sentence referencing the entities/commit"
}}
```
"""


# ---------------------------------------------------------------------------
# Signature extraction for the block-capture summary
# ---------------------------------------------------------------------------


def _extract_signatures(text: str) -> list[str]:
    """The named entities (tests/functions/structs) in ``text``, in order.

    Used to summarize a large block for the keep-vs-delete decision: the entity
    names are the signal (``brace_balance_passes`` vs ``old_dead_helper``). Each
    match returns a labeled name like ``test: brace_balance_passes`` so the model
    sees what kind of entity it is. Deduplicated, order-preserving. Empty for a
    block with no recognizable definitions (e.g. a config/data block).
    """
    import re

    seen: set[str] = set()
    out: list[str] = []
    # Run test patterns first so a test fn is labeled "test:" not "fn:".
    lines = text.split("\n")
    n = len(lines)
    for i, line in enumerate(lines):
        # Test attributes span two lines: ``#[test]\n    fn NAME``.
        if re.match(r"#\[\s*(test|tokio::test)\s*\]", line.strip()) and i + 1 < n:
            m = re.search(r"fn\s+(\w+)", lines[i + 1])
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                out.append(f"test: {m.group(1)}")
                continue
        # Plain definitions.
        for pat, label in (
            (r"^\s*(?:pub\s+)?fn\s+(\w+)", "fn"),
            (r"^\s*(?:pub\s+)?struct\s+(\w+)", "struct"),
            (r"^\s*(?:pub\s+)?enum\s+(\w+)", "enum"),
            (r"^\s*(?:pub\s+)?trait\s+(\w+)", "trait"),
            (r"^\s*(?:async\s+)?def\s+(\w+)", "def"),
            (r"^\s*class\s+(\w+)", "class"),
        ):
            m = re.match(pat, line)
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                out.append(f"{label}: {m.group(1)}")
                break
    return out


def parse_block_capture_decision(raw: str) -> tuple[str, str]:
    """Parse the block-capture model response into ``(decision, reason)``.

    ``decision`` is normalized to one of ``accept_deletion`` / ``keep_block`` /
    ``needs_human``. Any unparseable / unrecognized response defaults to
    ``needs_human`` (the safe fallback — block-capture never guesses; on any
    doubt it escalates). ``reason`` is the model's explanation, or "" .
    """
    # Reuse the JSON parser (tolerant of prose + fenced blocks) rather than the
    # candidate-dict aliaser (which targets resolved_text etc.).
    from capybase.adapters.parsers import parse_resolution_json

    data, _w = parse_resolution_json(raw)
    if not isinstance(data, dict):
        return "needs_human", ""
    decision = str(data.get("decision", "")).strip().lower().replace("-", "_")
    if decision not in ("accept_deletion", "keep_block", "needs_human"):
        return "needs_human", str(data.get("reason", "") or "")
    return decision, str(data.get("reason", "") or "")


# Variant tags are appended to the base prompt_version (e.g. "resolve_text_block.v5#v1")
# so offline eval can attribute outcomes to the phrasing — the seed data for any
# future prompt-optimization (survey §2 AOZPT) work. "" = baseline (no suffix).
PROMPT_VARIANT_TAGS: tuple[str, ...] = ("", "#v1", "#v2")


def build_resolve_prompt_variants(
    unit: ConflictUnit,
    context: ContextBundle,
    k: int = 3,
    budget: TokenBudget | None = None,
) -> list[tuple[str, str]]:
    """Return up to ``k`` semantically-equivalent resolve prompts (survey §4).

    Each entry is ``(prompt_text, variant_suffix)`` where ``variant_suffix`` is
    one of ``PROMPT_VARIANT_TAGS`` (``""`` for the baseline). The variants are
    *deterministic transforms* of the baseline's parts (``_resolve_prompt_parts``),
    NOT hand-rewritten templates — so every variant carries the identical three
    sides, structural anchor, JSON contract block, and CRITICAL rules. Only the
    *ordering and framing* of those parts differ:

    - ``""``  (v0): the baseline ordering (intro → data → contract → rules).
    - ``#v1`` (constraint-first): contract + rules BEFORE the data, so the model
      reads the output contract before the sides. Tests whether stating the
      constraint first improves faithfulness to the JSON/splice contract.
    - ``#v2`` (minimal-diff priming): the baseline ordering with a one-line
      steer prepended — "Prefer the smallest change that merges both intents;
      do not reformat surrounding lines." The minimal-diff coder persona as a
      sentence, not a separate template.

    ``k`` clamps to the number of available variants. This is the Code-Roulette
    robustness lever: correct merges tend to be stable across these phrasings,
    while incorrect logic is brittle, so the consensus cluster that survives
    multiple variants is a stronger correctness signal than any single sample.
    ``budget`` (when enabled) trims augmentation sections; disabled/None is a
    no-op.
    """
    p = _resolve_prompt_parts(unit, context, budget=budget)
    intro, data, contract, rules = p["intro"], p["data"], p["contract"], p["rules"]
    variants: list[tuple[str, str]] = [
        (intro + data + contract + rules, ""),                      # v0 baseline
        (intro + contract + rules + data, "#v1"),                   # constraint-first
        (_MINIMAL_DIFF_STEER + intro + data + contract + rules, "#v2"),  # minimal-diff
    ]
    if k < len(variants):
        variants = variants[: max(1, k)]
    return variants


_MINIMAL_DIFF_STEER = (
    "Prefer the smallest change that merges both intents; do not reformat "
    "surrounding lines.\n\n"
)


# ---------------------------------------------------------------------------
# Outline-first prompt variants (small-model experiment)
# ---------------------------------------------------------------------------
#
# A family of resolve-prompt framings that state the conflict's structure TWICE:
# first as a compact abstract OUTLINE (what each side wants, one line each),
# then in full detail (the exact code sides). The hypothesis: a small (1B)
# model reasons better when it first sees the task summarized — the outline
# primes the merge intent before the literal text demands token-by-token
# copying. The full detail block is identical to the baseline (same sides,
# JSON contract, rules), so the spliced-output contract is invariant.
#
# Each variant changes the OUTLINE's phrasing/ordering to probe which summary
# style helps a weak model most. Selection is via :func:`set_outline_variant`
# (driven by CAPYBASE_PROMPT_VARIANT in live_eval); the default (None/0) is the
# baseline prompt with NO outline — identical to production.

#: The active outline variant, or None for the baseline prompt. Set via
#: :func:`set_outline_variant`. 0/None = baseline; 1-5 = the outline framings.
_OUTLINE_VARIANT: int | None = None

#: Variant suffixes recorded on candidate.prompt_version for attribution.
_OUTLINE_VARIANT_TAGS = {
    1: "#outline.v1",
    2: "#outline.v2",
    3: "#outline.v3",
    4: "#outline.v4",
    5: "#outline.v5",
}


def set_outline_variant(variant: int | None) -> None:
    """Select the outline-first prompt variant process-wide.

    ``None`` or ``0`` restores the baseline prompt (no outline). ``1``-``5``
    select one of the outline framings. Used by the live eval to A/B outline-
    first prompts against a small model.
    """
    global _OUTLINE_VARIANT
    _OUTLINE_VARIANT = variant if (variant is None or variant in _OUTLINE_VARIANT_TAGS) else None


def get_outline_variant() -> int | None:
    """The active outline variant (None = baseline)."""
    return _OUTLINE_VARIANT


def _side_outline_lines(unit: ConflictUnit) -> tuple[str, str, str]:
    """One-line abstract summaries of the CURRENT / REPLAYED / BASE sides.

    The outline is built from the side-intent annotations (the structural
    metadata) when available, falling back to a first-line / line-count digest.
    These are deliberately COARSE — the full detail follows later, so the
    outline's job is to convey intent, not exact code.
    """
    cur = (unit.current.text or "").strip()
    rep = (unit.replayed.text or "").strip()
    base = (unit.base.text or "").strip()

    def head(t: str) -> str:
        if not t:
            return "(empty)"
        first = t.split("\n", 1)[0].strip()
        return first[:80] + ("…" if len(first) > 80 else "")

    return head(cur), head(rep), head(base)


def build_outline_resolve_prompt(
    unit: ConflictUnit,
    context: ContextBundle,
    budget: "TokenBudget | None" = None,
) -> tuple[str, str]:
    """Build the resolve prompt under the active outline variant.

    Returns ``(prompt_text, variant_tag)``. ``variant_tag`` is "" for the
    baseline and ``"#outline.vN"`` otherwise — appended to the candidate's
    ``prompt_version`` so the journal records which framing produced it.

    When the active variant is None/0, this is byte-identical to
    :func:`build_resolve_prompt`. The outline variants reuse the baseline's
    parts (intro/data/contract/rules from ``_resolve_prompt_parts``) — they only
    PREPEND a summary outline and adjust the intro's framing, so the full detail
    and the JSON contract are invariant across variants.
    """
    variant = _OUTLINE_VARIANT
    p = _resolve_prompt_parts(unit, context, budget=budget)
    intro, data, contract, rules = p["intro"], p["data"], p["contract"], p["rules"]
    if not variant:
        return intro + data + contract + rules, ""

    cur_h, rep_h, base_h = _side_outline_lines(unit)
    tag = _OUTLINE_VARIANT_TAGS[variant]

    # Shared header explaining the two-pass structure (outline then detail).
    outline_intro = (
        "Resolve ONE git merge conflict. This prompt shows the problem TWICE:\n"
        "first as a SHORT OUTLINE (what each side wants), then in FULL DETAIL\n"
        "(the exact code). Read the outline first to understand the goal, then\n"
        "use the full detail to write the exact merged text.\n\n"
        f"file: {unit.path}\n"
        f"language: {unit.language or 'unknown'}\n\n"
    )

    if variant == 1:
        # v1 — plain intent outline: one line per side, "goal" framing.
        outline = (
            "=== OUTLINE (read this first) ===\n"
            f"Goal: merge BOTH sides into one coherent result; keep each side's intent.\n"
            f"CURRENT (upstream) wants: {cur_h}\n"
            f"REPLAYED (commit being applied) wants: {rep_h}\n"
            f"BASE (common ancestor): {base_h}\n"
            "Both sides must be represented in the result.\n\n"
        )
    elif variant == 2:
        # v2 — change-relative outline: what CHANGED from base on each side.
        outline = (
            "=== OUTLINE (read this first) ===\n"
            "Two branches each changed the same region from a shared BASE. Your job is\n"
            "to combine both changes.\n"
            f"- BASE:              {base_h}\n"
            f"- CURRENT changed it to: {cur_h}\n"
            f"- REPLAYED changed it to: {rep_h}\n"
            "The result must include BOTH branches' changes, not pick one.\n\n"
        )
    elif variant == 3:
        # v3 — checklist outline: explicit "must include" items, then detail.
        outline = (
            "=== OUTLINE (read this first) ===\n"
            "Before writing the answer, confirm your merge will:\n"
            f"  [ ] include CURRENT's change: {cur_h}\n"
            f"  [ ] include REPLAYED's change: {rep_h}\n"
            f"  [ ] not lose either side (BASE was: {base_h})\n"
            "Both checkmarks must be satisfied.\n\n"
        )
    elif variant == 4:
        # v4 — role + outline: "you are merging two branches", then intent lines.
        outline = (
            "=== OUTLINE (read this first) ===\n"
            "You are merging two git branches that both edited the same code.\n"
            "CURRENT is the branch you are merging INTO; REPLAYED is the commit being\n"
            "applied. A correct merge contains BOTH edits.\n"
            f"  CURRENT  (merge into this): {cur_h}\n"
            f"  REPLAYED (apply this too):  {rep_h}\n"
            f"  BASE     (original):        {base_h}\n"
            "Do not drop either side's edit.\n\n"
        )
    else:
        # v5 — contrast outline: side-by-side one-liners emphasizing both must win.
        outline = (
            "=== OUTLINE (read this first) ===\n"
            "SIDE-BY-SIDE summary of the conflict:\n"
            f"  CURRENT  | {cur_h}\n"
            f"  REPLAYED | {rep_h}\n"
            f"  BASE     | {base_h}\n"
            "Rule: the merged result must contain CURRENT's intent AND REPLAYED's\n"
            "intent simultaneously. Neither side loses.\n\n"
        )

    # The full detail block: label it explicitly so the model knows the outline
    # is a summary and this is the authoritative source for exact text.
    detail_header = "=== FULL DETAIL (use this for the exact merged text) ===\n\n"
    prompt = outline_intro + outline + detail_header + data + contract + rules
    return prompt, tag


def build_retry_prompt(
    unit: ConflictUnit,
    context: ContextBundle,
    failures: Iterable[VerificationFailure],
    budget: TokenBudget | None = None,
) -> str:
    feedback = "\n".join(_render_failure(f) for f in failures) or "- (no specific failures reported)"
    inner = build_resolve_prompt(unit, context, budget=budget)
    return f"""Your previous merge attempt was rejected. Fix it.

{inner}

### validator feedback (previous attempt failed these checks)
{feedback}

Address every failure above; do not repeat the mistake. End with the ```json
fenced answer as instructed.
"""


PROMPT_RECOVERY = "cegis_recovery.v1"


def build_recovery_prompt(
    unit: ConflictUnit,
    context: ContextBundle,
    failures: Iterable[VerificationFailure] | None,
    budget: TokenBudget | None = None,
) -> str:
    """The recovery prompt for a model that self-reported needs_human (CEGIS loop).

    A reframed resolve for the case where the model GAVE UP. Distinct from
    build_retry_prompt (the candidate was syntactically wrong) and
    build_repair_prompt (the candidate had a specific fixable error): here the
    model refused entirely, so the prompt is restructured to:

    - Acknowledge the difficulty and reframe as a step-by-step task (a model
      that bailed on a zero-shot attempt often succeeds when walked through).
    - STRIP the ``needs_human=true`` escape hatch from the output schema — the
      recovery attempt must produce a merge, not another refusal. A genuine
      inability surfaces as a wrong/empty merge the validators catch.
    - Carry the prior failures (whatever caused the struggle) as concrete
      feedback so the model knows what to fix.

    Uses the same sides/structural anchor as the resolve prompt but with the
    recovery framing. Falls back to the standard resolve contract minus the
    needs_human field.
    """
    feedback = (
        "\n".join(_render_failure(f) for f in (failures or []))
        or "- (the previous attempt self-reported it could not merge; no specific validator failure)"
    )
    parts = _resolve_prompt_parts(unit, context, budget=budget or TokenBudget())
    # Recovery framing: the same DATA (the sides + structural anchor) as a fresh
    # resolve, but a CUSTOM contract+rules block. The standard contract/rules
    # mention needs_human (the escape hatch the recovery retry must strip so the
    # model produces a merge instead of repeating the refusal). A genuine
    # inability surfaces as a wrong/empty merge the validators catch.
    return f"""The previous merge attempt for this conflict reported it could not
resolve safely. This is a RETRY with a fresh approach — work through it step by
step. Most conflicts that seem impossible at first glance resolve once you
identify each side's DISTINCT change and combine them. Do NOT report that you
cannot merge; produce the best merge you can. If genuinely ambiguous, make the
most conservative choice that preserves both sides' additions.

{parts["data"]}
### context from the previous (failed) attempt
{feedback}

YOUR TASK — reason step by step:
1. What did CURRENT_UPSTREAM_SIDE change vs BASE? (one sentence)
2. What did REPLAYED_COMMIT_SIDE change vs BASE? (one sentence)
3. What is the smallest merge that keeps BOTH changes? (one sentence)
4. Emit that merge.

CRITICAL: PRESERVE leading indentation exactly. No conflict markers. Escape
newlines as \\n and double quotes as \\" inside resolved_text. Output the
```json block last; nothing after it.

Output ONE ```json fenced object — do NOT include a needs_human field (this is a
recovery attempt; you MUST produce a merge):
```json
{{
  "resolved_text": "<the merged replacement text, exact indentation>",
  "explanation": "<one short sentence>",
  "self_reported_confidence": 0.0
}}
```
"""


def build_repair_prompt(
    unit: ConflictUnit,
    context: ContextBundle,
    candidate: CandidateResolution,
    failures: Iterable[VerificationFailure],
    budget: TokenBudget | None = None,
) -> str:
    """Targeted repair: send the broken candidate back for surgical fixing.

    Unlike ``build_retry_prompt`` (full regeneration from scratch), this
    includes the previous attempt's ``resolved_text`` verbatim so the model can
    fix the specific error locally rather than re-deriving the whole merge. A
    3B model is highly capable of fixing its own minor errors (missing bracket,
    wrong indentation) when shown the exact code + the exact error.

    The repair prompt carries only the two sides + the candidate + feedback (no
    few-shot/deps/anchor), so ``budget`` is largely a no-op here — the sides
    and candidate are protected and never trimmed. Accepted for signature
    symmetry with the other prompt builders.

    Repair-path few-shot (embeddings survey §2): a SINGLE high-trust retrieved
    example (``context.repair_retrieved_examples``, top-1, quality-filtered) is
    surfaced as a one-shot anchor after the validator feedback. This is the A/B
    failure site where the model reproduces the same dropped-side merge across
    retries; one concrete prior resolution gives it a pattern to follow instead
    of regenerating the same mistake. Kept to top-1 (not top-k like fresh-gen)
    so the surgical-fix signal on the broken candidate is not diluted. Empty
    when no retriever, the corpus is too small, or nothing clears the stricter
    filter — the block is omitted, preserving the prior behavior.
    """
    feedback = "\n".join(_render_failure(f) for f in failures) or "- (no specific failures reported)"
    cur_lines, _base_lines, rep_lines = _prompt_sides(unit)
    side_intent = _side_intent_block(unit)
    struct_ctx = _structural_context_block(unit)
    # Repair few-shot anchor (embeddings survey §2): top-1 quality-filtered
    # example. Rendered as a compact one-shot AFTER the feedback and BEFORE the
    # plan-first step so the model has a concrete resolution pattern in mind.
    repair_anchor = ""
    if context.repair_retrieved_examples:
        ex = context.repair_retrieved_examples[0]
        repair_anchor = (
            "A SIMILAR conflict was resolved correctly before (match this style for the fix):\n"
            f"  CURRENT: {ex.current}\n"
            f"  REPLAYED: {ex.replayed}\n"
            f"  RESOLVED: {ex.resolved}\n\n"
        )
    # Self-correction plan step (survey §3.3): force the model to reason about
    # WHY each failure happened and WHAT it will change BEFORE emitting the fix,
    # in the same response (no extra round-trip). The A/B showed the model
    # reproducing the same dropped-side merge across retries — it wasn't
    # internalizing the feedback. Articulating a concrete plan first ("restore
    # validate_token because the critic flagged it dropped") makes the
    # subsequent code far more likely to actually address the failure instead of
    # regenerating the same mistake. The plan is emitted in a `plan` field the
    # candidate parser ignores, so it doesn't change the resolved_text contract.
    return f"""Your previous merge attempt had errors. Fix the SPECIFIC errors in
your code below — do not rewrite from scratch unless necessary. Keep all parts
that were correct; change only what the validator flagged.

file: {unit.path}
language: {unit.language or 'unknown'}
{struct_ctx}{side_intent}
CURRENT_UPSTREAM_SIDE body:
{cur_lines}

REPLAYED_COMMIT_SIDE body:
{rep_lines}

YOUR PREVIOUS ATTEMPT (needs fixing):
{candidate.resolved_text}

### validator feedback (fix these specific issues)
{feedback}

{repair_anchor}FIRST, reason about the fix: for each failure above, state in one short sentence
WHY it happened and the specific edit you will make. Only AFTER you have a
concrete plan, emit the correction.

OUTPUT MODE — choose ONE:

(A) EDIT mode (preferred for small, targeted fixes): output a JSON object with an
"edits" field — a list of SEARCH/REPLACE blocks applied to YOUR PREVIOUS ATTEMPT
above. Each "search" MUST be a UNIQUE verbatim snippet copied from your previous
attempt (include enough surrounding context to be unique); "replace" is the
corrected version of that snippet. Only the snippets change; everything else is
kept as-is.
{{
  "plan": "<one sentence per failure: why + the fix>",
  "edits": [
    {{"search": "<exact verbatim snippet from your previous attempt>", "replace": "<corrected snippet>"}}
  ],
  "explanation": "<what you changed and why>",
  "self_reported_confidence": 0.0
}}

(B) FULL mode (for large rewrites): output the complete corrected replacement
text, exact indentation.
{{
  "plan": "<one sentence per failure: why + the fix>",
  "resolved_text": "<the full fixed replacement text, exact indentation>",
  "explanation": "<what you changed and why>",
  "self_reported_confidence": 0.0
}}

Prefer (A) EDIT mode when the fix is localized — it avoids re-deriving the whole
merge and risking a new error. Use (B) FULL mode only when the fix is pervasive.
"""


def _render_failure(f: VerificationFailure) -> str:
    """Render a failure richly, surfacing structured counterexample detail.

    Validators populate ``VerificationFailure.detail`` with structured state
    (e.g. the exact syntax-error line/column, the AST fingerprint diff, the
    LSP diagnostic range). Rendering it here gives the model a concrete
    counterexample to fix, rather than a bare message — this is the core of
    CEGIS: the counterexample guides the next synthesis attempt.
    """
    parts = [f"- [{f.validator}] {f.message}"]
    if f.detail:
        for key, val in f.detail.items():
            # Truncate long values (e.g. full AST fingerprints) to keep the
            # prompt focused on the actionable signal.
            sval = str(val)
            if len(sval) > 200:
                sval = sval[:200] + " …"
            parts.append(f"    {key}: {sval}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# SEARCH/REPLACE focused repair (§3): the model emits targeted edits against the
# previous attempt instead of reproducing the whole resolved_text. Applied here
# to produce the new candidate's resolved_text — downstream (verify/splice) sees
# the full applied text, unchanged. Graceful: a missed/malformed edit is skipped,
# never produces empty/garbage; worst case is "no change" (retry again).
# ---------------------------------------------------------------------------


def _norm_ws(text: str) -> str:
    """Whitespace-normalized form for fuzzy SEARCH matching (minor drift)."""
    import re as _re

    return _re.sub(r"\s+", " ", text).strip()


def apply_search_replace(
    prev_text: str, edits: list[dict]
) -> tuple[str, list[str]]:
    """Apply SEARCH/REPLACE blocks (the Aider/Cline format) to ``prev_text``.

    Each edit is ``{"search": <verbatim snippet>, "replace": <new text>}``. The
    ``search`` block is located in ``prev_text`` and replaced with ``replace``.
    This lets the focused-repair path fix the failing region without reproducing
    the whole resolved_text — the model emits a small targeted edit instead of
    re-deriving the entire merge (which risks introducing a NEW error).

    Matching is exact-substring first; on a miss, a whitespace-normalized match
    locates the span (tolerates minor formatting drift from a small model). Each
    edit applies to the result of the previous one, in order.

    Returns ``(new_text, warnings)``. A warning is recorded per edit whose
    ``search`` couldn't be located (that edit is skipped). When EVERY edit misses
    the caller falls back to the model's full ``resolved_text`` (full-repair
    mode) — so a bad edit payload degrades to today's behavior, never to
    empty/garbage. Empty/missing ``search`` or ``replace`` keys are skipped.
    """
    warnings: list[str] = []
    text = prev_text
    for i, edit in enumerate(edits):
        # Defensive: small models sometimes emit edits as strings instead of
        # {"search": ..., "replace": ...} objects (e.g. a bare code snippet).
        # Skip malformed entries with a warning so a bad edit degrades to the
        # full-resolved_text fallback rather than crashing the retry loop.
        if not isinstance(edit, dict):
            warnings.append(f"edit {i}: not an object ({type(edit).__name__}); skipped")
            continue
        search = str(edit.get("search", "")).rstrip("\n")
        replace = str(edit.get("replace", ""))
        if not search:
            warnings.append(f"edit {i}: empty search block; skipped")
            continue
        # Exact substring match (first occurrence).
        idx = text.find(search)
        if idx != -1:
            text = text[:idx] + replace + text[idx + len(search):]
            continue
        # Fuzzy: whitespace-normalized match — locates the span tolerating minor
        # formatting drift, then replaces the matched raw slice.
        norm_text = _norm_ws(text)
        norm_search = _norm_ws(search)
        nidx = norm_text.find(norm_search)
        if nidx != -1:
            # Map the normalized span back to the raw text by walking characters.
            raw_start = _denorm_index(text, nidx)
            raw_end = _denorm_index(text, nidx + len(norm_search))
            text = text[:raw_start] + replace + text[raw_end:]
            continue
        warnings.append(f"edit {i}: search block not found; skipped")
    return text, warnings


def _denorm_index(raw: str, norm_offset: int) -> int:
    """Map an offset in the whitespace-normalized form back to ``raw``.

    Walks ``raw``, collapsing runs of whitespace to a single space (matching
    ``_norm_ws``), until we've consumed ``norm_offset`` normalized characters.
    Returns the raw index at that point. Used to project a fuzzy-match span back
    onto the original text for replacement.
    """
    raw_i = 0
    norm_i = 0
    in_ws = False
    while raw_i < len(raw) and norm_i < norm_offset:
        ch = raw[raw_i]
        if ch.isspace():
            if not in_ws:
                norm_i += 1  # one space for the whole run
                in_ws = True
            raw_i += 1
        else:
            in_ws = False
            norm_i += 1
            raw_i += 1
    return raw_i


def _apply_repair_edits(
    cand: CandidateResolution, prev_candidate: CandidateResolution
) -> CandidateResolution:
    """If a repair candidate carries SEARCH/REPLACE edits, apply them to the
    previous attempt's resolved_text. Otherwise return it unchanged (full mode).

    Module-level (called from ``propose()``) so it stays out of the class body.
    The edits are stashed on the candidate as ``_repair_edits`` during
    construction (see ``_candidate_from_response``).
    """
    edits = getattr(cand, "_repair_edits", None)
    if not edits:
        return cand  # full mode (resolved_text already set) or no edits
    applied, warnings = apply_search_replace(prev_candidate.resolved_text, edits)
    if warnings and applied == prev_candidate.resolved_text:
        # All edits missed → fall back to the model's full resolved_text if it
        # provided one, else keep the previous (no-op retry).
        return cand
    cand.resolved_text = applied
    return cand


# ---------------------------------------------------------------------------
# Two-pass prompting (Step 2): intent extraction → code generation
# ---------------------------------------------------------------------------


def build_intent_prompt(unit: ConflictUnit, context: ContextBundle) -> str:
    """Pass 1: extract semantic intents only. No code generation.

    A 3B model reasons better when asked to *understand* the conflict before
    *fixing* it. This request is small and fast — it asks only for a JSON list
    of what each side changed. The result becomes a "reasoning map" that guides
    the code-generation pass.
    """
    cur_lines, base_lines, rep_lines = _prompt_sides(unit)
    return f"""Analyze this git merge conflict and state what EACH side changed
relative to the base. Output ONLY a JSON object with two string-list fields.
Do NOT write code.

file: {unit.path}
language: {unit.language or 'unknown'}

CURRENT_UPSTREAM_SIDE (stage 2):
{cur_lines}

REPLAYED_COMMIT_SIDE (stage 3):
{rep_lines}

BASE (common ancestor):
{base_lines}

Output this JSON (```json fenced):
{{
  "current_side_intent": ["what the upstream/current side changed", ...],
  "replayed_commit_intent": ["what the local/replayed side changed", ...]
}}
"""


def build_code_prompt(
    unit: ConflictUnit,
    context: ContextBundle,
    intents: dict[str, list[str]],
    plan: dict | None = None,
) -> str:
    """Pass 2: generate code conditioned on the extracted intent map.

    The model sees its own prior reasoning (the intents) and is asked to merge
    BOTH sides into one coherent result guided by that understanding. This is
    the same output schema as the single-pass resolve prompt, but the intent
    context primes the model toward a correct synthesis.

    ``plan`` (PlanSearch, survey §1): when given, a hard-constraint block is
    prepended telling the model to implement THAT plan exactly — turning Pass 2
    into "one code candidate per plan" instead of "N candidates from one plan".
    With no plan, the prompt is byte-identical to the original.
    """
    cur_intents = intents.get("current_side_intent", [])
    rep_intents = intents.get("replayed_commit_intent", [])
    cur_lines, _base_lines, rep_lines = _prompt_sides(unit)
    sv = context.structural_view
    enc_sig = sv.get("enclosing_node_signature") if sv else None
    structural_anchor = ""
    if enc_sig:
        structural_anchor = f"Merging inside: {enc_sig}\n\n"
    plan_block = ""
    if plan:
        steps = plan.get("steps", [])
        strategy = plan.get("strategy", "")
        step_text = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(steps))
        plan_block = (
            "Implement THIS plan exactly, producing a merge that satisfies each "
            "step and no additional changes:\n"
            f"Strategy: {strategy}\n"
            f"Steps:\n{step_text}\n\n"
        )
    return f"""{plan_block}Resolve ONE git merge conflict by merging BOTH sides into one
coherent result. Be CONCISE. A prior analysis identified these intents:

Upstream/current side changed:
{json.dumps(cur_intents, indent=2)}

Replayed/local side changed:
{json.dumps(rep_intents, indent=2)}

{structural_anchor}CURRENT_UPSTREAM_SIDE body (exact, including leading spaces):
{cur_lines}

REPLAYED_COMMIT_SIDE body (exact, including leading spaces):
{rep_lines}

Output a ```json fenced object:
{{
  "resolved_text": "<the merged replacement text, exact indentation>",
  "explanation": "<one sentence>",
  "self_reported_confidence": 0.0,
  "preserved_current_side": true,
  "preserved_replayed_commit_side": true
}}
"""


def build_plan_search_prompt(unit: ConflictUnit, context: ContextBundle) -> str:
    """Pass 1 for PlanSearch (survey §1): ask for MULTIPLE distinct plans.

    Combines the survey's "observation" and "plan generation" steps: the model
    lists the key constraints a correct merge must respect, then proposes K
    distinct resolution strategies as a JSON list. Pass 2 then generates one
    code candidate per plan (``build_code_prompt(plan=...)``), so solution
    diversity comes from the *planning* axis, not just temperature — orthogonal
    to prompt-variant and temperature-diverse sampling.
    """
    cur_lines, _base_lines, rep_lines = _prompt_sides(unit)
    sv = context.structural_view
    enc_sig = sv.get("enclosing_node_signature") if sv else None
    structural_anchor = ""
    if enc_sig:
        structural_anchor = f"Merging inside: {enc_sig}\n\n"
    return f"""Analyze ONE git merge conflict and propose DISTINCT resolution plans.

{structural_anchor}CURRENT_UPSTREAM_SIDE body (exact, including leading spaces):
{cur_lines}

REPLAYED_COMMIT_SIDE body (exact, including leading spaces):
{rep_lines}

First, list 3-5 key constraints a correct merge must respect (edge cases,
invariants, behaviors both sides rely on). Then propose 3 DISTINCT high-level
plans for resolving this conflict. Each plan must take a genuinely different
strategy (e.g. merge both behaviors; prefer one side with a guard for the
other; restructure to unify). Do NOT write the merged code — only the plan.

Output a ```json fenced object:
{{
  "constraints": ["...", "..."],
  "plans": [
    {{"strategy": "<one phrase>", "steps": ["step 1", "step 2"]}},
    {{"strategy": "<one phrase>", "steps": ["step 1", "step 2"]}},
    {{"strategy": "<one phrase>", "steps": ["step 1", "step 2"]}}
  ]
}}
"""


def parse_plans(text: str) -> list[dict] | None:
    """Parse a PlanSearch plan-search response into a list of plan dicts.

    Tolerant: reuses ``coerce_candidate_dict`` (handles fenced/prose-prefixed
    JSON). Returns the ``plans`` list, or ``None`` on any parse failure or if
    fewer than 2 distinct plans were produced (PlanSearch needs >=2 to add
    planning-axis diversity; otherwise it falls back to single-intent mode).
    """
    data, _warnings = coerce_candidate_dict(text)
    if not isinstance(data, dict):
        return None
    plans = data.get("plans")
    if not isinstance(plans, list) or len(plans) < 2:
        return None
    # Keep only well-formed plan dicts with at least a strategy or steps.
    cleaned = [p for p in plans if isinstance(p, dict) and (p.get("strategy") or p.get("steps"))]
    if len(cleaned) < 2:
        return None
    return cleaned


def build_verifier_prompt(
    unit: ConflictUnit,
    candidate: CandidateResolution,
    context: ContextBundle,
    *,
    assertion_enabled: bool = True,
) -> str:
    """Build the critic prompt for the verifier-model validator (surveys §1/§5).

    Asks the LLM to judge whether ``candidate.resolved_text`` preserves BOTH
    sides' intent — the semantic check no syntactic validator can make. The
    model sees BASE, CURRENT, REPLAYED, and the candidate RESOLVED, and must
    return strict JSON verdict booleans. This is purely a judging call on the
    same black-box API; it never edits code (the CEGIS loop does repairs).

    Phase 1 (critic guardrail): when ``assertion_enabled`` (default), injects a
    SYSTEM ASSERTION block with the deterministic preservation math so the critic
    doesn't hallucinate drops the AST disproves. Computed inline from the three
    sides + candidate via tree-sitter + the token-set check.
    """
    cur_lines, base_lines, rep_lines = _prompt_sides(unit)
    sv = context.structural_view
    enc_sig = sv.get("enclosing_node_signature") if sv else None
    enc_text = sv.get("enclosing_node_text") if sv else None
    structural_anchor = ""
    if enc_sig and enc_text:
        structural_anchor = (
            f"Logical block (tree-sitter AST):\n{enc_sig}\n{enc_text}\n\n"
        )
    # Phase 1 deterministic assertion (critic guardrail): inject the authoritative
    # preservation math so the critic doesn't hallucinate drops the AST disproves.
    # When unanimous, a directive (MUST NOT flag missing additions); when
    # imperfect, a pointer at the genuine gaps.
    assertion = ""
    if assertion_enabled:
        dp = _deterministic_preservation(unit, candidate, cur_lines, rep_lines, base_lines)
        assertion = _deterministic_assertion_block(dp)
    # Deterministic per-side preservation evidence (survey §5.1 quantitative):
    # the specific logical units each side ADDED that are ABSENT from the
    # resolution. Gives the judge concrete evidence to weigh, beyond eyeballing
    # the three sides — a unit listed here is very likely a dropped intent.
    evidence = _dropped_units_evidence(unit, candidate, cur_lines, rep_lines, base_lines)
    return f"""You are a strict code reviewer judging a git merge resolution. A merge
conflict has three sides and a proposed resolution. Judge ONLY whether the
resolution preserves the INTENT of BOTH sides — it must not silently drop a
behavior, guard, or value that either side added.

{assertion}{structural_anchor}{evidence}CURRENT_UPSTREAM_SIDE (one branch's change):
{cur_lines}

REPLAYED_COMMIT_SIDE (the other branch's change):
{rep_lines}

BASE (common ancestor):
{base_lines}

PROPOSED RESOLUTION:
{candidate.resolved_text}

Does the resolution preserve each side's intent? A side is "not preserved"
only if a meaningful change it introduced is absent from the resolution
(ignore cosmetic reformatting and unchanged surrounding lines). Output ONE
```json fenced object, nothing else:
```json
{{
  "preserves_current": true,
  "preserves_replayed": true,
  "reason": "<one short sentence>",
  "confidence": 0.0
}}
```
"""


def _dropped_units_evidence(
    unit: ConflictUnit,
    candidate: CandidateResolution,
    cur_lines: str,
    rep_lines: str,
    base_lines: str,
) -> str:
    """A deterministic 'units this side appears to have dropped' note for the
    critic prompt, computed from the three sides + candidate via tree-sitter.

    Empty string when tree-sitter is unavailable or no structural entities were
    dropped (the judge then falls back to eyeballing the sides). Lists the
    specific (kind, name) units missing from the resolution so the judge weighs
    concrete evidence rather than guessing.
    """
    lang = unit.language
    if lang not in ("python", "rust"):
        return ""
    try:
        from capybase.adapters import structural
    except Exception:  # noqa: BLE001
        return ""
    if not structural.is_available(lang):
        return ""
    cur_dropped = structural.dropped_entities(base_lines, cur_lines, candidate.resolved_text, lang) or []
    rep_dropped = structural.dropped_entities(base_lines, rep_lines, candidate.resolved_text, lang) or []
    if not cur_dropped and not rep_dropped:
        return ""
    parts = ["Structural check (deterministic) — entities a side added that are ABSENT from the resolution:"]
    if cur_dropped:
        names = ", ".join(f"{e.kind} '{e.name}'" for e in cur_dropped)
        parts.append(f"  CURRENT side appears to drop: {names}")
    if rep_dropped:
        names = ", ".join(f"{e.kind} '{e.name}'" for e in rep_dropped)
        parts.append(f"  REPLAYED side appears to drop: {names}")
    parts.append("(Verify these are genuine intent drops, not renames the resolution deliberately made.)")
    parts.append("")
    return "\n".join(parts) + "\n"


@dataclass(frozen=True)
class DeterministicPreservation:
    """The deterministic structural-preservation verdict for a candidate.

    Two independent signals (embeddings survey → critic guardrail):
    - ``cur_ratio``/``rep_ratio``: entity-level coverage (tree-sitter
      ``preservation_coverage``) — of the structural units each side ADDED beyond
      base, the fraction present in the resolution. 1.0 = all preserved.
    - ``dropped_cur_additions``/``dropped_replayed_additions``: token-level
      signal (the ``BothSidesRepresentedValidator`` logic) — whether the merge
      carries ANY of a side's distinctive added tokens. False = represented.
    - ``unanimous``: True iff BOTH ratios are exactly 1.0 AND neither side has
      dropped additions — the hard-backstop condition.
    """

    cur_ratio: float
    rep_ratio: float
    dropped_cur_additions: bool
    dropped_replayed_additions: bool
    cur_dropped_names: list[str]
    rep_dropped_names: list[str]

    @property
    def unanimous(self) -> bool:
        return (
            self.cur_ratio >= 1.0
            and self.rep_ratio >= 1.0
            and not self.dropped_cur_additions
            and not self.dropped_replayed_additions
        )

    @property
    def min_ratio(self) -> float:
        return min(self.cur_ratio, self.rep_ratio)


def _deterministic_preservation(
    unit: ConflictUnit, candidate: CandidateResolution,
    cur_lines: str, rep_lines: str, base_lines: str,
) -> DeterministicPreservation | None:
    """Compute the deterministic structural-preservation verdict.

    Combines the entity-level coverage (tree-sitter, when available) with the
    token-level dropped-additions check (always available, stdlib regex). Returns
    None only when even the token-level check can't run (shouldn't happen for a
    real candidate). Pure; never raises — a structural failure degrades to the
    token-only signal with ratios reported as unavailable (represented as 1.0 so
    the token check alone can still drive ``unanimous``).
    """
    import re

    def _toks(text: str) -> set[str]:
        return set(re.findall(r"\w+", text or ""))

    base_t = _toks(base_lines)
    cur_t = _toks(cur_lines)
    rep_t = _toks(rep_lines)
    merged_t = _toks(candidate.resolved_text or "")
    cur_added = cur_t - base_t
    rep_added = rep_t - base_t
    dropped_cur = bool(cur_added) and not (cur_added & merged_t)
    dropped_rep = bool(rep_added) and not (rep_added & merged_t)

    # Entity-level coverage via tree-sitter (may be unavailable).
    lang = unit.language
    cur_ratio = rep_ratio = 1.0
    cur_names: list[str] = []
    rep_names: list[str] = []
    try:
        from capybase.adapters import structural

        if lang in ("python", "rust") and structural.is_available(lang):
            cur_cov = structural.preservation_coverage(base_lines, cur_lines, candidate.resolved_text, lang)
            rep_cov = structural.preservation_coverage(base_lines, rep_lines, candidate.resolved_text, lang)
            if cur_cov is not None:
                cur_ratio = cur_cov.ratio
                cur_names = [e.name for e in cur_cov.dropped]
            if rep_cov is not None:
                rep_ratio = rep_cov.ratio
                rep_names = [e.name for e in rep_cov.dropped]
    except Exception:  # noqa: BLE001 - token-only fallback
        pass

    return DeterministicPreservation(
        cur_ratio=cur_ratio, rep_ratio=rep_ratio,
        dropped_cur_additions=dropped_cur, dropped_replayed_additions=dropped_rep,
        cur_dropped_names=cur_names, rep_dropped_names=rep_names,
    )


def _deterministic_assertion_block(dp: DeterministicPreservation | None) -> str:
    """Render the SYSTEM ASSERTION block for the critic prompt (Phase 1).

    When preservation is unanimous, a DIRECTIVE: the math proves both sides are
    present, so the critic must not flag missing additions — judge only semantic
    coherence / ordering / syntax. When imperfect, a POINTER: list the specific
    dropped entities so the critic's attention goes to the real gap, not a
    hallucinated one. Empty when no deterministic data is available.
    """
    if dp is None:
        return ""
    lines = ["SYSTEM ASSERTION (deterministic, authoritative):"]
    lines.append(f"  current_preservation_ratio: {dp.cur_ratio:.2f}")
    lines.append(f"  replayed_preservation_ratio: {dp.rep_ratio:.2f}")
    lines.append(f"  dropped_current_additions: {str(dp.dropped_cur_additions).lower()}")
    lines.append(f"  dropped_replayed_additions: {str(dp.dropped_replayed_additions).lower()}")
    if dp.unanimous:
        lines.append(
            "  Both sides are MATHEMATICALLY preserved. You MUST NOT flag missing "
            "additions — they are present. Judge ONLY: semantic coherence, logical "
            "ordering, syntax validity."
        )
    else:
        gaps: list[str] = []
        if dp.dropped_cur_additions or dp.cur_ratio < 1.0:
            nm = ", ".join(dp.cur_dropped_names) if dp.cur_dropped_names else "(token-level drop)"
            gaps.append(f"  CURRENT side may drop: {nm}")
        if dp.dropped_replayed_additions or dp.rep_ratio < 1.0:
            nm = ", ".join(dp.rep_dropped_names) if dp.rep_dropped_names else "(token-level drop)"
            gaps.append(f"  REPLAYED side may drop: {nm}")
        if gaps:
            lines.append("  Focus your review on these GENUINE gaps (not other additions):")
            lines.extend(gaps)
    lines.append("")
    return "\n".join(lines) + "\n"


def build_verifier_reassessment_prompt(
    unit: ConflictUnit,
    candidate: CandidateResolution,
    verdict: dict,
    dp: DeterministicPreservation | None,
) -> str:
    """The Phase 2 show-your-work reflection prompt (critic guardrail).

    A second call that asks the critic to PROVE its drop claim by extracting the
    exact evidence snippet. The ``evidence_snippet`` is verified programmatically
    downstream (substring match against the actual sides/resolved) — null or
    fabricated evidence squashes the flag. Context-scoped: only the flagged
    region + resolved text + the deterministic ratios, not the whole file.
    """
    cur_lines, _base_lines, rep_lines = _prompt_sides(unit)
    dropped_sides = []
    if not verdict.get("preserves_current", True):
        dropped_sides.append("CURRENT_UPSTREAM_SIDE")
    if not verdict.get("preserves_replayed", True):
        dropped_sides.append("REPLAYED_COMMIT_SIDE")
    sides_label = " and ".join(dropped_sides) or "a side"
    ratios = ""
    if dp is not None:
        ratios = (
            f"Deterministic preservation ratios: current={dp.cur_ratio:.2f}, "
            f"replayed={dp.rep_ratio:.2f}.\n"
        )
    return f"""You previously judged this merge as dropping the {sides_label}. Re-examine
that verdict with the deterministic evidence below. Your earlier call may have
hallucinated a drop that the AST mathematically disproves.

{ratios}CURRENT_UPSTREAM_SIDE:
{cur_lines}

REPLAYED_COMMIT_SIDE:
{rep_lines}

PROPOSED RESOLUTION:
{candidate.resolved_text}

If you still believe a side's intent was dropped or mangled, you MUST quote the
EXACT verbatim text that is missing or wrong as ``evidence_snippet`` — copied
character-for-character from one of the sides above (the content you claim is
absent or corrupted in the resolution). If you cannot point to specific text
(because the content is in fact present), set ``evidence_snippet`` to null and
``original_verdict_accurate`` to false. Output ONE ```json fenced object:
```json
{{
  "original_verdict_accurate": true,
  "reasoning": "<one short sentence>",
  "evidence_snippet": "<exact verbatim text, or null>"
}}
```
"""


def build_verifier_prompt_conflict(
    unit: ConflictUnit,
    candidate: CandidateResolution,
    context: ContextBundle,
) -> str:
    """The CONFLICT-focus critic prompt (PoLL jury §2.1, second judge).

    A second critic with a COMPLEMENTARY focus to :func:`build_verifier_prompt`
    (which judges intent PRESERVATION — "did it drop a side"). This judge asks a
    different question: does the merge introduce a semantic CONFLICT — two
    behaviors that can't both hold, or a value/branch that CONTRADICTS a side's
    change? Same JSON schema so the existing verdict parsing is reused; the jury
    takes the UNION of both critics' flags (a candidate flagged by EITHER is
    retried) — coverage over voting, since for merge correctness missing a real
    bug is worse than an extra retry.

    Same-model different-prompt jury (our reality: one local model). Correlated
    blind spots vs a cross-model jury, but the distinct focus still broadens
    coverage beyond a single judge.
    """
    cur_lines, base_lines, rep_lines = _prompt_sides(unit)
    sv = context.structural_view
    enc_sig = sv.get("enclosing_node_signature") if sv else None
    enc_text = sv.get("enclosing_node_text") if sv else None
    structural_anchor = ""
    if enc_sig and enc_text:
        structural_anchor = (
            f"Logical block (tree-sitter AST):\n{enc_sig}\n{enc_text}\n\n"
        )
    return f"""You are a strict code reviewer judging a git merge resolution for SEMANTIC
CONFLICTS. A merge conflict has three sides and a proposed resolution. Judge
ONLY whether the resolution introduces a CONFLICT or CONTRADICTION — where the
two sides' changes cannot both hold, or the resolution picks a value/branch that
contradicts what one side deliberately changed. (This is distinct from "did it
DROP a side" — a different judge covers that. You cover CONTRADICTIONS.)

{structural_anchor}CURRENT_UPSTREAM_SIDE (one branch's change):
{cur_lines}

REPLAYED_COMMIT_SIDE (the other branch's change):
{rep_lines}

BASE (common ancestor):
{base_lines}

PROPOSED RESOLUTION:
{candidate.resolved_text}

Does the resolution introduce a semantic conflict? A conflict exists when the
resolution contradicts a deliberate change from either side, or combines two
behaviors that cannot both be true (ignore cosmetic differences and cases where
both sides' changes are independently preserved). Output ONE ```json fenced
object, nothing else:
```json
{{
  "preserves_current": true,
  "preserves_replayed": true,
  "reason": "<one short sentence>",
  "confidence": 0.0
}}
```
"""


class ResolutionEngine:
    def __init__(
        self,
        config: ModelConfig,
        *,
        client: LLMClient | None = None,
    ) -> None:
        self.config = config
        self.client = client or OpenAICompatibleClient(config)
        # Token-window budget for the resolve prompt (0/disabled → no trimming).
        # Built once from the config so every propose() call shares it; the
        # profile overlay (which sets context_window) is applied before the
        # engine is constructed, so this reflects the calibrated window.
        self.token_budget = TokenBudget.from_config(config)

    def raw_complete(self, prompt: str, *, json_mode: bool = False) -> LLMResponse:
        """One-shot completion: send ``prompt`` and return the raw response.

        Used by the block-capture layer (and any future decision-style prompt)
        where the model is NOT producing a candidate's resolved_text but a small
        structured decision. Mirrors :meth:`_one`'s message construction and
        client call, but returns the raw :class:`LLMResponse` for the caller to
        parse with the decision-specific parser (not the candidate coercer).
        Raises on a request failure — the caller decides retry/fall-through.
        """
        messages = [
            {"role": "system", "content": "You are a careful merge-resolution assistant."},
            {"role": "user", "content": prompt},
        ]
        return self.client.complete(
            messages,
            model=self.config.model,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            json_mode=json_mode,
        )

    def propose(
        self,
        unit: ConflictUnit,
        context: ContextBundle,
        *,
        failures: list[VerificationFailure] | None = None,
        prev_candidate: CandidateResolution | None = None,
        n_samples: int | None = None,
    ) -> list[CandidateResolution]:
        """Generate one or more candidates for ``unit``.

        ``failures`` is non-empty on retry; the retry prompt feeds them back
        (CEGIS-style). When ``prev_candidate`` is also given (the failed
        attempt), the targeted *repair* prompt is used — it includes the broken
        code so the model fixes locally rather than regenerating from scratch.
        The number of samples comes from config so self-consistency is enabled
        by raising ``samples``.

        ``n_samples`` overrides the config sample count when given (used by the
        orchestrator to allocate more samples to "complex" units, survey §4
        UAB-lite). ``None`` (default) uses ``self.config.samples`` unchanged.
        """
        prompt_trims: list[dict] = []
        if failures and prev_candidate and prev_candidate.resolved_text:
            prompt_version = PROMPT_REPAIR
            # The repair prompt carries sides+candidate+feedback only; build it
            # via the public builder (string). Trims stay empty (sides protected).
            prompt = build_repair_prompt(unit, context, prev_candidate, failures)
        elif failures:
            prompt_version = PROMPT_RETRY
            # Retry: resolve-parts (budget-trimmed) + feedback. Read the trims
            # from _resolve_prompt_parts directly so we can journal them.
            parts = _resolve_prompt_parts(unit, context, budget=self.token_budget)
            prompt_trims = parts["trims"]
            inner = parts["intro"] + parts["data"] + parts["contract"] + parts["rules"]
            feedback = "\n".join(_render_failure(f) for f in failures) or "- (no specific failures reported)"
            prompt = (
                "Your previous merge attempt was rejected. Fix it.\n\n"
                f"{inner}\n\n"
                "### validator feedback (previous attempt failed these checks)\n"
                f"{feedback}\n\n"
                "Address every failure above; do not repeat the mistake. End with the "
                "```json\nfenced answer as instructed.\n"
            )
        else:
            prompt_version = PROMPT_RESOLVE
            # Outline-first variant (small-model experiment): when an outline
            # variant is active, build the prompt via build_outline_resolve_prompt
            # (which reuses _resolve_prompt_parts, so the data/contract/rules are
            # invariant; only the framing + an outline preamble differ). The
            # variant tag is folded into prompt_version for attribution.
            outline_prompt, outline_tag = build_outline_resolve_prompt(
                unit, context, budget=self.token_budget
            )
            if outline_tag:
                prompt_version = PROMPT_RESOLVE + outline_tag
            parts = _resolve_prompt_parts(unit, context, budget=self.token_budget)
            prompt_trims = parts["trims"]
            prompt = outline_prompt
        candidates: list[CandidateResolution] = []
        n = max(1, self.config.samples if n_samples is None else n_samples)
        # Prompt-variant sampling (survey §4): on a FRESH resolve only (retries/
        # repair must stay single-template for reproducible counterexample
        # feedback), spread samples across semantically-equivalent phrasings.
        # The temperature portfolio still applies across the variants. Off by
        # default; engages only with samples > 1 + parallel sampling.
        if (
            not failures
            and n > 1
            and self.config.parallel_samples
            and getattr(self.config, "prompt_variants", False)
        ):
            variants = build_resolve_prompt_variants(unit, context, k=n, budget=self.token_budget)
            cands = self._sample_variants(unit, context, prompt_version, variants)
            # Attach the prompt-window trims to each variant candidate so the
            # orchestrator can journal them (observability of trimming).
            if prompt_trims:
                for c in cands:
                    c.prompt_trims = list(prompt_trims)
            return cands
        # Single sample or no parallelism: sequential (fast path, no overhead).
        if n == 1 or not self.config.parallel_samples:
            for _ in range(n):
                cand = self._one(unit, context, prompt, prompt_version)
                if prompt_trims:
                    cand.prompt_trims = list(prompt_trims)
                candidates.append(cand)
        else:
            # Draw samples concurrently in a thread pool. Each _one() call is a
            # blocking HTTP request; running them in parallel turns N×latency
            # into ~1×latency. Safe because the adapter is stateless per-call.
            candidates = self._sample_parallel(
                unit, context, prompt, prompt_version, n,
                temperature_override=self.config.sampling_temperature,
            )
            if prompt_trims:
                for c in candidates:
                    c.prompt_trims = list(prompt_trims)
        # Focused repair (§3): when the repair prompt was used and the model
        # emitted SEARCH/REPLACE edits, apply them to the previous attempt to
        # produce the new resolved_text (instead of requiring a full rewrite).
        # Graceful: no edits / all-missed → keep the model's resolved_text (full
        # mode). The applied result flows downstream verbatim (verify/splice).
        if prompt_version == PROMPT_REPAIR and prev_candidate and prev_candidate.resolved_text:
            candidates = [_apply_repair_edits(c, prev_candidate) for c in candidates]
        return candidates

    def _sample_variants(
        self,
        unit: ConflictUnit,
        context: ContextBundle,
        base_version: str,
        variants: list[tuple[str, str]],
    ) -> list[CandidateResolution]:
        """Draw one sample per prompt variant in a thread pool (survey §4).

        ``variants`` is a list of ``(prompt_text, variant_suffix)`` pairs from
        ``build_resolve_prompt_variants``. Each suffix is appended to
        ``base_version`` so the candidate's ``prompt_version`` records which
        phrasing produced it (e.g. ``resolve_text_block.v5#v1``). The
        temperature portfolio (``_sample_temperatures``) is applied across the
        variants in order, so a high-temperature exploratory sample and a
        low-temperature conservative sample still get drawn. Returns one
        candidate per variant.
        """
        temps = self._sample_temperatures(len(variants), self.config.sampling_temperature)
        with ThreadPoolExecutor(max_workers=min(len(variants), 8)) as pool:
            futures = [
                pool.submit(
                    self._one,
                    unit, context, prompt_text,
                    f"{base_version}{suffix}",
                    temps[i] if i < len(temps) else temps[-1],
                )
                for i, (prompt_text, suffix) in enumerate(variants)
            ]
            return [f.result() for f in futures]

    def _sample_parallel(
        self,
        unit: ConflictUnit,
        context: ContextBundle,
        prompt: str,
        prompt_version: str,
        n: int,
        *,
        temperature_override: float | None = None,
    ) -> list[CandidateResolution]:
        """Draw N samples: prefer one server-side ``n`` request, fall back to a
        thread pool.

        Step 2 (parallel sampling): a single request with ``n=N`` lets the
        server batch all samples in one round-trip — critical on a single-GPU
        llama-server where N concurrent requests serialize to one batch slot
        and pay N× scheduling overhead. When the client supports
        ``complete_many`` AND returns all N choices, we use them; otherwise we
        fall back to N concurrent ``complete`` calls (the original behavior).

        When ``diverse_sampling`` is enabled we bypass the batched path: the
        server draws all N choices at ONE temperature, so per-sample
        temperature diversity (survey §4.1) requires N separate requests.
        Diversity beats batching efficiency for correctness, so the thread
        pool is used with a per-sample temperature portfolio.
        """
        temps = self._sample_temperatures(n, temperature_override)
        # Only the thread-pool path supports per-sample temperatures; when all
        # temps are equal (diverse_sampling off, or N==1) try the batched path.
        if len(set(temps)) <= 1:
            candidates = self._sample_n(
                unit, prompt, prompt_version, n, temperature_override=temperature_override
            )
            if candidates is not None:
                return candidates
        # Fallback / diverse path: thread pool of independent requests.
        with ThreadPoolExecutor(max_workers=min(n, 8)) as pool:
            futures = [
                pool.submit(self._one, unit, context, prompt, prompt_version, t)
                for t in temps
            ]
            return [f.result() for f in futures]

    def _sample_temperatures(
        self, n: int, temperature_override: float | None = None
    ) -> list[float]:
        """Build the per-sample temperature portfolio (survey §4.1).

        When ``diverse_sampling`` is off (default), every sample uses the same
        temperature (the override, or the base) — returned as a list so callers
        can detect uniformity and try the batched ``n`` path.

        When on, the portfolio splits N into exploratory samples at the higher
        ``sampling_temperature`` and conservative samples at the lower base
        ``temperature``, guaranteeing at least one of each for N >= 2. This
        gives diversity (high-temp explores) AND a reliable fallback
        (low-temp stays close to a safe answer) — on a 3B model it raises the
        odds that at least one sample is both valid and distinct.
        """
        if n <= 1 or not getattr(self.config, "diverse_sampling", False):
            t = temperature_override if temperature_override is not None else self.config.temperature
            return [t] * n
        high = self.config.sampling_temperature
        low = self.config.temperature
        if high <= low:
            # No diversity to exploit (misconfigured); fall back to uniform.
            return [temperature_override if temperature_override is not None else low] * n
        # Split roughly in half: ceil(n/2) exploratory (high), rest conservative.
        n_high = (n + 1) // 2
        n_low = n - n_high
        return [high] * n_high + [low] * n_low


    def _sample_n(
        self,
        unit: ConflictUnit,
        prompt: str,
        prompt_version: str,
        n: int,
        *,
        temperature_override: float | None = None,
    ) -> list[CandidateResolution] | None:
        """Server-side N sampling via ``complete_many``.

        Returns None when the client lacks ``complete_many`` or the server
        ignored ``n`` (returned fewer than ``n`` choices) — the caller then
        falls back to the thread pool. This keeps the optimization transparent
        and safe: any server that doesn't support ``n`` simply yields the
        original behavior.
        """
        complete_many = getattr(self.client, "complete_many", None)
        if not callable(complete_many):
            return None
        messages = [
            {"role": "system", "content": "You are a careful merge-resolution assistant."},
            {"role": "user", "content": prompt},
        ]
        temperature = (
            temperature_override
            if temperature_override is not None
            else self.config.temperature
        )
        try:
            responses = complete_many(
                messages,
                model=self.config.model,
                temperature=temperature,
                max_tokens=self.config.max_tokens,
                json_mode=self.config.json_mode,
                n=n,
            )
        except Exception as exc:  # noqa: BLE001 - fall back to thread pool
            return None
        # complete_many is duck-typed (getattr above); coerce defensively.
        responses = list(responses) if responses is not None else []
        if len(responses) < n:
            # Server ignored/doesn't support n — not enough samples returned.
            return None
        return [
            self._candidate_from_response(unit, prompt_version, resp)
            for resp in responses
        ]

    def propose_recovery(
        self,
        unit: ConflictUnit,
        context: ContextBundle,
        *,
        failures: list[VerificationFailure] | None = None,
    ) -> list[CandidateResolution]:
        """One recovery candidate via build_recovery_prompt (CEGIS loop hardening).

        For a model that self-reported needs_human: a single-sample retry with
        the reframed recovery prompt (strips the needs_human escape hatch, adds
        step-by-step scaffolding). Distinct from propose() — no difficulty
        routing, no consensus, no prev_candidate (the refusal produced no usable
        code to repair). The orchestrator calls this when risk.decide grants a
        recovery retry (the __recovery_retry__ followup marker).
        """
        prompt = build_recovery_prompt(unit, context, failures, budget=self.token_budget)
        resp = self._one(unit, context, prompt, PROMPT_RECOVERY)
        return [resp] if resp is not None else []

    def propose_with_consensus(
        self,
        unit: ConflictUnit,
        context: ContextBundle,
        *,
        failures: list[VerificationFailure] | None = None,
        prev_candidate: CandidateResolution | None = None,
        n_samples: int | None = None,
    ) -> tuple[list[CandidateResolution], ConsensusReport | None]:
        """Generate N samples and reorder so the majority winner is first.

        When ``samples <= 1`` there is no voting to do; this returns the single
        candidate unchanged with a trivial report. Otherwise the candidates are
        normalized and clustered; the largest cluster's representative is moved
        to index 0 so the orchestrator's ``candidates[0]`` takes the consensus
        winner. The report (agreement score, cluster count) is returned for
        journaling and as a risk signal — low agreement flags an uncertain
        merge.

        ``n_samples`` overrides the config sample count (forwarded to
        ``propose``); used by the orchestrator for difficulty-aware allocation.
        ``prev_candidate`` (with ``failures``) selects the targeted *repair*
        prompt on a retry instead of the generic retry prompt — forwarded to
        ``propose`` so self-consistency retries keep the CEGIS counterexample
        feedback instead of degrading to a from-scratch retry.
        """
        candidates = self.propose(
            unit, context,
            failures=failures, prev_candidate=prev_candidate, n_samples=n_samples,
        )
        if len(candidates) <= 1:
            return candidates, None
        ordered, report = rank_by_consensus(candidates, unit.language)
        return ordered, report

    def propose_two_pass(
        self,
        unit: ConflictUnit,
        context: ContextBundle,
        *,
        n_samples: int = 1,
        temperature: float | None = None,
    ) -> list[CandidateResolution]:
        """Two-pass generation: extract intents, then generate code.

        Pass 1 (intent): one cheap request asking only for semantic intents.
        Pass 2 (code): N samples at raised temperature, each conditioned on the
        same intent map. The model generates code guided by its own prior
        reasoning — a 3B model reasons better when it understands the conflict
        before trying to fix it.

        If the intent pass fails, falls back to single-pass ``propose`` so the
        pipeline degrades gracefully.
        """
        # --- Pass 1: extract intents ---
        intents = self._call_intent(unit, context)
        if intents is None:
            # Intent pass failed — degrade to single-pass.
            return self.propose(unit, context)
        n = max(1, n_samples)
        temp = temperature if temperature is not None else self.config.sampling_temperature
        # --- PlanSearch (survey §1): sample N distinct plans → one code per plan.
        # Falls back to the standard one-plan→N-code path if disabled, if the
        # plan-search call fails, or if it yields <2 plans (parse_plans guards).
        if (
            getattr(self.config, "plan_search", False)
            and n > 1
        ):
            plans = self._call_plan_search(unit, context)
            if plans:
                return self._sample_plans(unit, context, intents, plans, n, temp)
        # --- Pass 2 (standard): N code samples conditioned on the single intent map ---
        code_prompt = build_code_prompt(unit, context, intents)
        if n == 1:
            return [self._one(unit, context, code_prompt, PROMPT_CODE, temp)]
        if not self.config.parallel_samples:
            return [self._one(unit, context, code_prompt, PROMPT_CODE, temp) for _ in range(n)]
        return self._sample_parallel(
            unit, context, code_prompt, PROMPT_CODE, n,
            temperature_override=temp,
        )

    def _call_plan_search(
        self, unit: ConflictUnit, context: ContextBundle
    ) -> list[dict] | None:
        """PlanSearch Pass 1: ask the model for multiple distinct plans.

        Returns a list of plan dicts (``{strategy, steps}``), or ``None`` on any
        failure — callers fall back to the single-intent path so a flaky
        plan-search call never blocks resolution.
        """
        plan_prompt = build_plan_search_prompt(unit, context)
        messages = [
            {"role": "system", "content": "You are a careful merge-planning assistant."},
            {"role": "user", "content": plan_prompt},
        ]
        try:
            resp = self.client.complete(
                messages,
                model=self.config.model,
                temperature=self.config.temperature,
                max_tokens=min(self.config.max_tokens, 2048),
                json_mode=self.config.json_mode,
            )
        except Exception:  # noqa: BLE001
            return None
        return parse_plans(resp.text or "")

    def _sample_plans(
        self,
        unit: ConflictUnit,
        context: ContextBundle,
        intents: dict[str, list[str]],
        plans: list[dict],
        n: int,
        temp: float,
    ) -> list[CandidateResolution]:
        """PlanSearch Pass 2: generate one code candidate per plan.

        Each plan gets its own plan-conditioned prompt and one sample; the plan
        index is tagged on ``prompt_version`` (``code_from_intent.v1#plan{i}``)
        so offline eval can attribute outcomes per plan. ``n`` caps the number
        of plans used (min of plans length and n). The thread pool draws them
        concurrently, like the standard parallel path.
        """
        k = min(len(plans), n)
        chosen = plans[:k]
        # Each candidate's prompt carries its plan; the version tags the index.
        prompts = [
            (build_code_prompt(unit, context, intents, plan=p),
             f"{PROMPT_CODE}#plan{i}")
            for i, p in enumerate(chosen)
        ]
        if not self.config.parallel_samples or k == 1:
            return [self._one(unit, context, p, v, temp) for p, v in prompts]
        with ThreadPoolExecutor(max_workers=min(k, 8)) as pool:
            futures = [
                pool.submit(self._one, unit, context, p, v, temp)
                for p, v in prompts
            ]
            return [f.result() for f in futures]

    def _call_intent(
        self, unit: ConflictUnit, context: ContextBundle
    ) -> dict[str, list[str]] | None:
        """Pass 1: extract semantic intents via a dedicated lightweight call.

        Unlike ``_one`` (which expects ``resolved_text``), this call parses the
        intent JSON directly — the response has ``current_side_intent`` and
        ``replayed_commit_intent`` fields, not code. Returns None on any failure.
        """
        intent_prompt = build_intent_prompt(unit, context)
        messages = [
            {"role": "system", "content": "You are a careful merge-analysis assistant."},
            {"role": "user", "content": intent_prompt},
        ]
        try:
            resp = self.client.complete(
                messages,
                model=self.config.model,
                temperature=self.config.temperature,
                max_tokens=min(self.config.max_tokens, 2048),
                json_mode=self.config.json_mode,
            )
        except Exception:  # noqa: BLE001
            return None
        try:
            parsed, _warnings = coerce_candidate_dict(resp.text)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(parsed, dict):
            return None
        cur = parsed.get("current_side_intent", [])
        rep = parsed.get("replayed_commit_intent", [])
        if not cur and not rep:
            return None
        return {
            "current_side_intent": list(cur) if isinstance(cur, list) else [str(cur)],
            "replayed_commit_intent": list(rep) if isinstance(rep, list) else [str(rep)],
        }

    def _one(
        self,
        unit: ConflictUnit,
        context: ContextBundle,
        prompt: str,
        prompt_version: str,
        temperature_override: float | None = None,
    ) -> CandidateResolution:
        messages = [
            {"role": "system", "content": "You are a careful merge-resolution assistant."},
            {"role": "user", "content": prompt},
        ]
        temperature = (
            temperature_override
            if temperature_override is not None
            else self.config.temperature
        )
        try:
            resp: LLMResponse = self.client.complete(
                messages,
                model=self.config.model,
                temperature=temperature,
                max_tokens=self.config.max_tokens,
                json_mode=self.config.json_mode,
            )
        except Exception as exc:  # noqa: BLE001 - degrade to retryable failure
            return _failed_candidate(
                unit, self.config.model, prompt_version, str(exc), "",
                failure_kind="request_failed",
            )
        return self._candidate_from_response(unit, prompt_version, resp)

    def _candidate_from_response(
        self, unit: ConflictUnit, prompt_version: str, resp: LLMResponse
    ) -> CandidateResolution:
        """Build a CandidateResolution from a single LLMResponse.

        Shared by ``_one`` (thread-pool path) and ``_sample_n`` (server-side
        N sampling) so every sample is validated identically regardless of how
        it was drawn. Detects truncation (finish_reason=length) and parse
        failures, mapping them to retryable failure_kinds.
        """
        meta = resp.raw or {}
        finish = ""
        if isinstance(meta, dict):
            acc = meta.get("_accumulated")
            if isinstance(acc, dict):
                finish = acc.get("finish_reason") or ""
            if not finish:
                choices = meta.get("choices") or []
                if choices:
                    finish = choices[0].get("finish_reason") or ""
        if finish == "length":
            return _failed_candidate(
                unit, self.config.model, prompt_version,
                "model output truncated (finish_reason=length); increase max_tokens",
                resp.text, failure_kind="truncated",
            )
        data, warnings = coerce_candidate_dict(resp.text)
        has_edits = isinstance(data, dict) and bool(data.get("edits"))
        if not data or ("resolved_text" not in data and not has_edits):
            warnings = warnings or ["response missing resolved_text and edits"]
            return _failed_candidate(
                unit, self.config.model, prompt_version,
                "could not parse resolution", resp.text, warnings,
                failure_kind="parse_failed",
            )
        needs_human = bool(data.get("needs_human", False))
        cand = CandidateResolution(
            candidate_id=f"{unit.unit_id}:{uuid.uuid4().hex[:6]}",
            unit_id=unit.unit_id,
            model_name=self.config.model,
            prompt_version=prompt_version,
            current_side_intent=list(data.get("current_side_intent", [])),
            replayed_commit_intent=list(data.get("replayed_commit_intent", [])),
            resolved_text=str(data.get("resolved_text", "")),
            explanation=str(data.get("explanation", "")),
            repair_plan=str(data.get("plan", "")),
            preserved_current_side=bool(data.get("preserved_current_side", True)),
            preserved_replayed_commit_side=bool(
                data.get("preserved_replayed_commit_side", True)
            ),
            dropped_current_side_details=list(data.get("dropped_current_side_details", [])),
            dropped_replayed_commit_details=list(data.get("dropped_replayed_commit_details", [])),
            assumptions=list(data.get("assumptions", [])),
            needs_human=needs_human,
            self_reported_confidence=float(data.get("self_reported_confidence", 0.0)),
            # TECP: surface the API's per-token logprob signal onto the candidate
            # so the calibration seam can learn from model-side uncertainty.
            mean_token_entropy=resp.mean_token_entropy,
            raw_response=resp.text,
            parse_warnings=warnings,
            # A genuine model refusal (it answered JSON but said needs_human).
            failure_kind="model_refusal" if needs_human else "",
            # Default LLM provenance (#9 step 8): plain LLM. The orchestrator
            # re-stamps this to "history_augmented_llm" when history context
            # meaningfully augmented the prompt (history_confidence >= threshold
            # and future/history lines were actually injected).
            provenance="plain_llm",
        )
        # Stash SEARCH/REPLACE edits (focused-repair §3) for the propose() path
        # to apply against the previous attempt. In edit mode the resolved_text
        # is empty until the edits are applied.
        if has_edits:
            cand._repair_edits = list(data.get("edits") or [])
        return cand



def _failed_candidate(
    unit: ConflictUnit,
    model_name: str,
    prompt_version: str,
    reason: str,
    raw: str,
    warnings: list[str] | None = None,
    *,
    failure_kind: str = "request_failed",
) -> CandidateResolution:
    return CandidateResolution(
        candidate_id=f"{unit.unit_id}:{uuid.uuid4().hex[:6]}",
        unit_id=unit.unit_id,
        model_name=model_name,
        prompt_version=prompt_version,
        resolved_text="",
        explanation=reason,
        needs_human=True,
        raw_response=raw,
        parse_warnings=warnings or [reason],
        failure_kind=failure_kind,
    )
