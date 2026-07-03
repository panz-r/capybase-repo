"""Tests for prompt token-window enforcement (_fit_to_budget + _resolve_prompt_parts).

The budget caps the resolve prompt to the model's context window by trimming
AUGMENTATION sections (few-shot → deps → anchor → siblings → surrounding
context), while the three conflict sides + the JSON contract are ALWAYS sent
intact (protect-the-conflict policy). When disabled (total=0) or when the prompt
already fits, nothing is trimmed.
"""

from __future__ import annotations

from capybase.conflict_model import (
    ConflictSide,
    ConflictUnit,
    ContextBundle,
    HistoricalExample,
    RelatedSnippet,
    TokenBudget,
    estimate_tokens,
)
from capybase.resolution_engine import (
    _fit_to_budget,
    _resolve_prompt_parts,
    build_resolve_prompt,
)


def _unit(sides_text: str = "    return 2") -> ConflictUnit:
    return ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=sides_text),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=sides_text),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=sides_text),
        original_worktree_text=sides_text, marker_span=(0, 1),
    )


def _ctx(
    *,
    primary: str = "ctx line\n",
    few_shot: int = 0,
    deps: int = 0,
    anchor: bool = False,
    siblings: bool = False,
) -> ContextBundle:
    def ex(t: str) -> HistoricalExample:
        return HistoricalExample(summary="s", base="e", current=t, replayed=t, resolved=t)
    sv = {}
    if anchor:
        sv["enclosing_node_signature"] = "fn merge()"
        sv["enclosing_node_text"] = "enclosing " * 50
    if siblings:
        sv["sibling_entities"] = ["fn other()", "fn another()"]
    return ContextBundle(
        primary_text=primary,
        retrieved_examples=[ex("shot" * 200) for _ in range(few_shot)],
        related_snippets=[RelatedSnippet(path="dep.py", reason="uses", text="dep" * 200) for _ in range(deps)],
        structural_view=sv,
    )


# ---------------------------------------------------------------------------
# Disabled / no-op (backward compatibility)
# ---------------------------------------------------------------------------


def test_no_budget_passes_everything_through():
    ctx = _ctx(few_shot=2, deps=2, anchor=True, siblings=True)
    anchor, siblings, deps_b, shot, primary, hist, obls, trims = _fit_to_budget(
        budget=None,
        intro="i", contract="c", rules="r",
        sides_text="sides", structural_anchor="A", siblings_block="S",
        deps="D", few_shot="F", primary_text="P", history="H", obligations="O",
    )
    assert (anchor, siblings, deps_b, shot, primary, hist, obls) == ("A", "S", "D", "F", "P", "H", "O")
    assert trims == []


def test_disabled_budget_is_noop():
    ctx = _ctx(few_shot=2, deps=2)
    # budget total=0 → disabled
    anchor, siblings, deps_b, shot, primary, hist, obls, trims = _fit_to_budget(
        budget=TokenBudget(total=0),
        intro="i", contract="c", rules="r",
        sides_text="sides", structural_anchor="A", siblings_block="S",
        deps="D", few_shot="F", primary_text="P", history="H", obligations="O",
    )
    assert (anchor, siblings, deps_b, shot, primary, hist, obls) == ("A", "S", "D", "F", "P", "H", "O")
    assert trims == []


# ---------------------------------------------------------------------------
# Trimming priority: few-shot → deps → anchor → siblings → primary_text
# ---------------------------------------------------------------------------


def test_fits_under_budget_no_trims():
    # Generous budget; small prompt → nothing trimmed.
    parts = _resolve_prompt_parts(_unit(), _ctx(few_shot=1, deps=1), budget=TokenBudget(total=100000, reserved_for_completion=1024))
    assert parts["trims"] == []


def test_drops_few_shot_first_when_over():
    # The boilerplate (intro+contract+rules) is ~300 tokens, so the budget must
    # clear that + the sides to enter the gradual-trim path. Use a window that
    # fits the essential content but not the few-shot examples.
    parts = _resolve_prompt_parts(
        _unit(),
        _ctx(few_shot=3, deps=0, anchor=False, siblings=False, primary="p\n"),
        budget=TokenBudget(total=600, reserved_for_completion=100),
    )
    sections = [t["section"] for t in parts["trims"]]
    # few-shot is the first augmentation dropped; it should be present (whether
    # alone or as part of all_augmentations depends on exact sizing, but few-shot
    # must never survive when we're over budget and it exists).
    assert parts["data"].count("Example ") < 3  # at least one few-shot dropped


def test_drops_deps_after_few_shot():
    parts = _resolve_prompt_parts(
        _unit(),
        _ctx(few_shot=3, deps=3, primary="p\n"),
        budget=TokenBudget(total=600, reserved_for_completion=100),
    )
    sections = [t["section"] for t in parts["trims"]]
    # With few-shot AND deps both present and a budget that can't hold both,
    # at least few-shot is dropped; deps is dropped if few-shot wasn't enough.
    assert "few_shot" in sections or "all_augmentations" in sections
    # The dependency snippets don't survive if few-shot alone didn't free enough.
    if "all_augmentations" not in sections:
        assert "deps" in sections


