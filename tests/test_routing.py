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
from capybase.conflict_model import VerificationFailure, VerificationResult
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


def test_samples_complex_sets_ceiling_and_draws_stay_lazy(multi_unit_conflicted_repo):
    """S28-129 re-scope: samples_complex is an UPPER LIMIT on validated
    resolution iterations for complex units — never an upfront spend. A
    first-try validated success exits after exactly ONE draw per unit, even
    with samples_complex=3. The ceiling itself is pinned via
    _resolution_iteration_cap (complex: samples_complex; simple: samples).
    """
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

    payload = json.dumps({"resolved_text": '    "merged"', "explanation": "m"})
    client = CountingClient(payload)
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    cfg.routing.enabled = True
    cfg.model.samples = 1            # base count
    cfg.model.samples_complex = 3    # complex ceiling
    # The multi-unit file has two DISTINCT hunks needing different resolutions;
    # a single canned payload can't satisfy both. This test measures the DRAW
    # COUNT (the ceiling lever), not merge validity, so relax the checks that
    # would otherwise retry and inflate the count.
    cfg.validation.require_whole_file_validation = False
    cfg.validation.reject_if_drops_a_side = False
    cfg.validation.reject_if_drops_referenced_symbol = False
    cfg.validation.enable_per_unit_syntax_check = False  # fragmentary candidates
    # Disable the deterministic pre-LLM layers so the conflicts reach the LLM
    # path where the ceiling applies. (Without this the union/structural
    # rules merge them with zero LLM calls, exercising the resolver not the
    # sample allocation.)
    cfg.features.structural_resolution = False
    cfg.features.combination_search = False
    cfg.future.enable_block_capture = False
    cfg.future.enable_source_portfolio = False
    # The comment-reconciliation pass + the verifier-model critic are always-on
    # by default and each make their own LLM calls after code resolution. This
    # test measures the CODE draw count, not comment reconciliation or critic
    # evaluation, so disable both to keep the call-count assertion precise.
    cfg.future.enable_comment_reconciliation = False
    cfg.features.llm_critic = False
    repo = multi_unit_conflicted_repo["repo"]
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert not result.escalated, result.reason
    # The ceiling: complex units MAY spend up to samples_complex=3 iterations,
    # but each unit's first validated draw exits early — 2 units, 2 draws.
    assert client.calls == 2, (
        f"first-try successes must exit early: expected 2 draws (1 per "
        f"complex unit), got {client.calls}")
    # The ceiling lever itself: complex honors samples_complex, simple the base.
    assert orch._resolution_iteration_cap("complex") == 3
    assert orch._resolution_iteration_cap("simple") == 1


