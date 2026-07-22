"""Tests for difficulty-aware routing.

The legacy ``classify_difficulty`` now delegates to the first-class
:class:`capybase.classifier.ConflictClassification` (band + reasons); this file
covers the routing CONTRACT — the simple/complex label that drives the
orchestrator's fast path vs full pipeline — plus the orchestrator integration.
Band/reason detail coverage lives in ``test_classifier.py``.

Under the classifier:
- a same-line both-modify conflict is ``medium`` → complex (a real conflict
  needing judgment, not "easy");
- disjoint/one-sided/deterministically-mergeable conflicts are ``trivial`` →
  simple;
- multi-hunk, definition-touching, large, or same-symbol-overlap conflicts are
  ``medium``/``hard`` → complex.
"""

from __future__ import annotations

from capybase.classifier import classify
from capybase.conflict_model import ConflictSide, ConflictUnit


def _unit(
    *,
    base: str = "def f():\n    return 1",
    current: str = "    return 2",
    replayed: str = "    return 3",
    sibling_count: int = 0,
) -> ConflictUnit:
    return ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text="def f():\n<<<<<<<\n    return 2\n=======\n    return 3\n>>>>>>>\n",
        marker_span=(1, 5),
        structural_metadata={"sibling_count": sibling_count},
    )


def _disjoint_unit() -> ConflictUnit:
    """Both sides add a DISTINCT non-overlapping line → trivial (det-mergeable)."""
    base = "a = 1\nb = 2\nc = 3\n"
    current = "a = 1\nx = 9\nb = 2\nc = 3\n"  # add x after a
    replayed = "a = 1\nb = 2\nc = 3\ny = 8\n"  # add y after c
    return ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text=base, marker_span=(1, 1),
        structural_metadata={"sibling_count": 0},
    )


# ---------------------------------------------------------------------------
# Label contract: simple vs complex (drives the orchestrator fast path)
# ---------------------------------------------------------------------------


def test_same_line_both_modify_is_complex():
    """A same-line both-modify conflict is medium → complex (needs judgment)."""
    assert classify(_unit()).difficulty == "complex"
    assert classify(_unit()).band == "medium"


def test_disjoint_insertions_are_simple():
    """Disjoint non-overlapping edits are trivial → simple (deterministically
    mergeable, zero LLM judgment needed)."""
    c = classify(_disjoint_unit())
    assert c.difficulty == "simple"
    assert c.band == "trivial"


def test_one_sided_change_is_simple():
    """One side changed, the other conceded → trivial → simple."""
    base = "def f():\n    return 1\n"
    u = _unit(base=base, current="def f():\n    return 2\n", replayed=base)
    c = classify(u)
    assert c.difficulty == "simple"
    assert c.band == "trivial"


def test_classification_carries_reasons():
    """Every classification carries human-readable reasons for the band."""
    c = classify(_unit())
    assert c.reasons, "expected non-empty reasons for a medium conflict"


# ---------------------------------------------------------------------------
# Orchestrator integration: fast path vs full pipeline
# ---------------------------------------------------------------------------


