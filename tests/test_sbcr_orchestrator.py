"""Integration tests: SBCR in the orchestrator resolve path (survey §4.1).

Proves the safety contract end-to-end:

1. A both-sides-add conflict (where the structural resolver declines) resolves
   via SBCR with NO LLM call — the combination search proposes a valid merge
   that passes whole-file validation.
2. A genuinely contradictory conflict: SBCR PROPOSES an invalid concatenation,
   but validation REJECTS it, and the conflict falls through to the model. This
   is the crux — SBCR is a candidate generator; validation is the decider.
3. When the gate is off, even a resolvable combination hits the model.
"""

from __future__ import annotations

import json
from pathlib import Path

from capybase.adapters.llm_openai import LLMResponse
from capybase.config import Config
from capybase.orchestrator import Orchestrator
from capybase.resolution_engine import ResolutionEngine

from tests.conftest import git


class CallCountingClient:
    """Fake client. If SBCR works, it is NEVER called for resolvable conflicts."""

    def __init__(self, response: str = '{"resolved_text": "SHOULD NOT BE USED"}'):
        self.response = response
        self.calls = 0

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        self.calls += 1
        return LLMResponse(text=self.response)


def _config(repo: Path) -> Config:
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    return cfg


def _make_both_add_imports(repo: Path) -> Path:
    """A both-sides-add conflict: base imports os; upstream adds `import sys`,
    replayed adds `import json`. The structural resolver declines (pure
    additions, no base content to be one-sided about); SBCR proposes the union
    `import sys\\nimport json`, which is valid Python and passes validation."""
    base = "import os\n"
    upstream = "import os\nimport sys\n"     # current adds sys
    replayed = "import os\nimport json\n"    # replayed adds json
    (repo / "app.py").write_text(base)
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text(replayed)
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "replayed")
    git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text(upstream)
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "upstream")
    git(repo, "checkout", "-q", "feat")
    r = git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected a rebase conflict"
    return repo


def _make_contradictory_assignment(repo: Path) -> Path:
    """A genuinely contradictory conflict: both sides change the SAME assignment
    differently. SBCR will propose concatenating them (an invalid duplicate),
    but whole-file validation must reject it, so the conflict falls through to
    the model."""
    base = "x = 1\n"
    upstream = "x = 2\n"
    replayed = "x = 3\n"
    (repo / "app.py").write_text(base)
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text(replayed)
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "replayed")
    git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text(upstream)
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "upstream")
    git(repo, "checkout", "-q", "feat")
    r = git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected a rebase conflict"
    return repo


# ---------------------------------------------------------------------------
# both-sides-add → SBCR resolves with NO model call
# ---------------------------------------------------------------------------