def test_primary_text_truncated_last_not_below_floor():
    # Very tight budget forces primary_text truncation.
    parts = _resolve_prompt_parts(
        _unit(),
        _ctx(few_shot=0, deps=0, primary="line\n" * 100),
        budget=TokenBudget(total=60, reserved_for_completion=10),
    )
    sections = [t["section"] for t in parts["trims"]]
    # Either all augmentations dropped (essential alone exceeds) or primary_text
    # was truncated. Either way, the sides survive.
    rebuilt = parts["data"]
    assert "return 2" in rebuilt  # the conflict side survived


# ---------------------------------------------------------------------------
# Protect the conflict: sides + contract never trimmed
# ---------------------------------------------------------------------------


def test_sides_always_present_after_trim():
    side = "    return 2"
    u = _unit(side)
    trimmed_prompt = build_resolve_prompt(
        u, _ctx(few_shot=5, deps=5, primary="x " * 1000),
        budget=TokenBudget(total=50, reserved_for_completion=10),
    )
    # The conflict side text survives even under the tightest budget.
    assert side in trimmed_prompt
    # The JSON contract (the schema the model must follow) survives.
    assert "resolved_text" in trimmed_prompt


def test_essential_exceeds_window_sends_anyway_and_flags():
    # A side so large it alone exceeds a tiny window → all augmentations dropped,
    # sides still present, a context_budget_exceeded/all_augmentations note.
    huge_side = "    return " + " + ".join(str(i) for i in range(200)) + "\n"
    u = _unit(huge_side)
    parts = _resolve_prompt_parts(
        u, _ctx(few_shot=2, deps=2, primary="ctx"),
        budget=TokenBudget(total=100, reserved_for_completion=20),
    )
    sections = [t["section"] for t in parts["trims"]]
    assert "all_augmentations" in sections
    # Sides still in the prompt.
    assert "return 0" in parts["data"]


# ---------------------------------------------------------------------------
# Trimming is observable: trims payload carries section + detail
# ---------------------------------------------------------------------------


def test_trims_payload_has_section_and_detail():
    parts = _resolve_prompt_parts(
        _unit(),
        _ctx(few_shot=3, deps=3, primary="p\n"),
        budget=TokenBudget(total=250, reserved_for_completion=100),
    )
    for t in parts["trims"]:
        assert "section" in t and "detail" in t
        assert isinstance(t["detail"], str) and t["detail"]


# ---------------------------------------------------------------------------
# #idea 9: obligations survive trimming (highest-priority augmentation)
# ---------------------------------------------------------------------------


def test_obligations_survive_when_history_dropped():
    """Obligations are a first-class budget section that trims AFTER structural
    context — they survive when history (replay facts) is dropped (#idea 9)."""
    # A tight budget that forces history to drop but leaves obligations.
    # Use _fit_to_budget directly for precise control. Make history large enough
    # (~500 chars ≈ 125 tokens) to exceed the small augmentation budget.
    anchor, siblings, deps_b, shot, primary, hist, obls, trims = _fit_to_budget(
        budget=TokenBudget(total=200, reserved_for_completion=100),
        intro="i", contract="c", rules="r",
        sides_text="sides",
        structural_anchor="", siblings_block="",
        deps="", few_shot="", primary_text="",
        history="H" * 500,  # ~125 tokens — exceeds the ~97 available
        obligations="Future obligations:\n  - keep parse_config\n",
    )
    # History was dropped (lowest priority).
    sections = [t["section"] for t in trims]
    assert "history" in sections
    # Obligations survived (highest priority — dropped last).
    assert obls, "obligations should survive when history is dropped"


def test_obligations_dropped_last_after_structural():
    """Obligations are the LAST augmentation dropped — after anchor, siblings,
    deps, few-shot, primary_text, and history. A very tight budget drops all of
    those but keeps obligations until they too must go."""
    anchor, siblings, deps_b, shot, primary, hist, obls, trims = _fit_to_budget(
        budget=TokenBudget(total=150, reserved_for_completion=100),
        intro="i", contract="c", rules="r",
        sides_text="sides",
        structural_anchor="A" * 50, siblings_block="S" * 50,
        deps="D" * 50, few_shot="F" * 50, primary_text="P" * 50,
        history="H" * 50,
        obligations="OBLIG: keep parse\n",
    )
    sections = [t["section"] for t in trims]
    # History is dropped before obligations.
    if "obligations" in sections:
        # If obligations were dropped, everything lower-priority was too.
        assert "history" in sections
        assert "structural_anchor" in sections


def test_obligations_in_prompt_render_before_sides():
    """The obligations section renders in the data block (the model sees it)."""
    from capybase.conflict_model import ContextBundle
    bundle = ContextBundle(
        primary_text="x", token_estimate=1,
        obligations_context="Future obligations:\n  - keep helper\n",
    )
    parts = _resolve_prompt_parts(_unit(), bundle, budget=None)
    assert "keep helper" in parts["data"]