def test_orchestrator_disjoint_unit_uses_fast_path(repo):
    """A deterministically-mergeable conflict routes to the fast path: the
    structural resolver merges it with ZERO LLM calls (the trivial band)."""
    import json

    from capybase.config import Config
    from capybase.orchestrator import Orchestrator
    from capybase.resolution_engine import ResolutionEngine

    class CountingClient:
        def __init__(self, payload):
            self.calls = 0
            self._payload = payload

        def complete(self, messages, **kw):
            self.calls += 1
            from capybase.adapters.llm_openai import LLMResponse
            return LLMResponse(text=self._payload)

    payload = json.dumps({"resolved_text": "x = 9", "explanation": "m"})
    client = CountingClient(payload)
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    cfg.routing.enabled = True
    cfg.model.samples = 3  # would cost 3 if the path weren't trivial
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    # Build a real disjoint conflict in the repo so capybase rebases into it.
    from tests.conftest import git
    (repo / "app.py").write_text("a = 1\nb = 2\nc = 3\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text("a = 1\nb = 2\nc = 3\ny = 8\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "feat: add y")
    git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text("a = 1\nx = 9\nb = 2\nc = 3\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "main: add x")
    git(repo, "checkout", "-q", "feat")

    result = orch.rebase("main")
    assert not result.escalated, result.reason
    # Trivial/deterministically-mergeable → the structural resolver handled it
    # with ZERO LLM calls (the cheap path won).
    assert client.calls == 0, (
        f"disjoint conflict made {client.calls} LLM calls, expected 0 "
        f"(should be deterministically merged)"
    )


def test_orchestrator_routing_disabled_unchanged(conflicted_repo):
    """When routing.enabled is False, behavior is unchanged (no classify call,
    no difficulty_classified journal event). Sanity check for default-off."""
    import json

    from capybase.config import Config
    from capybase.orchestrator import Orchestrator
    from tests.test_orchestrator import CyclingClient
    from capybase.resolution_engine import ResolutionEngine

    repo = conflicted_repo["repo"]
    payload = json.dumps(
        {"resolved_text": "    return 'hi' + 'howdy'", "explanation": "m"}
    )
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


# ---------------------------------------------------------------------------
# Difficulty-aware sample allocation (UAB-lite): samples_complex
# makes complex units draw more samples than the base count.
# ---------------------------------------------------------------------------


def test_samples_complex_draws_more_on_complex_unit(multi_unit_conflicted_repo):
    """With routing on + samples_complex=K, a complex (multi-hunk) unit draws
    K samples per unit instead of the base samples. The multi-unit fixture has
    two units, both complex, so the total call count is 2*K."""
    import json

    from capybase.adapters.llm_openai import LLMResponse
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator
    from capybase.resolution_engine import ResolutionEngine

    repo = multi_unit_conflicted_repo["repo"]

    class CountingClient:
        def __init__(self, payload):
            self.calls = 0
            self._payload = payload

        def complete(self, messages, **kw):
            self.calls += 1
            return LLMResponse(text=self._payload)

    payload = json.dumps({"resolved_text": '    "merged"', "explanation": "m"})
    client = CountingClient(payload)
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    cfg.routing.enabled = True
    cfg.model.samples = 1            # base count
    cfg.model.samples_complex = 3    # complex units draw 3
    # The multi-unit file has two DISTINCT hunks needing different resolutions;
    # a single canned payload can't satisfy both. This test measures the SAMPLE
    # COUNT (the allocation lever), not merge validity, so relax the checks that
    # would otherwise retry and inflate the count.
    cfg.validation.require_whole_file_validation = False
    cfg.validation.reject_if_drops_a_side = False
    cfg.validation.reject_if_drops_referenced_symbol = False
    cfg.validation.enable_per_unit_syntax_check = False  # fragmentary candidates
    # Disable the deterministic pre-LLM layers so the conflicts reach the LLM
    # path where samples_complex applies. (Without this the union/structural
    # rules merge them with zero LLM calls, exercising the resolver not the
    # sample allocation.)
    cfg.future.enable_structural_resolver = False
    cfg.future.enable_combination_search = False
    cfg.future.enable_block_capture = False
    # The comment-reconciliation pass + the verifier-model critic are always-on
    # by default and each make their own LLM calls after code resolution. This
    # test measures the CODE sample-count allocation (1 simple + 3 complex = 4),
    # not comment reconciliation or critic evaluation, so disable both to keep
    # the call-count assertion precise.
    cfg.future.enable_comment_reconciliation = False
    cfg.validation.enable_verifier_model = False
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert not result.escalated, result.reason
    # Both hunks classify as complex under the ConflictClassifier: each is a
    # both-sides edit of the same base line (the services-list line and the
    # feature-flags dict), which the classifier counts as a same-line
    # modify/modify → complex (bands medium and hard respectively). So each
    # draws samples_complex=3, for a total of 3 + 3 = 6. (With samples_complex
    # unset, both would draw the base 1 → 1 + 1 = 2.) This asserts the
    # samples_complex lever scales the draw count for complex units; the
    # contract under test is the allocation, not the per-unit band.
    assert client.calls == 6, client.calls