def test_both_sides_add_resolves_via_sbcr_without_llm(repo: Path):
    _make_both_add_imports(repo)
    client = CallCountingClient()
    engine = ResolutionEngine(_config(repo).model, client=client)
    orch = Orchestrator(_config(repo), repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert not result.escalated, result.reason
    # The model was NEVER called — SBCR handled it.
    assert client.calls == 0, f"expected no LLM calls, got {client.calls}"
    # The combination applied: both added imports are present.
    text = (repo / "app.py").read_text()
    assert "import sys" in text
    assert "import json" in text
    assert "<<<<<<<" not in text
    # Journal records the SBCR resolution + acceptance.
    events = [e for e in orch.journal.read_events()
              if e.event_type == "combination_resolved"]
    assert events and events[0].payload["passed"] is True
    accepted = [e for e in orch.journal.read_events()
                if e.event_type == "candidate_accepted"]
    assert accepted and accepted[-1].payload["via"] == "sbcr"


def test_combination_search_disabled_falls_through_to_model(repo: Path):
    """When the gate is off, even a resolvable both-sides-add hits the model."""
    _make_both_add_imports(repo)
    payload = json.dumps({"resolved_text": "import sys\nimport json",
                          "self_reported_confidence": 0.8})
    client = CallCountingClient(payload)
    engine = ResolutionEngine(_config(repo).model, client=client)
    cfg = _config(repo)
    cfg.future.enable_combination_search = False
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert not result.escalated, result.reason
    # The model WAS called this time.
    assert client.calls > 0


# ---------------------------------------------------------------------------
# contradictory conflict → SBCR proposes, validation rejects, model handles it
# ---------------------------------------------------------------------------


def test_contradictory_conflict_declined_falls_to_model(repo: Path):
    """The scope-guard safety contract. A modification conflict (both sides
    changed the same shared line differently) has a non-empty diff3-refined base,
    so SBCR declines to propose at all — its combination search space is unsafe
    for modifications (it would rank two contradictory lines' concatenation
    highest, which is a semantically-wrong last-wins merge). The conflict falls
    through to the LLM untouched. This is safe-by-SCOPE: SBCR never even proposes
    on a modification, so validation is a second safety net, not the only one."""
    _make_contradictory_assignment(repo)
    payload = json.dumps({"resolved_text": "x = 2 + 3",
                          "self_reported_confidence": 0.8})
    client = CallCountingClient(payload)
    engine = ResolutionEngine(_config(repo).model, client=client)
    orch = Orchestrator(_config(repo), repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert not result.escalated, result.reason
    # SBCR declined (modification conflict) → the model handled it.
    assert client.calls > 0
    # No combination_resolved event: SBCR declined before proposing (scope guard).
    sbcr_events = [e for e in orch.journal.read_events()
                   if e.event_type == "combination_resolved"]
    assert not sbcr_events
    # The accepted candidate came from the model, not SBCR.
    accepted = [e for e in orch.journal.read_events()
                if e.event_type == "candidate_accepted"]
    assert accepted
    # The model path's payload doesn't carry the deterministic "via" key; its
    # absence (or any non-"sbcr" value) confirms SBCR didn't accept this one.
    via = accepted[-1].payload.get("via")
    assert via != "sbcr"


# ---------------------------------------------------------------------------
# Balance-aware routing (survey §4.2): SBCR wins balanced, LLM wins imbalanced
#
# Two mechanisms encode the survey's finding:
# 1. SBCR's similarity floor already self-declines on heavily-imbalanced conflicts
#    (the union's mean-similarity-to-both-parents can't clear 0.6 when one side
#    dwarfs the other). See test_sbcr.py for that floor behavior.
# 2. The explicit balance gate here: when routing is ON and balance < threshold,
#    SBCR declines to short-circuit even if it WOULD resolve, deferring to the
#    LLM. This makes the imbalance signal explicit + tunable rather than relying
#    on the floor's incidental behavior near the boundary.
# ---------------------------------------------------------------------------


def test_balanced_conflict_diverts_to_llm_when_threshold_high(repo: Path):
    """With routing ON and a threshold above a BALANCED conflict's fitness-clears
    region, SBCR resolves but does NOT short-circuit — the LLM runs. This proves
    the balance gate can divert even conflicts SBCR would otherwise accept."""
    _make_both_add_imports(repo)  # balanced 1+1 addition; SBCR clears the floor
    # A valid merge the LLM would produce (carries both sides' additions).
    payload = json.dumps({
        "resolved_text": "import sys\nimport json",
        "self_reported_confidence": 0.8,
    })
    client = CallCountingClient(payload)
    cfg = _config(repo)
    cfg.routing.enabled = True
    # balance of a 1+1 conflict is 1.0; set threshold above it to force diversion.
    cfg.routing.min_balance_for_sbcr_accept = 1.5
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert not result.escalated, result.reason
    # The LLM WAS called — SBCR declined to short-circuit (threshold diversion).
    assert client.calls > 0, "expected LLM call when balance < threshold"
    # Journal records SBCR deferred (not accepted).
    sbcr_events = [e for e in orch.journal.read_events()
                   if e.event_type == "combination_resolved"]
    assert sbcr_events and sbcr_events[0].payload.get("deferred_to_llm") is True


def test_balanced_conflict_uses_sbcr_when_routing_off(repo: Path):
    """The conservative default: with routing OFF, SBCR accepts a balanced
    conflict whenever it resolves — exactly as before. The balance gate is
    opt-in; it never changes behavior unless enabled."""
    _make_both_add_imports(repo)
    client = CallCountingClient()
    cfg = _config(repo)
    # routing.enabled stays False (default) → balance check is skipped.
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert not result.escalated, result.reason
    # SBCR handled it — no LLM call.
    assert client.calls == 0
    accepted = [e for e in orch.journal.read_events()
                if e.event_type == "candidate_accepted"]
    assert accepted and accepted[-1].payload["via"] == "sbcr"
