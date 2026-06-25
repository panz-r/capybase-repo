"""Tests for difficulty-aware routing (survey §6.1).

classify_difficulty is a pure function of a ConflictUnit's structural metadata
and side texts. Simple conflicts (single isolated hunk, small node, short
sides) take a fast path; complex ones (multi-hunk, large node, large sides)
get the full test-time pipeline. Disabled by default — opt-in via config.
"""

from __future__ import annotations

from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.routing import RoutingConfig, classify_difficulty


def _unit(
    *,
    sibling_count: int = 0,
    node_span: tuple[int, int] | None = None,
    base: str = "def f():\n    return 1",
    current: str = "    return 2",
    replayed: str = "    return 3",
) -> ConflictUnit:
    meta: dict = {"sibling_count": sibling_count}
    if node_span is not None:
        meta["enclosing_node_span"] = list(node_span)
    return ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text="def f():\n<<<<<<<\n    return 2\n=======\n    return 3\n>>>>>>>\n",
        marker_span=(1, 5),
        structural_metadata=meta,
    )


# ---------------------------------------------------------------------------
# Classifier: all signal combinations
# ---------------------------------------------------------------------------


def test_simple_isolated_hunk():
    """Single hunk, small node, short sides → simple."""
    u = _unit(node_span=(1, 5))  # 5-line node
    assert classify_difficulty(u) == "simple"


def test_multi_hunk_file_is_complex():
    """sibling_count > 0 → complex (the documented multi-hunk failure mode)."""
    u = _unit(sibling_count=1, node_span=(1, 5))
    assert classify_difficulty(u) == "complex"


def test_large_enclosing_node_is_complex():
    """Node larger than max_simple_node_lines → complex."""
    u = _unit(node_span=(1, 50))  # 50-line node, default threshold 40
    assert classify_difficulty(u) == "complex"


def test_node_at_threshold_is_simple():
    """Node exactly at the threshold (inclusive) → simple (>, not >=)."""
    u = _unit(node_span=(1, 40))  # 40 lines == threshold 40
    assert classify_difficulty(u) == "simple"


def test_large_side_text_is_complex():
    """Combined side text above max_simple_side_chars → complex."""
    big = "x" * 500
    u = _unit(base=big, current=big, replayed=big)  # 1500 chars > 1200
    assert classify_difficulty(u) == "complex"


def test_no_node_metadata_uses_other_signals():
    """Missing enclosing_node_span falls through to side-text/sibling checks."""
    u = _unit()  # no node_span, short sides, no siblings
    assert classify_difficulty(u) == "simple"


def test_disabled_thresholds_make_everything_simple():
    """Custom config that relaxes thresholds → a large hunk becomes simple."""
    u = _unit(sibling_count=0, node_span=(1, 200))
    cfg = RoutingConfig(
        enabled=True,
        complex_if_sibling_count_gt=0,
        max_simple_node_lines=500,  # very lenient
        max_simple_side_chars=10_000,
    )
    assert classify_difficulty(u, cfg) == "simple"


def test_sibling_threshold_respects_config():
    """complex_if_sibling_count_gt=2 → 1 sibling is still simple."""
    u = _unit(sibling_count=1, node_span=(1, 5))
    cfg = RoutingConfig(
        enabled=True, complex_if_sibling_count_gt=2,
        max_simple_node_lines=40, max_simple_side_chars=1200,
    )
    assert classify_difficulty(u, cfg) == "simple"


def test_non_numeric_sibling_count_treated_as_zero():
    u = _unit()
    u.structural_metadata["sibling_count"] = "garbage"
    assert classify_difficulty(u) == "simple"


def test_malformed_node_span_ignored():
    u = _unit()
    u.structural_metadata["enclosing_node_span"] = ["not", "numeric"]
    assert classify_difficulty(u) == "simple"


# ---------------------------------------------------------------------------
# Orchestrator integration: fast path vs full pipeline
# ---------------------------------------------------------------------------


def test_orchestrator_simple_unit_uses_fast_path(conflicted_repo):
    """A simple conflict (single hunk, short) takes the fast path: one sample,
    no two-pass, no consensus — when routing is enabled."""
    import json

    from capybase.config import Config
    from capybase.orchestrator import Orchestrator

    repo = conflicted_repo["repo"]

    class CountingClient:
        """Records how many complete() calls were made."""

        def __init__(self, payload):
            self.calls = 0
            self._payload = payload

        def complete(self, messages, **kw):
            self.calls += 1
            from capybase.adapters.llm_openai import LLMResponse

            return LLMResponse(text=self._payload)

    payload = json.dumps(
        {"resolved_text": "    return 'hi' + 'howdy'", "explanation": "m"}
    )
    client = CountingClient(payload)
    from capybase.resolution_engine import ResolutionEngine

    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    # Enable routing; the single-hunk short conflict is simple → ONE call.
    cfg.routing.enabled = True
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert not result.escalated, result.reason
    # Simple fast path: exactly one LLM call (no intent pass, no N samples).
    assert client.calls == 1


def test_orchestrator_routing_disabled_unchanged(conflicted_repo):
    """When routing.enabled is False, behavior is unchanged (no classify call,
    no difficulty_classified journal event). Sanity check for default-off."""
    import json

    from capybase.config import Config
    from capybase.orchestrator import Orchestrator

    repo = conflicted_repo["repo"]
    payload = json.dumps(
        {"resolved_text": "    return 'hi' + 'howdy'", "explanation": "m"}
    )
    from tests.test_orchestrator import CyclingClient
    from capybase.resolution_engine import ResolutionEngine

    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    # routing.enabled stays False (default).
    engine = ResolutionEngine(cfg.model, client=CyclingClient([payload]))
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert not result.escalated, result.reason
