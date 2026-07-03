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
    # into sides_text for the budget accounting.
    side_intent = _side_intent_block(unit)
    sides_text = (
        f"{side_intent}"
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
        f"{obls_t}{anchor_t}{siblings_t}{deps_t}{history_t}{few_shot_t}{side_intent}"
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
    """
    feedback = "\n".join(_render_failure(f) for f in failures) or "- (no specific failures reported)"
    cur_lines, _base_lines, rep_lines = _prompt_sides(unit)
    side_intent = _side_intent_block(unit)
    return f"""Your previous merge attempt had errors. Fix the SPECIFIC errors in
your code below — do not rewrite from scratch unless necessary. Keep all parts
that were correct; change only what the validator flagged.

file: {unit.path}
language: {unit.language or 'unknown'}
{side_intent}
CURRENT_UPSTREAM_SIDE body:
{cur_lines}

REPLAYED_COMMIT_SIDE body:
{rep_lines}

YOUR PREVIOUS ATTEMPT (needs fixing):
{candidate.resolved_text}

### validator feedback (fix these specific issues)
{feedback}

Output the corrected resolved_text as a ```json fenced object:
{{
  "resolved_text": "<the fixed replacement text, exact indentation>",
  "explanation": "<what you changed and why>",
  "self_reported_confidence": 0.0
}}
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
) -> str:
    """Build the critic prompt for the verifier-model validator (surveys §1/§5).

    Asks the LLM to judge whether ``candidate.resolved_text`` preserves BOTH
    sides' intent — the semantic check no syntactic validator can make. The
    model sees BASE, CURRENT, REPLAYED, and the candidate RESOLVED, and must
    return strict JSON verdict booleans. This is purely a judging call on the
    same black-box API; it never edits code (the CEGIS loop does repairs).
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
    # Deterministic per-side preservation evidence (survey §5.1 quantitative):
    # the specific logical units each side ADDED that are ABSENT from the
    # resolution. Gives the judge concrete evidence to weigh, beyond eyeballing
    # the three sides — a unit listed here is very likely a dropped intent.
    evidence = _dropped_units_evidence(unit, candidate, cur_lines, rep_lines, base_lines)
    return f"""You are a strict code reviewer judging a git merge resolution. A merge
conflict has three sides and a proposed resolution. Judge ONLY whether the
resolution preserves the INTENT of BOTH sides — it must not silently drop a
behavior, guard, or value that either side added.

{structural_anchor}{evidence}CURRENT_UPSTREAM_SIDE (one branch's change):
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
            parts = _resolve_prompt_parts(unit, context, budget=self.token_budget)
            prompt_trims = parts["trims"]
            prompt = parts["intro"] + parts["data"] + parts["contract"] + parts["rules"]
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
        if not data or "resolved_text" not in data:
            warnings = warnings or ["response missing resolved_text"]
            return _failed_candidate(
                unit, self.config.model, prompt_version,
                "could not parse resolution", resp.text, warnings,
                failure_kind="parse_failed",
            )
        needs_human = bool(data.get("needs_human", False))
        return CandidateResolution(
            candidate_id=f"{unit.unit_id}:{uuid.uuid4().hex[:6]}",
            unit_id=unit.unit_id,
            model_name=self.config.model,
            prompt_version=prompt_version,
            current_side_intent=list(data.get("current_side_intent", [])),
            replayed_commit_intent=list(data.get("replayed_commit_intent", [])),
            resolved_text=str(data.get("resolved_text", "")),
            explanation=str(data.get("explanation", "")),
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