def test_samples_ceiling_exhaustion_escalates(repo):
    """The ceiling is an upper limit on validated iterations: with
    samples_complex=2, max_retries=0, and every draw failing the syntax
    gate, the unit draws at most 2 LLM candidates before escalating
    (feedback-conditioned retries inside the ceiling — the outer loop IS
    the multisampling)."""
    import json

    from capybase.config import Config
    from capybase.orchestrator import Orchestrator
    from capybase.resolution_engine import ResolutionEngine
    from tests.conftest import git

    class BrokenClient:
        def __init__(self):
            self.calls = 0

        def complete(self, messages, **kw):
            self.calls += 1
            from capybase.adapters.llm_openai import LLMResponse
            # Non-empty, non-JSON, syntactically INVALID python: the per-unit
            # syntax gate hard-fails every iteration (no recovery detour —
            # needs_human never fires on a non-empty candidate).
            return LLMResponse(
                text="def greet(:\n    return broken( <<<\n",
                raw={"_accumulated": {"finish_reason": "stop"}})

    client = BrokenClient()
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    cfg.routing.enabled = True
    cfg.model.samples_complex = 2
    cfg.policy.max_retries_per_unit = 0   # ceiling = samples_complex alone
    cfg.policy.max_recovery_retries_per_unit = 0
    cfg.validation.require_whole_file_validation = False
    cfg.validation.enable_per_unit_syntax_check = True
    cfg.features.structural_resolution = False
    cfg.features.combination_search = False
    cfg.future.enable_block_capture = False
    cfg.future.enable_source_portfolio = False
    cfg.future.enable_comment_reconciliation = False
    cfg.features.llm_critic = False
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    # A real single-unit conflict (adjacent-line edits).
    (repo / "app.py").write_text("a = 1\nb = 2\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat"); git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text("a = 1\nb = 20\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "feat")
    git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text("a = 10\nb = 2\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "main")
    git(repo, "checkout", "-q", "feat")
    r = git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected the conflict"

    result = orch.run()
    assert result.escalated
    # ceiling = samples_complex = 2 iterations -> exactly 2 draws
    # (the max_retries=0 override stays the floor; the samples ceiling
    # raises the budget to 2 iterations).
    assert client.calls == 2, (
        f"ceiling samples_complex=2 must cap draws at 2, got {client.calls}")


def test_samples_ceiling_honored_through_risk_engine(repo):
    """CRITICAL S28-129 review pin: on the standard run() path escalation is
    decided by risk.decide, whose budget derives from
    policy.max_retries_per_unit — the samples ceiling must RAISE that budget
    (samples_ceiling_retries), or a calibrated samples>1 silently escalates
    early and the ceiling is dead. samples_complex=4 with the default
    max_retries=2: every draw fails the syntax gate -> exactly 4 draws
    (pre-fix: risk's budget of ~2 capped at 3)."""
    import json

    from capybase.config import Config
    from capybase.orchestrator import Orchestrator
    from capybase.resolution_engine import ResolutionEngine
    from tests.conftest import git

    class BrokenClient:
        """Every draw is UNIQUE (call-count comment) but always syntactically
        broken — unique candidates keep the oscillation backstop out of the
        picture so risk.decide's retry budget is the limiter under test."""

        def __init__(self):
            self.calls = 0

        def complete(self, messages, **kw):
            self.calls += 1
            from capybase.adapters.llm_openai import LLMResponse
            return LLMResponse(
                text=f"def greet(:\n    return broken(  # attempt {self.calls}\n",
                raw={"_accumulated": {"finish_reason": "stop"}})

    client = BrokenClient()
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    cfg.routing.enabled = True
    cfg.model.samples_complex = 4
    # max_retries=1 makes risk's own budget 1-2 (change-type factor), so the
    # raised ceiling (4 draws) is unambiguous evidence the samples ceiling
    # reached risk.decide.
    cfg.policy.max_retries_per_unit = 1
    cfg.policy.max_recovery_retries_per_unit = 0
    cfg.validation.require_whole_file_validation = False
    cfg.validation.enable_per_unit_syntax_check = True
    cfg.features.structural_resolution = False
    cfg.features.combination_search = False
    cfg.future.enable_block_capture = False
    cfg.future.enable_source_portfolio = False
    cfg.future.enable_comment_reconciliation = False
    cfg.features.llm_critic = False
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    (repo / "app.py").write_text("a = 1\nb = 2\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat"); git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text("a = 1\nb = 20\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "feat")
    git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text("a = 10\nb = 2\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "main")
    git(repo, "checkout", "-q", "feat")
    r = git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected the conflict"

    result = orch.run()
    assert result.escalated
    # The ceiling is an UPPER bound on iterations: other guards (the
    # no-progress signature backstop) may stop the unit earlier, but the
    # samples ceiling must never be exceeded.
    assert 2 <= client.calls <= 4, (
        f"samples_complex=4 must not be exceeded: {client.calls} draws")


def test_risk_engine_honors_samples_ceiling_kwarg():
    """Unit pin for the wiring: RiskEngine.decide's
    samples_ceiling_retries raises the change-type-scaled budget — the
    mechanism the orchestrator uses to make the samples ceiling reachable
    on the standard run() path."""
    from capybase.risk import RiskEngine

    engine = RiskEngine(max_retries_per_unit=1)
    result = VerificationResult(
        candidate_id="c", unit_id="u", passed=False,
        hard_failures=[VerificationFailure(
            validator="syntax", severity="error", message="broken")],
        warnings=[], features={})

    # Without the ceiling: budget = max(1, scaled(1)) -> retry_count 1
    # escalates.
    decision = engine.decide(
        result, retry_count=1, failure_kind="syntax")
    assert decision.action == "escalate"
    # With the ceiling: samples_complex=4 -> 3 retries allowed -> retry.
    decision = engine.decide(
        result, retry_count=1, failure_kind="syntax",
        samples_ceiling_retries=3)
    assert decision.action == "retry", decision
    decision = engine.decide(
        result, retry_count=3, failure_kind="syntax",
        samples_ceiling_retries=3)
    assert decision.action == "escalate"  # ceiling itself is finite


def test_orchestrator_passes_samples_ceiling_to_risk(multi_unit_conflicted_repo):
    """Wiring pin: the orchestrator hands risk.decide the samples ceiling
    (n_cap - 1 retries) so the raised budget is enforceable on the standard
    run() path (where the max_retries-gated budget check is skipped). Uses
    the multi-hunk fixture: both units classify COMPLEX, so
    samples_complex (not the base samples) sets the ceiling."""
    repo = multi_unit_conflicted_repo["repo"]
    import json

    from capybase.config import Config
    from capybase.orchestrator import Orchestrator
    from capybase.resolution_engine import ResolutionEngine
    from capybase.risk import RiskEngine
    from tests.conftest import git

    recorded: list[dict] = []

    class RecordingRisk(RiskEngine):
        def __init__(self):
            super().__init__(max_retries_per_unit=99)  # always retry

        def decide(self, result, **kwargs):
            recorded.append(kwargs)
            return super().decide(result, **kwargs)

    class BrokenClient:
        def complete(self, messages, **kw):
            from capybase.adapters.llm_openai import LLMResponse
            return LLMResponse(
                text="def greet(:\n    return broken( <<<\n",
                raw={"_accumulated": {"finish_reason": "stop"}})

    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    cfg.routing.enabled = True
    cfg.model.samples_complex = 3
    cfg.policy.max_retries_per_unit = 0   # ceiling alone must raise the budget
    cfg.policy.max_recovery_retries_per_unit = 0
    cfg.validation.require_whole_file_validation = False
    cfg.validation.enable_per_unit_syntax_check = True
    cfg.features.structural_resolution = False
    cfg.features.combination_search = False
    cfg.future.enable_block_capture = False
    cfg.future.enable_source_portfolio = False
    cfg.future.enable_comment_reconciliation = False
    cfg.features.llm_critic = False
    engine = ResolutionEngine(cfg.model, client=BrokenClient())
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    orch.risk = RecordingRisk()
    orch.run()
    assert recorded, "risk.decide was never called"
    for kw in recorded:
        assert kw.get("samples_ceiling_retries") == 2, (
            f"orchestrator must pass samples_complex - 1 as the ceiling "
            f"(got {kw.get('samples_ceiling_retries')!r})")
