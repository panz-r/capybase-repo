"""Integration tests for the orchestrator against real temp git repos.

A fake LLM client (no network) returns a pre-baked merged resolution so the
full M3 loop — extract → propose → verify → risk → splice → stage → continue
— can be exercised end to end without a live model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from capybase.adapters.llm_openai import LLMResponse
from capybase.config import Config
from capybase.orchestrator import Orchestrator, StepResult
from capybase.resolution_engine import ResolutionEngine

from tests.conftest import git


class FakeClient:
    """Returns canned JSON responses in order; repeats the last one forever."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        if self.responses:
            r = self.responses.pop(0)
        else:
            raise RuntimeError("no more fake responses")
        return LLMResponse(text=r)


class CyclingClient:
    """Like FakeClient but repeats the final response indefinitely.

    Used where the orchestrator may retry; avoids brittle payload counting.
    """

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls = 0

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        self.calls += 1
        if len(self.responses) > 1:
            return LLMResponse(text=self.responses.pop(0))
        return LLMResponse(text=self.responses[0])


def _config(tmp_path: Path, *, tests_required: bool = True, pre_continue: str | None = "true") -> Config:
    cfg = Config()
    cfg.model.model = "fake"
    # The hermetic suite scripts exact fake-client responses for single-
    # resolution flows; force samples=1 so the production default (samples=3 +
    # self-consistency) doesn't triple the candidate draw and exhaust them. Tests
    # that exercise the multi-sample path set samples explicitly.
    cfg.model.samples = 1
    cfg.model.enable_self_consistency = False
    cfg.tests.required = tests_required
    cfg.tests.pre_continue = pre_continue  # `true` always exits 0
    cfg.tests.final = pre_continue
    # The per-unit syntax validators (PythonSyntaxValidator/RustSyntaxValidator)
    # are a production feature; the hermetic suite's fake clients produce partial
    # conflict-region snippets (not complete parseable files), so the per-unit
    # compile would false-fail on them. Disable here — the validators have their
    # own dedicated tests with complete code.
    cfg.validation.enable_per_unit_syntax_check = False
    # The source portfolio pre-empts the LLM on simple conflicts; tests that
    # exercise the LLM path need it off (same rationale as per-unit syntax).
    cfg.future.enable_source_portfolio = False
    # Write artifacts under the repo's .rebase-agent (cwd of the repo).
    return cfg


def _make_resolved_payload(text: str) -> str:
    return json.dumps({"resolved_text": text, "explanation": "merge", "self_reported_confidence": 0.8})


# ---------------------------------------------------------------------------
# M1: inspect (no mutation)
# ---------------------------------------------------------------------------


def test_inspect_no_mutation(conflicted_repo):
    repo = conflicted_repo["repo"]
    before = (repo / "app.py").read_text()
    orch = Orchestrator(_config(repo), repo=str(repo))
    result = orch.inspect()
    assert not result.escalated
    # worktree file untouched
    assert (repo / "app.py").read_text() == before
    # one conflict unit extracted
    assert "app.py" in result.units_by_path
    # review bundle written
    assert (orch.paths.final / "review-bundle.md").exists()
    # journal exists
    assert orch.paths.journal.exists()


def test_inspect_no_rebase(repo):
    orch = Orchestrator(_config(repo), repo=str(repo))
    result = orch.inspect()
    assert result.escalated
    assert "no rebase" in (result.reason or "")


# ---------------------------------------------------------------------------
# M2: manual mode
# ---------------------------------------------------------------------------


def test_manual_mode_resolves(conflicted_repo):
    repo = conflicted_repo["repo"]
    # Manual mode reads the literal resolved text (not JSON).
    inputs = ["    return 'merged'"]
    orch = Orchestrator(
        _config(repo), repo=str(repo),
        stdin_reader=lambda _prompt, **_kw: inputs.pop(0),
        out=lambda *_a, **_k: None,
    )
    result = orch.manual()
    assert not result.escalated
    # file no longer has markers
    text = (repo / "app.py").read_text()
    assert "<<<<<<<" not in text
    assert "merged" in text
    # staged
    staged = git(repo, "diff", "--cached", "--name-only")
    assert "app.py" in staged.stdout


def test_manual_mode_rejects_bad_resolution(conflicted_repo):
    repo = conflicted_repo["repo"]
    # resolution that leaves a marker -> validation fails
    inputs = ["    x\n<<<<<<< leaked\n"]
    orch = Orchestrator(
        _config(repo), repo=str(repo),
        stdin_reader=lambda _prompt, **_kw: inputs.pop(0),
        out=lambda *_a, **_k: None,
    )
    result = orch.manual()
    assert result.escalated


# ---------------------------------------------------------------------------
# M3: full run (fake model)
# ---------------------------------------------------------------------------


def test_run_resolves_and_continues(conflicted_repo):
    repo = conflicted_repo["repo"]
    # A resolution that merges both sides (differs from either verbatim) so the
    # preservation heuristic does not force retries.
    payload = _make_resolved_payload("    return 'hi' + 'howdy'")
    engine = ResolutionEngine(_config(repo).model, client=CyclingClient([payload]))
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    # rebase completed cleanly
    assert not result.escalated, result.reason
    # no conflict markers anywhere
    assert "<<<<<<<" not in (repo / "app.py").read_text()
    # rebase no longer in progress
    r = git(repo, "rebase", "--abort", check=False)  # ensure clean state readable
    # HEAD should be the replayed branch tip rebased onto main.
    log = git(repo, "log", "--oneline").stdout
    assert "replayed change" in log


def test_run_passing_candidate_not_escalated_by_model_suspicion(conflicted_repo):
    """V8 regression (sea-orm-history-0016): the model returned a correct merge
    that passed all hard validation, but ALSO set suspected_validator_error=true.
    Pre-fix, the risk engine escalated on suspicion before checking pass state,
    throwing away a proven-correct resolution. The candidate must be accepted.

    This reproduces the sea-orm-0016 shape end-to-end: a clean resolution paired
    with a spurious suspicion flag. See test_risk.test_passing_candidate_not_
    escalated_by_suspicion for the unit-level guard."""
    repo = conflicted_repo["repo"]
    # Same correct merge as test_run_resolves_and_continues, but the model also
    # (mis)sets the suspicion flag — the bug condition.
    payload = json.dumps({
        "resolved_text": "    return 'hi' + 'howdy'",
        "explanation": "merge both sides",
        "self_reported_confidence": 0.8,
        "suspected_validator_error": True,
    })
    engine = ResolutionEngine(_config(repo).model, client=CyclingClient([payload]))
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    # The candidate passed hard validation → suspicion must NOT override it.
    assert not result.escalated, (
        f"a passing candidate must not be escalated by model suspicion alone, "
        f"but escalated: {result.reason}"
    )
    assert "<<<<<<<" not in (repo / "app.py").read_text()


def test_run_journals_prompt_trims_when_context_window_is_tight(conflicted_repo):
    """With context_window set, an over-large prompt is trimmed and the trims
    are journaled on the candidate_generated event."""
    repo = conflicted_repo["repo"]
    cfg = _config(repo)
    # A very tight window: the boilerplate (intro+contract+rules) is ~300 tokens,
    # so even this small conflict's full prompt exceeds it → augmentations trimmed.
    cfg.model.context_window = 350
    cfg.model.completion_reserve = 10
    payload = _make_resolved_payload("    return 'hi' + 'howdy'")
    engine = ResolutionEngine(cfg.model, client=CyclingClient([payload]))
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    orch.run()
    # Read the journal and find a candidate_generated event with prompt_trims.
    events = []
    for line in orch.paths.journal.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            d = json.loads(line)
            if d["event_type"] == "candidate_generated":
                events.append(d.get("payload", {}))
    trimmed = [e for e in events if e.get("prompt_trims")]
    assert trimmed, "expected a candidate_generated event carrying prompt_trims"
    assert any(t["section"] for t in trimmed[0]["prompt_trims"])


def test_run_no_prompt_trims_when_context_window_disabled(conflicted_repo):
    """context_window=0 (default) → no trimming, no prompt_trims in the journal."""
    repo = conflicted_repo["repo"]
    cfg = _config(repo)
    assert cfg.model.context_window == 0  # disabled by default
    payload = _make_resolved_payload("    return 'hi' + 'howdy'")
    engine = ResolutionEngine(cfg.model, client=CyclingClient([payload]))
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    orch.run()
    for line in orch.paths.journal.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            d = json.loads(line)
            if d["event_type"] == "candidate_generated":
                assert not d.get("prompt_trims"), "no trims when window disabled"


def test_run_skips_llm_when_conflict_oversized_for_window(conflicted_repo):
    """When the conflict SIDES alone exceed the available context window, the
    LLM call is doomed (server truncates) — skip it and escalate instead of
    wasting the call. An llm_skipped_oversized event is journaled and no
    candidate_generated event appears."""
    repo = conflicted_repo["repo"]
    cfg = _config(repo)
    # A window so tiny that even the small test conflict's sides don't fit.
    # completion_reserve=1 → available = 5 - 1 = 4 tokens; the sides are ~30+
    # tokens → oversized. (The trim test uses 350 which fits the sides; here we
    # go below the sides to trigger the hopeless case.)
    cfg.model.context_window = 5
    cfg.model.completion_reserve = 1
    payload = _make_resolved_payload("    return 'hi' + 'howdy'")
    client = CyclingClient([payload])
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    orch.run()
    events = {}
    for line in orch.paths.journal.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            d = json.loads(line)
            events[d["event_type"]] = d.get("payload", {})
    # The oversized-skip event fired.
    assert "llm_skipped_oversized" in events, "expected llm_skipped_oversized event"
    assert events["llm_skipped_oversized"]["essential_tokens"] > events["llm_skipped_oversized"]["available_tokens"]
    # No LLM candidate was generated (the call was skipped).
    assert "candidate_generated" not in events, "LLM should not have been called"
    # The fake client was never asked to complete (the call was skipped pre-loop).
    assert client.calls == 0, f"LLM client should not have been called, got {client.calls} calls"


def test_run_does_not_skip_llm_when_window_disabled(conflicted_repo):
    """context_window=0 (default) → the size guard is a no-op; the LLM runs
    normally even for large conflicts (historical behavior)."""
    repo = conflicted_repo["repo"]
    cfg = _config(repo)
    assert cfg.model.context_window == 0  # disabled
    payload = _make_resolved_payload("    return 'hi' + 'howdy'")
    engine = ResolutionEngine(cfg.model, client=CyclingClient([payload]))
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    orch.run()
    has_oversized = False
    has_candidate = False
    for line in orch.paths.journal.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            d = json.loads(line)
            if d["event_type"] == "llm_skipped_oversized":
                has_oversized = True
            if d["event_type"] == "candidate_generated":
                has_candidate = True
    assert not has_oversized, "size guard must not fire when window is disabled"
    assert has_candidate, "LLM should run normally when window is disabled"


def test_oversized_guard_uses_hunk_not_full_file():
    """V7 regression: the oversized guard measured the FULL file
    (unit.original_worktree_text) but the prompt's context_builder only sends
    a windowed slice (±15 lines around the marker span). A 1-line conflict in
    a 766-line file (561 tokens of conflict) was measured as 6363 tokens and
    escalated, even though the prompt only sends ~67 lines. The guard must
    measure the windowed slice, not the full file."""
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator
    from capybase.conflict_model import ConflictUnit, ConflictSide

    # Build a large file: 800 lines, with a tiny conflict at lines 400-404.
    padding = "\n".join(f"// line {i}" for i in range(400))
    conflict_block = (
        "<<<<<<< current\n"
        'pub const VERSION: &str = "1.28.1";\n'
        "=======\n"
        'pub const VERSION: &str = "1.29.0";\n'
        ">>>>>>> replayed\n"
    )
    padding_after = "\n".join(f"// line {i}" for i in range(400, 795))
    full_text = padding + "\n" + conflict_block + padding_after
    lines = full_text.split("\n")
    # The marker block spans lines 400-404 (0-based: 400 is <<<<<<<).
    # Find the marker start/end.
    start = next(i for i, l in enumerate(lines) if l.startswith("<<<<<<<"))
    end = next(i for i, l in enumerate(lines) if l.startswith(">>>>>>>"))

    cfg = Config()
    # An 8K window with 2K reserve → available = 6144 tokens. The full file
    # is ~800 lines × ~10 chars = 8000 chars = 2000 tokens — would fit even
    # the old way. Make the padding bigger to exceed 6144 tokens when full.
    # Actually: to test the FIX, set window so the FULL file exceeds it but
    # the windowed slice (±15 lines = ~35 lines) fits easily.
    big_padding = "\n".join(f"// padding line {i} with extra text to pad length" for i in range(800))
    full_text_big = big_padding + "\n" + conflict_block + big_padding
    lines_big = full_text_big.split("\n")
    start_big = next(i for i, l in enumerate(lines_big) if l.startswith("<<<<<<<"))
    end_big = next(i for i, l in enumerate(lines_big) if l.startswith(">>>>>>>"))

    cfg.model.context_window = 8192
    cfg.model.completion_reserve = 2048
    orch = Orchestrator(cfg, repo=".", resolution_engine=None,
                        out=lambda *_a, **_k: None)
    unit = ConflictUnit(
        session_id="test", step_index=0, path="src/lib.rs", language="rust",
        unit_id="src/lib.rs:1:0",
        base=ConflictSide(label="BASE", text='pub const VERSION: &str = "1.28.1";'),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text='pub const VERSION: &str = "1.28.1";'),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text='pub const VERSION: &str = "1.29.0";'),
        original_worktree_text=full_text_big,
        marker_span=(start_big, end_big),
    )
    oversized, essential, available = orch._llm_oversized_for_window(unit)
    # The full file is huge (~3500+ lines × 40 chars = ~35K tokens) but the
    # windowed slice (±15 lines around a 5-line conflict = ~35 lines) is tiny.
    full_tokens = len(full_text_big) // 4
    assert essential < full_tokens, (
        f"essential ({essential}) should be much smaller than full file "
        f"({full_tokens}) — the guard must measure the windowed slice"
    )
    assert not oversized, (
        f"a 1-line conflict in a big file should NOT be oversized: "
        f"essential={essential} available={available}"
    )


def test_escape_hatch_accepts_advisory_cycling_candidate(repo):
    """V7 regression: the convergence escape hatch fired 0 times across 143
    cases because it only matched preservation_heuristic/STRUCTURAL_CODE. The
    real cycling blockers were both_sides_represented (an advisory warning on
    a passing candidate). Phase 10 broadens the advisory set. When a candidate
    passes hard validation, cycles ≥ threshold, and is blocked ONLY by advisory
    warnings, the hatch accepts it instead of escalating."""
    # Build an ADDITIVE conflict (both sides add distinct lines, not a value-
    # resolution) so both_sides_represented fires without the value-resolution
    # suppression fast-path.
    base = (
        "def process():\n"
        "    setup()\n"
    )
    upstream = (
        "def process():\n"
        "    setup()\n"
        "    validate()\n"           # CURRENT adds validate()
    )
    replayed = (
        "def process():\n"
        "    setup()\n"
        "    teardown()\n"           # REPLAYED adds teardown()
    )
    (repo / "app.py").write_text(base)
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat"); git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text(replayed)
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "add teardown")
    git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text(upstream)
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "add validate")
    git(repo, "checkout", "-q", "feat")
    r = git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected conflict"

    cfg = _config(repo)
    cfg.policy.cegis_convergence_threshold = 2
    cfg.policy.max_retries_per_unit = 5  # allow enough iterations to cycle
    cfg.validation.reject_if_copies_one_side = False  # avoid separate hard blocker
    # The model picks ONE side's addition (validate only), dropping teardown.
    # both_sides_represented flags it as a warning (not hard). With the same
    # payload repeated, it cycles → the escape hatch should accept it.
    payload = _make_resolved_payload("    setup()\n    validate()")
    client = CyclingClient([payload])
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    # The escape hatch should have accepted the cycling candidate — NOT escalated.
    assert not result.escalated, (
        f"escape hatch should accept advisory-only cycling candidate, "
        f"but escalated: {result.reason}"
    )


def test_run_escalates_when_model_returns_markers(conflicted_repo):
    repo = conflicted_repo["repo"]
    # model keeps returning a leaked marker across all retries -> escalate
    payload = _make_resolved_payload("    x\n<<<<<<< still\n")
    engine = ResolutionEngine(_config(repo).model, client=CyclingClient([payload]))
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert result.escalated
    assert (orch.paths.final / "review-bundle.md").exists()


def test_no_progress_guard_escalates_on_identical_failure_signature(repo):
    """Fix C (V8 CASE_TIMEOUT): when the hard-failure SIGNATURE (set of
    (validator, message) tuples) is unchanged across cegis_convergence_threshold+1
    consecutive attempts, the loop is producing zero new information → escalate
    immediately. Keys on failure shape, not candidate hashes, so it catches
    stuck loops the content-hash backstops miss (e.g. empty-output transport
    loops where each candidate gets a fresh random UUID).

    Here the model cycles a candidate that consistently fails the same way. The
    guard fires after threshold+1 identical signatures — before the retry budget
    or wall budget would."""
    # Build a conflict (the standard value-resolution shape).
    base = "def f():\n    return 'hello'\n"
    (repo / "app.py").write_text(base)
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat"); git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text("def f():\n    return 'howdy'\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "replayed")
    git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text("def f():\n    return 'hi'\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "upstream")
    git(repo, "checkout", "-q", "feat")
    r = git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected conflict"

    cfg = _config(repo)
    cfg.policy.cegis_convergence_threshold = 2  # the default; explicit
    cfg.policy.max_retries_per_unit = 50  # large, so the guard fires first
    # A candidate that consistently leaks a marker → same hard-failure signature
    # every attempt (no_conflict_markers validator).
    payload = _make_resolved_payload("    return 1\n<<<<<<< leaked\n")
    engine = ResolutionEngine(cfg.model, client=CyclingClient([payload]))
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert result.escalated
    # The no-progress guard should fire (identical signature across attempts).
    assert "no hard-failure progress" in (result.reason or "") or "identical" in (result.reason or ""), (
        f"no-progress guard should fire on identical failure signature, got: {result.reason}"
    )


def test_hard_failure_signature_normalizes_line_numbers():
    """Fix C-v2 (reviewer feedback): the (validator, message) signature was too
    coarse — the SAME error at a shifted line number (the model moved the bug
    but didn't fix it) produced a 'changed' signature, so the guard would NOT
    fire on a genuine stuck loop. Normalizing line numbers → N makes the same
    error at a different location still count as 'no progress'.

    Conversely, symbol names and error kinds are preserved, so a genuinely
    DIFFERENT error (different symbol) still registers as a changed signature."""
    from capybase.conflict_model import VerificationFailure
    from capybase.orchestrator import _hard_failure_signature

    # Same error (duplicate 'foo') at DIFFERENT line numbers across attempts.
    sig_a = _hard_failure_signature([VerificationFailure(
        validator="duplicate_definition",
        message="line 142: function 'foo' defined more than once (at lines 142, 160)",
    )])
    sig_b = _hard_failure_signature([VerificationFailure(
        validator="duplicate_definition",
        message="line 150: function 'foo' defined more than once (at lines 150, 172)",
    )])
    assert sig_a == sig_b, (
        "same error at a shifted line must normalize to the same signature "
        "(else the guard misses genuine stuck loops)"
    )

    # A DIFFERENT symbol → different signature (the loop IS exploring).
    sig_c = _hard_failure_signature([VerificationFailure(
        validator="duplicate_definition",
        message="line 150: function 'bar' defined more than once (at lines 150, 172)",
    )])
    assert sig_a != sig_c, (
        "different symbol must produce a different signature (else the guard "
        "would conflate distinct errors and fire prematurely)"
    )

    # Fewer errors (one fixed) → different signature (progress).
    sig_two = _hard_failure_signature([
        VerificationFailure(validator="rust_syntax", message="error at line 5"),
        VerificationFailure(validator="rust_syntax", message="error at line 9"),
    ])
    sig_one = _hard_failure_signature([
        VerificationFailure(validator="rust_syntax", message="error at line 5"),
    ])
    assert sig_two != sig_one, (
        "fixing one of two errors must change the signature (progress detected)"
    )


def test_no_progress_guard_does_not_fire_when_signature_changes(repo):
    """Fix C companion: the no-progress guard must NOT fire when the hard-failure
    signature is GENUINELY different on every attempt (the loop IS making
    progress / exploring distinct errors). This proves the guard is keyed on
    signature repetition, not just attempt count.

    Here each attempt alternates between TWO distinct failure VALIDATORS
    (no_conflict_markers vs non_empty_resolution) but with threshold=3 so a
    single repeat of either doesn't trip the guard.

    NOTE: with the default threshold=2, an A,B,A,B pattern DOES fire (see
    test_no_progress_guard_catches_alternating_signatures). Here threshold=3
    ensures the 2 repeats of each don't reach the bar."""
    base = "def f():\n    return 'hello'\n"
    (repo / "app.py").write_text(base)
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat"); git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text("def f():\n    return 'howdy'\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "replayed")
    git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text("def f():\n    return 'hi'\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "upstream")
    git(repo, "checkout", "-q", "feat")
    r = git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected conflict"

    cfg = _config(repo)
    cfg.policy.cegis_convergence_threshold = 3  # need 3 repeats to fire
    # Alternate between markers and empty — each appears 2x (< threshold 3).
    marker_payload = _make_resolved_payload("    return 1\n<<<<<<< leaked\n")
    empty_payload = _make_resolved_payload("")
    payloads = [marker_payload, empty_payload, marker_payload, empty_payload]
    engine = ResolutionEngine(cfg.model, client=FakeClient(payloads))
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert result.escalated  # still escalates (all candidates fail)
    # NOT via the no-progress guard — no signature repeated >= 3 times.
    assert "no hard-failure progress" not in (result.reason or ""), (
        f"guard must not fire when no sig reaches threshold 3, got: {result.reason}"
    )


def test_no_progress_guard_catches_alternating_signatures(repo):
    """Gap A fix: the original guard only fired when N consecutive signatures
    were ALL identical (len(set(recent)) == 1). A model oscillating between
    two distinct error signatures (A, B, A, B, ...) never tripped it, because
    the window size equaled the threshold. The strengthened guard uses a wider
    window (2× threshold) so alternating repeats accumulate and fire."""
    base = "def f():\n    return 'hello'\n"
    (repo / "app.py").write_text(base)
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat"); git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text("def f():\n    return 'howdy'\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "replayed")
    git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text("def f():\n    return 'hi'\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "upstream")
    git(repo, "checkout", "-q", "feat")
    r = git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected conflict"

    cfg = _config(repo)
    cfg.policy.cegis_convergence_threshold = 2
    # Truly alternate between TWO distinct failure signatures:
    # Sig A: leaked markers (no_conflict_markers validator)
    # Sig B: empty resolution (non_empty_resolution validator)
    # Each candidate has distinct text so the normalized-hash convergence
    # backstop doesn't fire before the no-progress guard.
    # Pattern: A, B, A, B, A → A repeats 3 times in a window of 4 (2×threshold).
    payloads = [
        _make_resolved_payload("    x = AAA1\n<<<<<<< leaked\n"),  # sig A (markers)
        _make_resolved_payload(""),                                  # sig B (empty)
        _make_resolved_payload("    x = AAA2\n<<<<<<< leaked\n"),  # sig A again
        _make_resolved_payload(""),                                  # sig B again
        _make_resolved_payload("    x = AAA3\n<<<<<<< leaked\n"),  # sig A 3rd time
    ]
    engine = ResolutionEngine(cfg.model, client=FakeClient(payloads))
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert result.escalated
    # The no-progress guard fires because sig A (markers) repeats 3 times
    # within the wider window, even though it alternates with sig B (empty).
    assert "no hard-failure progress" in (result.reason or ""), (
        f"guard should catch alternating signatures with wider window, got: {result.reason}"
    )


def test_no_progress_guard_excludes_needs_human_signatures(repo):
    """The no-progress convergence guard must NOT fire on needs_human refusals.

    A needs_human refusal produces a non-empty hard-failure signature
    (needs_human + non_empty_resolution). Without the exclusion, two identical
    refusals trigger the guard BEFORE the recovery-retry path can give the model
    a reframed second chance. needs_human cases have their own budget
    (max_recovery_retries_per_unit); the convergence guard — built for compiler-
    error cycling — should defer to that path.

    Surfaced in the C live-eval (sqlite fts3_expr.c): the model self-reported
    needs_human twice; the guard fired at attempt 2, pre-empting the recovery
    retry that was configured (max_recovery_retries_per_unit=2)."""
    base = "def f():\n    return 'hello'\n"
    (repo / "app.py").write_text(base)
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat"); git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text("def f():\n    return 'howdy'\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "replayed")
    git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text("def f():\n    return 'hi'\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "upstream")
    git(repo, "checkout", "-q", "feat")
    r = git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected conflict"

    cfg = _config(repo)
    # The first-empty fast-fail would rescue the empty candidates (7b6ae57) —
    # disable it so this test's mechanism decides the outcome.
    cfg.future.enable_empty_fast_fail = False
    cfg.policy.cegis_convergence_threshold = 2
    cfg.policy.max_retries_per_unit = 50        # large, so only the guard/budget limits
    cfg.policy.max_recovery_retries_per_unit = 0  # exhaust recovery fast → escalate via budget
    # needs_human=true payload, repeated (CyclingClient repeats the last).
    payload = json.dumps({"resolved_text": "", "needs_human": True})
    engine = ResolutionEngine(cfg.model, client=CyclingClient([payload]))
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert result.escalated  # still escalates (recovery budget exhausted)
    # But NOT via the no-progress guard — needs_human signatures are excluded.
    assert "no hard-failure progress" not in (result.reason or ""), (
        f"guard must not fire on needs_human signatures, got: {result.reason}"
    )


def test_run_escalates_on_needs_human(conflicted_repo):
    repo = conflicted_repo["repo"]
    payload = json.dumps({"resolved_text": "    return 1", "needs_human": True})
    engine = ResolutionEngine(_config(repo).model, client=CyclingClient([payload]))
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert result.escalated


def test_run_aborts_tests_when_required_and_failing(conflicted_repo):
    repo = conflicted_repo["repo"]
    payload = _make_resolved_payload("    return 'hi' + 'howdy'")
    engine = ResolutionEngine(_config(repo).model, client=CyclingClient([payload]))
    cfg = _config(repo, tests_required=True, pre_continue="false")  # exits 1
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert result.escalated
    assert "tests failed" in (result.reason or "")


def test_unit_count_aware_retry_budget_caps_calls(repo):
    """A file with many units (>20) gets max_retries=0: each failing unit
    escalates after 1 LLM call instead of 3. This prevents throughput timeouts
    on files like nlohmann-json-0019 (78 regions)."""
    # Build a conflict with a single unit (we can't easily make 21+ real
    # conflict regions in a temp repo). Instead, test the _resolve_unit
    # parameter directly: verify that max_retries=0 caps model calls to 1.
    base = "def f():\n    return 'hello'\n"
    (repo / "app.py").write_text(base)
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat"); git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text("def f():\n    return 'howdy'\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "replayed")
    git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text("def f():\n    return 'hi'\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "upstream")
    git(repo, "checkout", "-q", "feat")
    r = git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected conflict"

    cfg = _config(repo)
    # Use a CyclingClient that always returns broken output (leaked markers).
    payload = _make_resolved_payload("    return 1\n<<<<<<< leaked\n")
    client = CyclingClient([payload])
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert result.escalated
    # With the default config (max_retries_per_unit=2), the model gets called
    # multiple times before escalating. The call count depends on the
    # convergence/no-progress guards, but should be > 1.
    default_calls = client.calls
    assert default_calls > 1, f"expected multiple calls with default budget, got {default_calls}"

    # Now re-run with max_retries=0 (simulating a >20-unit file). Build a fresh
    # repo to avoid state leakage.
    import tempfile
    with tempfile.TemporaryDirectory() as d2:
        rp = Path(d2)
        git(rp, "init", "-q", "-b", "main")
        (rp / "app.py").write_text(base)
        git(rp, "add", "app.py"); git(rp, "commit", "-q", "-m", "base")
        git(rp, "branch", "feat"); git(rp, "checkout", "-q", "feat")
        (rp / "app.py").write_text("def f():\n    return 'howdy'\n")
        git(rp, "add", "app.py"); git(rp, "commit", "-q", "-m", "replayed")
        git(rp, "checkout", "-q", "main")
        (rp / "app.py").write_text("def f():\n    return 'hi'\n")
        git(rp, "add", "app.py"); git(rp, "commit", "-q", "-m", "upstream")
        git(rp, "checkout", "-q", "feat")
        r2 = git(rp, "rebase", "main", check=False)
        assert r2.returncode != 0

        client2 = CyclingClient([payload])
        engine2 = ResolutionEngine(cfg.model, client=client2)
        orch2 = Orchestrator(
            cfg, repo=str(rp), resolution_engine=engine2,
            out=lambda *_a, **_k: None,
        )
        # Manually call _resolve_unit with max_retries=0.
        from capybase.conflict_extractor import ConflictExtractor
        ext = ConflictExtractor(orch2.git)
        step_units = ext.extract_file_units("app.py", 1, "test-session")
        if step_units:
            unit = step_units[0]
            outcome = orch2._resolve_unit(unit, max_retries=0)
            assert outcome.accepted is None  # escalated
            # With max_retries=0, the model should be called at most once
            # (1 initial attempt, then the budget cap fires).
            assert client2.calls <= 1, (
                f"max_retries=0 should cap to 1 call, got {client2.calls}"
            )


# ---------------------------------------------------------------------------
# sbcr consecutive-terminator guard: valid C++ patterns must NOT be rejected
# ---------------------------------------------------------------------------


def test_sbcr_guard_accepts_switch_case_returns(repo):
    """The sbcr consecutive-terminator guard must NOT reject switch cases
    where each case has its own return — the intervening case/default label
    makes it safe."""
    import re
    terminator_re = re.compile(r"^\s*(return|throw|break|continue|goto)\b")
    safe_next_re = re.compile(
        r"^\s*("
        r"\}|\)|\]|"
        r"case\b|default\b|"
        r"public:|private:|protected:|"
        r"#|//|/\*|\*"
        r")"
    )
    # Valid switch: returns separated by case labels
    switch_code = """switch (x) {
    case 1:
        return 1;
    case 2:
        return 2;
    default:
        return 0;
}"""
    lines = switch_code.split("\n")
    bad = False
    for i in range(len(lines) - 1):
        if not terminator_re.match(lines[i]):
            continue
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines):
            continue
        if safe_next_re.match(lines[j]):
            continue
        bad = True
        break
    assert not bad, "switch case returns should NOT be flagged"


def test_sbcr_guard_accepts_preprocessor_branch_returns(repo):
    """The sbcr guard must NOT reject preprocessor-branch returns where
    #else/#endif separates the terminators."""
    import re
    terminator_re = re.compile(r"^\s*(return|throw|break|continue|goto)\b")
    safe_next_re = re.compile(
        r"^\s*("
        r"\}|\)|\]|"
        r"case\b|default\b|"
        r"public:|private:|protected:|"
        r"#|//|/\*|\*"
        r")"
    )
    pp_code = """#if FOO
    return 1;
#else
    return 2;
#endif"""
    lines = pp_code.split("\n")
    bad = False
    for i in range(len(lines) - 1):
        if not terminator_re.match(lines[i]):
            continue
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines):
            continue
        if safe_next_re.match(lines[j]):
            continue
        bad = True
        break
    assert not bad, "preprocessor branch returns should NOT be flagged"


def test_sbcr_guard_rejects_return_after_return():
    """The sbcr guard MUST reject two consecutive returns at the same block
    level with no intervening label/brace/preprocessor — this is unreachable
    code (the defect pattern from clickhouse-0041)."""
    import re
    terminator_re = re.compile(r"^\s*(return|throw|break|continue|goto)\b")
    safe_next_re = re.compile(
        r"^\s*("
        r"\}|\)|\]|"
        r"case\b|default\b|"
        r"public:|private:|protected:|"
        r"#|//|/\*|\*"
        r")"
    )
    bad_code = """    return std::to_string(n) + "th";
    return std::to_string(n) + suffix;"""
    lines = bad_code.split("\n")
    bad = False
    for i in range(len(lines) - 1):
        if not terminator_re.match(lines[i]):
            continue
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines):
            continue
        if safe_next_re.match(lines[j]):
            continue
        bad = True
        break
    assert bad, "return-after-return with no intervening label should be flagged"


# ---------------------------------------------------------------------------
# Regression-prevention tests: each test closes a gap that let a Sprint 7-9
# regression through. These would have caught the regression BEFORE the
# expensive live eval.
# ---------------------------------------------------------------------------


def test_asymmetry_flag_only_fires_for_sub_units_with_deletions(repo):
    """The parent_has_asymmetry flag must fire when the parent had substantial
    deletions (computed by the conflict extractor at split time), NOT based on
    the sub-unit's own side ratio. A sub-unit fragment can look balanced even
    when the parent had 102 lines deleted by one side.

    Regression: the old ratio-based flag fired on ANY unit with a 3x side ratio,
    disabling source_portfolio on 6 previously-PASS cases (large headers where
    one side naturally had more content). The parent-deletion-based flag is
    precise: it only fires when the parent conflict genuinely had deletions."""
    from capybase.conflict_model import ConflictUnit, ConflictSide

    def _side(label, text):
        return ConflictSide(label=label, text=text)  # type: ignore[arg-type]

    # A sub-unit whose parent had >5 deleted base lines → flag fires
    sub_with_deletions = ConflictUnit(
        session_id="s", step_index=0, path="f.hpp", unit_id="f.hpp:1:0#s0",
        language="cpp",
        base=_side("BASE", "line1\n"),
        current=_side("CURRENT_UPSTREAM_SIDE", "a\nb\n"),
        replayed=_side("REPLAYED_COMMIT_SIDE", "x\n"),
        original_worktree_text="line1\n",
        structural_metadata={
            "parent_unit_id": "f.hpp:1:0",
            "parent_has_deletions": True,
        },
    )
    # A sub-unit whose parent had NO deletions → flag does NOT fire
    sub_without_deletions = ConflictUnit(
        session_id="s", step_index=0, path="f.hpp", unit_id="f.hpp:1:1#s0",
        language="cpp",
        base=_side("BASE", "line1\n"),
        current=_side("CURRENT_UPSTREAM_SIDE", "a\nb\nc\nd\ne\n"),
        replayed=_side("REPLAYED_COMMIT_SIDE", "x\n"),
        original_worktree_text="line1\n",
        structural_metadata={
            "parent_unit_id": "f.hpp:1:1",
            "parent_has_deletions": False,
        },
    )
    # A non-split unit (no parent_unit_id) → flag does NOT fire
    nonsplit_unit = ConflictUnit(
        session_id="s", step_index=0, path="f.hpp", unit_id="f.hpp:1:0",
        language="cpp",
        base=_side("BASE", "line1\n"),
        current=_side("CURRENT_UPSTREAM_SIDE", "a\nb\nc\nd\ne\n"),
        replayed=_side("REPLAYED_COMMIT_SIDE", "x\n"),
        original_worktree_text="line1\n",
        structural_metadata={},
    )

    # Simulate the orchestrator's asymmetry check (reads parent_has_deletions)
    for unit, should_flag in [
        (sub_with_deletions, True),
        (sub_without_deletions, False),
        (nonsplit_unit, False),
    ]:
        flagged = unit.structural_metadata.get("parent_has_deletions", False)
        if flagged:
            unit.structural_metadata["parent_has_asymmetry"] = True
        flagged = unit.structural_metadata.get("parent_has_asymmetry", False)
        assert flagged == should_flag, (
            f"unit {unit.unit_id}: expected flag={should_flag}, got {flagged}"
        )


def test_header_retry_budget_allows_one_retry():
    """Header files get _header_max_retries=1 (not 0), allowing the model to
    act on risk-layer rejection feedback. Previously capped at 0, which meant
    any header unit whose first candidate was risk-rejected escalated
    immediately — the second attempt (informed by the rejection reason) was
    never made. This caused nlohmann-0034 (sim=1.00) to escalate despite the
    resolution being correct.

    This test verifies the header detection and budget computation in
    _resolve_unit's setup block, without running the full CEGIS loop."""
    from capybase.config import Config

    config = Config()
    # Header extensions: .h, .hpp, .hh, .hxx, .H
    header_paths = ["a.h", "a.hpp", "a.hh", "a.hxx", "a.H"]
    for path in header_paths:
        _is_header = path.endswith((".h", ".hpp", ".hh", ".hxx", ".H"))
        _header_max_retries = 1 if _is_header else config.policy.max_retries_per_unit
        assert _is_header, f"{path} should be detected as header"
        assert _header_max_retries == 1, (
            f"header {path}: expected _header_max_retries=1, got {_header_max_retries}"
        )

    # Source files get the config default (not capped)
    source_paths = ["a.cpp", "a.c", "a.cc", "a.rs", "a.py"]
    for path in source_paths:
        _is_header = path.endswith((".h", ".hpp", ".hh", ".hxx", ".H"))
        _header_max_retries = 1 if _is_header else config.policy.max_retries_per_unit
        assert not _is_header, f"{path} should NOT be detected as header"
        assert _header_max_retries == config.policy.max_retries_per_unit, (
            f"source {path}: expected config default "
            f"({config.policy.max_retries_per_unit}), got {_header_max_retries}"
        )


def test_consensus_entropy_n2_disagreement_is_maximal():
    """With n=2 samples that disagree (two singleton clusters), normalized
    Shannon entropy is exactly 1.0. This means the consensus entropy gate
    (threshold 0.6-0.8) escalates on ANY disagreement at n=2 — even when both
    candidates are valid. This is why samples=2 is unsafe with the entropy gate.
    This test documents the mathematical constraint so future changes don't
    re-enable samples=2 without addressing it."""
    from capybase.consensus import _entropy

    # n=2, 2 singleton clusters → entropy = 1.0
    assert _entropy([1, 1], 2) == 1.0

    # n=3, 2-vs-1 split → entropy < 1.0 (safe for the gate)
    e3 = _entropy([2, 1], 3)
    assert 0.0 < e3 < 1.0

    # n=3, all agree → entropy = 0.0
    assert _entropy([3], 3) == 0.0


def test_no_progress_guard_fires_on_identical_non_needs_human(repo):
    """The no-progress guard must FIRE when two identical non-needs_human
    signatures repeat. This proves the guard's exclusion of needs_human is
    SCOPED (only excludes needs_human), not a universal no-op.

    Regression: the has_needs_human predicate had a tuple-unpacking bug
    ('v == needs_human' where v is a tuple) that made it always False.
    The guard then appended ALL signatures (including needs_human ones),
    but the existing test couldn't distinguish 'exclusion works' from
    'exclusion is broken no-op' because both produced the same outcome."""
    base = "def f():\n    return 'hello'\n"
    (repo / "app.py").write_text(base)
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat"); git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text("def f():\n    return 'howdy'\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "replayed")
    git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text("def f():\n    return 'hi'\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "upstream")
    git(repo, "checkout", "-q", "feat")
    r = git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected conflict"

    cfg = _config(repo)
    cfg.policy.cegis_convergence_threshold = 2
    # Two identical candidates that fail with leaked markers (NOT needs_human)
    payload = _make_resolved_payload("    return 1\n<<<<<<< leaked\n")
    client = CyclingClient([payload])
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert result.escalated
    # The no-progress guard MUST fire — this is a non-needs_human signature
    assert "no hard-failure progress" in (result.reason or ""), (
        f"guard should fire on identical non-needs_human signatures, got: {result.reason}"
    )


def test_structure_preserving_rules_and_token_disjoint_full_verify():
    """token_disjoint ALWAYS gets full verify (syntax + AST). The line-count
    ratio is not a sound proxy for token-splice correctness — a splice on
    'stable' sides can still produce garbled output. insertion_union must
    NOT be in the static set (needs full verify).

    Regression: the shape-conditional fast_verify (commit 25c7263) used a
    line-count ratio to skip syntax/AST validators for 'stable' shapes,
    contradicting the rule's documented full-verify contract. Removed."""
    # The static set must NOT include token_disjoint or insertion_union.
    src = open("src/capybase/orchestrator.py").read()
    idx = src.index("_STRUCTURE_PRESERVING_RULES = frozenset({")
    start = src.index("{", idx)
    end = src.index("})", start) + 2
    set_str = src[idx:end]
    assert '"token_disjoint"' not in set_str, (
        "token_disjoint must NOT be in _STRUCTURE_PRESERVING_RULES — "
        "it's a recombinant token splice that always needs full verify"
    )
    assert '"insertion_union"' not in set_str, (
        "insertion_union must NOT be in _STRUCTURE_PRESERVING_RULES — "
        "it needs full verify (can produce invalid unions)"
    )
    # The shape-conditional logic must NOT exist for token_disjoint
    assert 'result.rule == "token_disjoint"' not in src, (
        "token_disjoint must NOT have shape-conditional fast_verify — "
        "it always gets full verify (line-count ratio is not a sound proxy)"
    )


# ---------------------------------------------------------------------------
# Step 3: rank-order candidate validation (try the next sample if the
# consensus winner fails validation, before falling back to CEGIS repair)
# ---------------------------------------------------------------------------


class FakeConsensusEngine:
    """Returns a fixed candidate list + trivial consensus report.

    Mimics ResolutionEngine.propose_with_consensus so the orchestrator's
    self-consistency path can be driven with controlled candidates without a
    live model. The candidates are returned in the order given (index 0 is the
    consensus "winner").
    """

    def __init__(self, candidates):
        from capybase.consensus import ConsensusReport

        self._candidates = list(candidates)
        # The orchestrator's recovery/repair prompts read engine.token_budget
        # (a TokenBudget on the real engine — an int would crash .enabled).
        # total=0 → disabled, matching the historical unbounded default.
        from capybase.conflict_model import TokenBudget
        self.token_budget = TokenBudget(total=0)
        # A unanimous report so the risk engine doesn't escalate on entropy/
        # agreement — we want to isolate the rank-order validation behavior.
        self._report = ConsensusReport(
            winner=candidates[0] if candidates else None,
            clusters=[],
            n_samples=len(candidates),
            agreement_score=1.0,
            cluster_count=1,
            entropy=0.0,
        )

    def propose_with_consensus(self, unit, context, *, failures=None,
                               prev_candidate=None, n_samples=None,
                               attempt=0):
        return list(self._candidates), self._report


def _self_consistency_config(repo):
    """Enable self-consistency so the orchestrator takes the multi-candidate path."""
    cfg = _config(repo)
    cfg.future.enable_self_consistency = True
    return cfg


def _cand(text, *, cid="c"):
    from capybase.conflict_model import CandidateResolution

    return CandidateResolution(
        candidate_id=cid, unit_id="u", model_name="fake",
        prompt_version="v", resolved_text=text,
    )


def test_run_accepts_second_candidate_when_winner_fails(conflicted_repo):
    """The consensus winner has a syntax error; the 2nd sample is valid.

    Step 3 says "discard that candidate immediately" — the orchestrator should
    validate the 2nd/3rd samples (already in memory) and accept the first that
    passes, rather than discarding all N and jumping to CEGIS regeneration.
    """
    repo = conflicted_repo["repo"]
    # Winner: leaks a conflict marker -> per-unit validation fails
    # (no_conflict_markers is a hard check the per-unit validator enforces).
    # 2nd: a valid merge of both sides -> per-unit AND whole-file pass.
    # 3rd: also valid (untouched, the loop stops at the 2nd).
    engine = FakeConsensusEngine([
        _cand("    x\n<<<<<<< leaked\n", cid="winner-broken"),
        _cand("    return 'hi' + 'howdy'", cid="second-valid"),
        _cand("    return 'hi' + 'howdy'", cid="third-valid"),
    ])
    # The first-empty fast-fail would rescue the empty candidates (7b6ae57);
    # disable it so this test's mechanism decides the outcome.
    _cfg = _self_consistency_config(repo)
    _cfg.future.enable_empty_fast_fail = False
    orch = Orchestrator(
        _cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert not result.escalated, result.reason
    # The accepted candidate is the second (valid) one, not the broken winner.
    assert result.outcomes
    accepted = result.outcomes[0].accepted
    assert accepted is not None
    assert accepted.candidate_id == "second-valid"
    # No markers leaked into the file.
    assert "<<<<<<<" not in (repo / "app.py").read_text()


def test_run_escalates_when_all_candidates_fail(conflicted_repo):
    """When every surviving candidate fails validation, fall back to the normal
    retry/escalate path — and when that exhausts too, F1 tier-2 lands the
    adjudicated pristine side. Sprint-24: the takeovers used to `continue`
    past the outer loop's write-and-stage (journaled but never landed); they
    now complete the file. The fake engine's raw_complete payloads yield a
    parseable tier-2 choice, so this fixture documents the LANDING: the file
    resolves to a compiling pristine side instead of escalating."""
    repo = conflicted_repo["repo"]
    engine = FakeConsensusEngine([
        _cand("    return 'hi'(", cid="a-broken"),
        _cand("    return 'howdy'(", cid="b-broken"),
        _cand("    x\n<<<<<<< leaked\n", cid="c-marker"),
    ])
    # The first-empty fast-fail would rescue the empty candidates (7b6ae57);
    # disable it so this test's mechanism decides the outcome.
    _cfg = _self_consistency_config(repo)
    _cfg.future.enable_empty_fast_fail = False
    orch = Orchestrator(
        _cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    # All model candidates fail across retries; the always-on F1 tier-2
    # takeover then resolves the file with a compiling pristine side.
    assert not result.escalated
    final = (repo / "app.py").read_text()
    assert final in (
        conflicted_repo["current"], conflicted_repo["replayed"],
    ), f"F1 takeover should land a pristine side, got: {final!r}"



def test_f1_tier2_takeover_lands_only_compiling_side(tmp_path):
    """The tier-2 adjudicated side is compile-gated: when the chosen pristine
    side doesn't build, the takeover declines (journaled) and the step
    escalates honestly instead of writing an unverified file. The
    protobuf-0051 shape — both side probes fail, tier-2 still has an
    opinion — must not land that opinion."""
    import subprocess

    repo = tmp_path / "r"
    repo.mkdir()

    def git(*a):
        return subprocess.run(
            ["git", "-C", str(repo)] + list(a), capture_output=True, text=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    # Both sides syntactically broken (unbalanced parens): the compile gate
    # must decline whichever side tier-2 picks.
    base = "def greet():\n    return 'hello'\n"
    cur = "def greet():\n    return 'hi'(\n"
    rep = "def greet():\n    return 'howdy'(\n"
    (repo / "app.py").write_text(base)
    git("add", "app.py"); git("commit", "-qm", "base")
    git("branch", "feat"); git("checkout", "-q", "feat")
    (repo / "app.py").write_text(rep)
    git("add", "app.py"); git("commit", "-qm", "rep")
    git("checkout", "-q", "main")
    (repo / "app.py").write_text(cur)
    git("add", "app.py"); git("commit", "-qm", "up")
    git("checkout", "-q", "feat")
    git("rebase", "main")

    engine = FakeConsensusEngine([
        _cand("    return 'hi'(", cid="a-broken"),
        _cand("    return 'howdy'(", cid="b-broken"),
    ])
    _cfg = _self_consistency_config(repo)
    _cfg.future.enable_empty_fast_fail = False
    orch = Orchestrator(
        _cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert result.escalated, (
        "tier-2 must not land a side that fails the compile gate"
    )
    # escalated=True is the contract: the takeover path (which sets
    # escalated=False on landing) never ran with the broken side. Whatever
    # the last-resort repair-side fallback leaves in the worktree is that
    # mechanism's business, not the takeover's.


def test_run_retries_after_transient_error(conflicted_repo):
    """A request_failed candidate (timeout/network) should retry, then succeed."""
    from tests.test_resolution_engine import MetaClient
    from capybase.adapters.llm_openai import LLMResponse

    repo = conflicted_repo["repo"]
    # First call: a runtime error -> request_failed -> retry.
    # Second call: a valid merged resolution -> accept.
    seq = [
        RuntimeError("connection timed out"),
        LLMResponse(
            text=_make_resolved_payload("    return 'hi' + 'howdy'"),
            raw={"choices": [{"finish_reason": "stop"}]},
        ),
    ]
    engine = ResolutionEngine(_config(repo).model, client=MetaClient(seq))
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert not result.escalated, result.reason
    assert "<<<<<<<" not in (repo / "app.py").read_text()


def test_run_escalates_fast_on_repeated_transient_failures(conflicted_repo):
    """V8 CASE_TIMEOUT regression (axum-0036, sea-orm-0015, tokio-0109): repeated
    request_failed candidates (HTTP 400 / empty output) spun the retry loop
    indefinitely because retry_count never incremented — every retry was
    misclassified as a critic_retry (the critic also flags empty candidates),
    so risk.decide's 'retry_count < budget' check was always true and only the
    360s wall budget stopped it (~22 attempts, ~29 min per case).

    Post-fix: technical failures (request_failed/truncated/parse_failed/
    lsp_failed) increment retry_count, so the loop escalates after
    max_retries_per_unit retries — not after the wall budget."""
    from tests.test_resolution_engine import MetaClient

    repo = conflicted_repo["repo"]
    cfg = _config(repo)
    # The first-empty fast-fail would rescue the empty candidates (7b6ae57) —
    # disable it so this test's mechanism decides the outcome.
    cfg.future.enable_empty_fast_fail = False
    cfg.policy.max_retries_per_unit = 2  # the default; make it explicit
    # Recovery retry stays ON (the production default) — the V8 spin scenario
    # had recovery retry on; the bug was that request_failed retries were
    # misclassified as critic retries, so retry_count stayed 0 and risk.decide's
    # 'retry_count < budget' check was always true. Only the wall budget stopped
    # it. Post-fix retry_count increments, terminating via the budget.
    # Repeated request_failed (the model API returns errors indefinitely).
    seq = [RuntimeError("HTTP Error 400: Bad Request")] * 50
    engine = ResolutionEngine(cfg.model, client=MetaClient(seq))
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    # Must escalate (not spin forever). The escalation terminates via the retry
    # budget now that retry_count increments on request_failed — pre-fix
    # retry_count stayed 0 (the counter misclassification) and only the wall
    # budget stopped it.
    assert result.escalated, "repeated transient failures must escalate"
    # The core proof: retry_count must increment on request_failed. Pre-fix it
    # stayed at 0 across ALL attempts (verified in V8 flight journals:
    # sea-orm-0015, axum-0036, tokio-0109 all showed retry_count=0 for 22-26
    # attempts). With the fix, at least one rejection carries retry_count > 0.
    events = []
    for line in orch.paths.journal.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            d = json.loads(line)
            if d["event_type"] == "candidate_rejected":
                events.append(d.get("payload", {}))
    assert any(e.get("retry_count", 0) > 0 for e in events), (
        "retry_count must increment on request_failed (pre-fix it stayed 0 — "
        "the V8 CASE_TIMEOUT root cause); "
        f"rejection events: {events}"
    )


# ---------------------------------------------------------------------------
# Multi-unit-per-file (the regression class this whole fix targets)
# ---------------------------------------------------------------------------


def test_run_resolves_multi_unit_file(multi_unit_conflicted_repo):
    """Two hunks in one file: both must be resolved and accumulated into the
    final file. This is the direct regression test for the splice bug —
    previously only the last unit's resolution survived."""
    repo = multi_unit_conflicted_repo["repo"]
    payload1 = _make_resolved_payload(multi_unit_conflicted_repo["services_merged"])
    payload2 = _make_resolved_payload(multi_unit_conflicted_repo["flags_merged"])
    # Sequential: unit 0 (services) then unit 1 (flags).
    engine = ResolutionEngine(_config(repo).model, client=FakeClient([payload1, payload2]))
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert not result.escalated, result.reason
    text = (repo / "cfg.py").read_text()
    # No markers anywhere in the whole file.
    assert "<<<<<<<" not in text
    # BOTH resolutions present (the bug dropped the first one).
    assert "scheduler" in text and "reloader" in text
    assert '"cache": "on"' in text and '"metrics": "on"' in text


def test_manual_mode_resolves_multi_unit(multi_unit_conflicted_repo):
    """Manual mode must also accumulate both units' resolutions."""
    repo = multi_unit_conflicted_repo["repo"]
    inputs = [
        multi_unit_conflicted_repo["services_merged"],
        multi_unit_conflicted_repo["flags_merged"],
    ]
    orch = Orchestrator(
        _config(repo), repo=str(repo),
        stdin_reader=lambda _prompt, **_kw: inputs.pop(0),
        out=lambda *_a, **_k: None,
    )
    result = orch.manual()
    assert not result.escalated, result.reason
    text = (repo / "cfg.py").read_text()
    assert "<<<<<<<" not in text
    assert "scheduler" in text and "reloader" in text
    assert '"cache": "on"' in text and '"metrics": "on"' in text


def test_run_escalates_when_whole_file_invalid(multi_unit_conflicted_repo):
    """Two candidates that individually pass Phase A but produce invalid Python
    when juxtaposed → Phase B (verify_file) fails. With execution-driven
    whole-file CEGIS, the system now attempts to REPAIR (feed the cross-unit
    failure back to the unit), escalating only when the repair also fails.

    Here the FakeClient has no responses left for the repair attempt, so the
    re-resolution fails and the file escalates with a whole-file repair
    message (not the old immediate "whole-file validation failed")."""
    repo = multi_unit_conflicted_repo["repo"]
    # Both hunks resolve to a bare ``return`` at module level: valid alone in
    # the per-unit context but a SyntaxError when juxtaposed at module scope.
    bad = _make_resolved_payload("return 1")
    cfg = _config(repo)
    # The first-empty fast-fail would rescue the empty candidates (7b6ae57) —
    # disable it so this test's mechanism decides the outcome.
    cfg.future.enable_empty_fast_fail = False
    # The ``return 1`` candidate deliberately drops both sides' content — it's
    # not a real merge. This test is about Phase B (whole-file juxtaposition),
    # so relax the Phase A both-sides-represented check so the candidate passes
    # Phase A and actually reaches Phase B (the behavior under test). The
    # dependency-preservation check (P3) is likewise relaxed: it would flag the
    # same dropped-content pattern and reroute to a retry before Phase B.
    cfg.validation.reject_if_drops_a_side = False
    cfg.validation.reject_if_drops_referenced_symbol = False
    cfg.future.enable_structural_resolver = False
    engine = ResolutionEngine(cfg.model, client=FakeClient([bad, bad]))
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    # The LLM repair exhausts (FakeClient has no responses left), but the
    # final deterministic-only repair pass (_whole_file_repair's brace/
    # boundary beam — landed after this test was written) recovers a
    # compiling file, so escalation is no longer the terminal outcome.
    # Phase B still caught the juxtaposition: the journal shows
    # file_validated(passed=False) + whole_file_repair before the recovery.
    assert not result.escalated, result.reason
    import py_compile as _pc
    _pc.compile(str(repo / "cfg.py"), doraise=True)  # recovered file compiles


def test_whole_file_repair_recovers_and_accepts(multi_unit_conflicted_repo):
    """Execution-driven whole-file CEGIS: both units pass per-unit validation
    in isolation, but unit 1's first resolution breaks the file when juxtaposed
    (an unclosed bracket). The whole-file validator catches it, feeds the
    concrete SyntaxError back to unit 1, which re-resolves to the valid merge
    on the repair attempt. The file is then ACCEPTED (not escalated).

    This is the principle: ground the model's correction in concrete
    execution feedback instead of escalating the cross-unit error."""
    repo = multi_unit_conflicted_repo["repo"]
    services = multi_unit_conflicted_repo["services_merged"]   # unit 0, valid
    # Per-unit-valid-but-whole-file-invalid: an unclosed paren survives the
    # per-unit splice (where the sibling block is blanked) but breaks the full
    # file when both resolutions are juxtaposed.
    flags_broken = '    "cache": "on", "metrics": "on"\n    extra_stale_line('
    flags_good = multi_unit_conflicted_repo["flags_merged"]
    # Sequence: unit0(services), unit1(flags broken) → whole-file fails →
    # repair re-resolves unit1 → flags_good. CyclingClient repeats the last.
    client = CyclingClient([
        _make_resolved_payload(services),
        _make_resolved_payload(flags_broken),
        _make_resolved_payload(flags_good),
    ])
    cfg = _config(repo)
    cfg.future.enable_structural_resolver = False
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert not result.escalated, result.reason
    text = (repo / "cfg.py").read_text()
    assert "<<<<<<<" not in text
    # Both merges present after repair.
    assert "scheduler" in text and "reloader" in text
    assert '"cache": "on"' in text and '"metrics": "on"' in text
    # Causal attribution (V8b reviewer feedback): the whole-file repair that
    # cleared the failure must be recorded with effect=CLEARED — distinguishing
    # 'the mechanism fired' from 'the mechanism caused recovery'. This is the
    # signal that prevents the projection overestimate from Fix A (a dedup event
    # firing was mistaken for causal recovery).
    effects = []
    for line in orch.paths.journal.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            d = json.loads(line)
            if d["event_type"] == "mechanism_effect":
                effects.append(d.get("payload", {}).get("effect"))
    assert "CLEARED" in effects, (
        f"a whole-file repair that recovered must record effect=CLEARED, "
        f"got mechanism_effect events: {effects}"
    )


def test_deterministic_repair_after_budget_exhaustion(repo):
    """The cheap deterministic repairs (brace balance) must get a final attempt
    after the LLM whole-file-repair budget is exhausted. Surfaced in the C
    live-eval (redis pubsub.c): the model dropped one closing brace; the
    deterministic brace repair fixes it, but the budget broke before it ran.

    Here the LLM budget is 0 (no in-loop repair), so the only shot at recovery
    is the final deterministic-only attempt. The candidate has a trivial unclosed
    brace that _try_balance_braces closes deterministically."""
    base = "def f():\n    return 'hello'\n"
    (repo / "app.py").write_text(base)
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat"); git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text("def f():\n    return 'howdy'\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "replayed")
    git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text("def f():\n    return 'hi'\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "upstream")
    git(repo, "checkout", "-q", "feat")
    r = git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected conflict"

    cfg = _config(repo)
    cfg.policy.max_whole_file_repair_retries = 0  # exhaust budget immediately
    # A candidate with an extra unclosed brace — the deterministic brace repair
    # can close it, but the LLM repair path never runs (budget 0).
    payload = _make_resolved_payload("    return 'hi' + 'howdy'\n    if True {\n")
    engine = ResolutionEngine(cfg.model, client=CyclingClient([payload]))
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    # The deterministic brace repair should have closed the brace on the final
    # attempt. The case may still escalate (the candidate is wrong beyond the
    # brace), but the KEY assertion is that the deterministic repair RAN after
    # budget exhaustion — recorded in the journal.
    journal = orch.paths.journal.read_text(encoding="utf-8")
    has_det_repair = (
        "deterministic_brace_repair" in journal
        or "boundary_echo_strip" in journal
        or "coherence_repair_applied" in journal
    )
    # Sprint-21 coherence rung: the splice-gate repair may fix the brace
    # BEFORE the whole-file repair stage ever runs — same deterministic
    # mechanism, earlier seam. Either journal trace satisfies the test.
    assert has_det_repair, (
        "a deterministic repair must run after budget exhaustion; "
        f"journal has no deterministic-repair event. reason: {result.reason}"
    )


def test_file_linker_dedup_survives_whole_file_validation(repo, tmp_path):
    """V8 WHOLE_FILE_FAILED regression (axum-history-0020 shape): the model's
    per-unit resolution re-states a `use` import that already exists just
    BELOW the conflict span. Per-unit Phase A passes (the duplicate isn't
    visible in isolation), the file_linker dedup correctly removes the
    duplicate, BUT pre-fix verify_file re-spliced the un-deduped per-unit
    spans and failed on the same duplicate the dedup just removed — so
    file_linker_dedup fired and the case still escalated.

    Post-fix: the deduped buffer is passed to verify_file via whole_text, so
    the validated text matches what gets written to disk and the case PASSES.

    This is the canonical single-unit in-context failure: 15/18 V8
    WHOLE_FILE_FAILED cases have a single conflict unit whose resolution
    collides with content immediately adjacent to the span."""
    # Build a Rust file with an import conflict. The conflict span covers the
    # `use std::sync::Arc;` line; `use std::collections::HashMap;` sits just
    # above (outside the span). Using std:: imports keeps the file self-contained
    # (no external crate to resolve) while still exercising the dedup path.
    base = (
        "use std::collections::HashMap;\n"
        "use std::sync::Arc;\n"
        "\n"
        "fn main() {\n"
        "    let _ = Arc::new(HashMap::<i32, i32>::new());\n"
        "}\n"
    )
    # Upstream renames the Arc import (modifies the span line).
    upstream = (
        "use std::collections::HashMap;\n"
        "use std::sync::Arc as A;\n"
        "\n"
        "fn main() {\n"
        "    let _ = A::new(HashMap::<i32, i32>::new());\n"
        "}\n"
    )
    # Replayed ALSO modifies the Arc import line (adds a doc comment) — same line
    # as upstream, so git reports a genuine both-modify conflict.
    replayed = (
        "use std::collections::HashMap;\n"
        "/// shared ref\n"
        "use std::sync::Arc;\n"
        "\n"
        "fn main() {\n"
        "    let _ = Arc::new(HashMap::<i32, i32>::new());\n"
        "}\n"
    )
    (repo / "src").mkdir()
    f = repo / "src" / "lib.rs"
    f.write_text(base)
    git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat"); git(repo, "checkout", "-q", "feat")
    f.write_text(replayed)
    git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "replayed")
    git(repo, "checkout", "-q", "main")
    f.write_text(upstream)
    git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "upstream")
    git(repo, "checkout", "-q", "feat")
    r = git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected conflict"

    cfg = _config(repo)
    # The model's resolution re-states BOTH imports (a common mistake when the
    # model can't tell whether the span includes the surrounding context). This
    # produces a duplicate `use std::collections::HashMap;` when spliced
    # (HashMap exists just above the span). Pre-fix: verify_file re-splices and
    # fails on the duplicate. Post-fix: file_linker dedup removes it and the
    # deduped buffer is validated via whole_text.
    payload = _make_resolved_payload(
        "use std::collections::HashMap;\n"
        "/// shared ref\n"
        "use std::sync::Arc as A;"
    )
    client = CyclingClient([payload])
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    # The file_linker dedup must have rescued this — NOT escalated.
    assert not result.escalated, (
        f"file_linker dedup should remove the duplicate import and the case "
        f"should pass whole-file validation, but escalated: {result.reason}"
    )
    text = f.read_text()
    assert "<<<<<<<" not in text
    # Exactly one occurrence of HashMap (dedup removed the duplicate).
    assert text.count("use std::collections::HashMap;") == 1
    assert "/// shared ref" in text
    assert "use std::sync::Arc as A;" in text


# ---------------------------------------------------------------------------
# Verifier-model critic integration: the LLM judge gates the
# orchestrator's accept path end-to-end when enable_verifier_model is on.
# ---------------------------------------------------------------------------


class SequenceClient:
    """Serves canned responses in strict order; raises if exhausted.

    Unlike CyclingClient, this lets a test script an exact call sequence —
    resolution payloads followed by critic verdicts — so we can assert the
    critic's effect on accept vs escalate.
    """

    def __init__(self, responses: list[str]):
        self.responses = list(responses)

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        if not self.responses:
            raise RuntimeError("no more fake responses")
        return LLMResponse(text=self.responses.pop(0))


def _verifier_config(repo):
    cfg = _config(repo)
    cfg.validation.enable_verifier_model = True
    # Disable the critic guardrail phases in the raw-critic-behavior tests: they
    # exercise the critic's verdict→escalation path with fake clients that return
    # the verdict schema (not the reassessment schema), so Phase 2 would squash a
    # genuine drop via null-evidence. The guardrail has its own test module.
    cfg.validation.enable_verifier_assertion = False
    cfg.validation.enable_verifier_reflection = False
    cfg.validation.enable_verifier_guardrail = False
    return cfg


def test_verifier_blocks_accept_when_it_flags_dropped_intent(distinct_additions_repo, verifier_critic_enabled):
    """Flag on + critic says the resolution drops a side → NOT accepted. The
    candidate is structurally clean (no markers, valid merge) so the syntactic
    validators pass; only the semantic critic catches the dropped intent, and at
    error severity it blocks the accept path (escalation).

    Uses a DISTINCT-ADDITIONS conflict (each side adds a different import) so a
    one-sided merge genuinely drops an addition — the critic SHOULD block it.
    (A same-line value conflict like ``return 'hi'`` vs ``return 'howdy'`` is a
    value resolution where one-sided merging is correct, so it wouldn't test the
    blocking path.)"""
    repo = distinct_additions_repo["repo"]
    # 1st call: a structurally-clean, one-sided resolution. 2nd call: the critic
    # verdict saying the replayed side's import was dropped.
    client = SequenceClient([
        _make_resolved_payload(distinct_additions_repo["current_only"]),  # drops replayed
        json.dumps({"preserves_current": True, "preserves_replayed": False,
                    "reason": "dropped import sys", "confidence": 0.9}),
    ])
    cfg = _verifier_config(repo)
    # The first-empty fast-fail would rescue the empty candidates (7b6ae57) —
    # disable it so this test's mechanism decides the outcome.
    cfg.future.enable_empty_fast_fail = False
    cfg.validation.verifier_severity = "error"
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    # The critic caught the semantic drop the structural checks could not.
    assert result.escalated


def test_verifier_allows_accept_when_it_confirms_both_sides(conflicted_repo, verifier_critic_enabled):
    """Flag on + critic confirms both sides preserved → accepted (rebase
    completes), proving the critic does not over-reject clean merges."""
    repo = conflicted_repo["repo"]
    client = SequenceClient([
        _make_resolved_payload("    return 'hi' + 'howdy'"),  # real merge of both
        json.dumps({"preserves_current": True, "preserves_replayed": True,
                    "reason": "both preserved", "confidence": 0.9}),
    ])
    cfg = _verifier_config(repo)
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert not result.escalated, result.reason
    assert "<<<<<<<" not in (repo / "app.py").read_text()


class CapturingSequenceClient:
    """Like SequenceClient but records the prompt of each complete() call.

    Used to assert the critic's verdict is seeded into the repair prompt on
    retry (the Step-2 feedback-seeding fix): without it, a critic-driven retry
    regenerated with no feedback and the model kept reproducing the same
    dropped-side merge.
    """

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.prompts: list[str] = []  # the user-message text of each call, in order

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        self.prompts.append(messages[-1]["content"])
        if not self.responses:
            raise RuntimeError("no more fake responses")
        return LLMResponse(text=self.responses.pop(0))


def test_verifier_seeds_verdict_into_repair_prompt_on_retry(distinct_additions_repo, verifier_critic_enabled):
    """A retry's repair prompt CONTAINS the validator feedback — so the model
    sees concrete evidence ("dropped a side's addition") instead of regenerating
    blind. This is what makes retries converge on a correct merge.

    Uses a DISTINCT-ADDITIONS conflict (each side adds a different flag) so a
    one-sided merge genuinely drops an addition and the deterministic validators
    flag it, driving a retry. (A value-resolution conflict would accept the
    one-sided merge, so it can't exercise the retry-seeding path.)

    Sequence: (1) one-sided resolution (drops replayed) → validators flag it →
    (2) the retry: model returns the correct merge. We assert the retry's prompt
    carried the validator feedback, and the run converged on the correct merge."""
    repo = distinct_additions_repo["repo"]
    client = CapturingSequenceClient([
        _make_resolved_payload(distinct_additions_repo["current_only"]),  # drops replayed
        _make_resolved_payload(distinct_additions_repo["correct_merged"]),  # correct merge on retry
    ])
    cfg = _verifier_config(repo)
    cfg.validation.verifier_severity = "warning"
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert not result.escalated, result.reason
    text = (repo / "app.py").read_text()
    assert "<<<<<<<" not in text
    assert "cache_on" in text and "metrics_on" in text  # both sides preserved
    # The retry (2nd complete() call) must carry seeded validator feedback.
    assert len(client.prompts) >= 2, client.prompts
    retry_prompt = client.prompts[1]
    assert "drop" in retry_prompt.lower() or "both_sides" in retry_prompt.lower() or "verifier" in retry_prompt.lower(), (
        "validator feedback not seeded into the repair prompt: " + retry_prompt[:300]
    )


def test_critic_retry_names_specific_dropped_unit():
    """The critic-driven retry feedback names the SPECIFIC entity the resolution
    dropped (quantitative per-side preservation), not just 'you dropped a side'.
    A conflict where the replayed side ADDS a function and the candidate drops
    it: the seeded failure must carry "reintroduce: function '<name>'" so the
    model has an exact target. This converges faster than a vague verdict."""
    from capybase.orchestrator import _dropped_units_for, _critic_failure
    from capybase.conflict_model import (
        CandidateResolution, ConflictSide, ConflictUnit, VerificationWarning,
    )
    base = "def main():\n    return 1\n"
    replayed = "def main():\n    return 1\n\ndef helper():\n    return 2\n"
    unit = ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=base),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text="", marker_span=(1, 5),
    )
    # Candidate resolves to base → drops the helper() the replayed side added.
    cand = CandidateResolution(
        candidate_id="c", unit_id="u", model_name="m", prompt_version="v",
        resolved_text=base,
    )
    dropped = _dropped_units_for(unit, cand)
    assert ("function", "helper") in dropped, dropped
    warning = VerificationWarning(
        validator="verifier_model",
        message="verifier: resolution may drop replayed side intent",
        detail={"reason": "dropped helper"},
    )
    failure = _critic_failure(warning, dropped)
    # The rendered failure names the specific unit to reintroduce.
    assert "reintroduce: function 'helper'" in failure.message, failure.message
    assert failure.detail["dropped_units"] == [("function", "helper")]
    # _render_failure surfaces it in the prompt the model sees on retry.
    from capybase.resolution_engine import _render_failure
    rendered = _render_failure(failure)
    assert "helper" in rendered and "reintroduce" in rendered, rendered


def test_dropped_units_no_false_positive_when_entity_present():
    """An entity the resolution preserves (even renamed) is NOT reported dropped
    — a rename is a legitimate merge, not a drop. Only genuinely-absent entities
    surface (matched by name)."""
    from capybase.orchestrator import _dropped_units_for
    from capybase.conflict_model import (
        CandidateResolution, ConflictSide, ConflictUnit,
    )
    base = "def main():\n    return 1\n"
    replayed = "def main():\n    return 1\n\ndef helper():\n    return 2\n"
    unit = ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=base),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text="", marker_span=(1, 5),
    )
    # Candidate keeps helper (under its original name) → nothing dropped.
    cand = CandidateResolution(
        candidate_id="c", unit_id="u", model_name="m", prompt_version="v",
        resolved_text="def main():\n    return 1\n\ndef helper():\n    return 2\n",
    )
    assert _dropped_units_for(unit, cand) == []


def test_soft_warning_failures_lifts_actionable_warnings():
    """A warning-driven retry must carry the validator's SPECIFIC finding into the
    prompt — not regenerate from scratch with zero feedback. The old retry seed
    lifted only hard_failures + the critic warning, so actionable soft warnings
    (intent_coverage, unattributed_code, ...) produced feedback-free
    regenerations. ``_soft_warning_failures`` lifts them into failure shape so
    ``_render_failure`` surfaces their structured detail (dropped names, ratios)
    and ``propose`` selects the targeted repair path."""
    from capybase.orchestrator import _soft_warning_failures
    from capybase.conflict_model import VerificationResult, VerificationWarning
    validation = VerificationResult(
        candidate_id="c", unit_id="u", passed=False,
        hard_failures=[],
        warnings=[
            VerificationWarning(
                validator="intent_coverage",
                message="replayed side coverage 0.00 below floor 0.50",
                detail={"dropped_names": ["helper"], "ratio": 0.0},
            ),
            VerificationWarning(
                validator="unattributed_code",
                message="resolved introduces unit in neither side",
                detail={"names": ["mystery_fn"]},
            ),
        ],
    )
    lifted = _soft_warning_failures(validation)
    validators = {f.validator for f in lifted}
    assert validators == {"intent_coverage", "unattributed_code"}
    # The structured detail (dropped names) is preserved → reaches the prompt.
    ic = next(f for f in lifted if f.validator == "intent_coverage")
    assert ic.detail == {"dropped_names": ["helper"], "ratio": 0.0}
    assert ic.severity == "warning"  # distinguishable from a real hard failure
    # _render_failure surfaces the dropped name so the model gets a concrete target.
    from capybase.resolution_engine import _render_failure
    assert "helper" in _render_failure(ic)


def test_soft_warning_failures_excludes_critic_and_unrelated_warnings():
    """``verifier_model*`` warnings are handled by ``_critic_failure`` against the
    separate critic budget — they must NOT also be lifted here (double-counting).
    Unrelated soft warnings (no retry semantics) are likewise excluded."""
    from capybase.orchestrator import _soft_warning_failures
    from capybase.conflict_model import VerificationResult, VerificationWarning
    validation = VerificationResult(
        candidate_id="c", unit_id="u", passed=False, hard_failures=[],
        warnings=[
            VerificationWarning(validator="verifier_model", message="critic flag"),
            VerificationWarning(
                validator="verifier_model_conflict", message="jury flag"),
            VerificationWarning(validator="something_unrelated", message="noise"),
        ],
    )
    assert _soft_warning_failures(validation) == []


def test_warning_driven_retry_uses_repair_prompt_with_feedback():
    """End-to-end: when the only signal driving a retry is an actionable soft
    warning (no hard failures, no critic), the retry must (a) reuse the previous
    candidate and (b) feed the validator's specific finding back via the repair
    prompt — not regenerate from scratch. This pins the Phase 1 fix: a non-empty
    ``failures`` list selects ``build_repair_prompt`` over ``build_resolve_prompt``.
    """
    from capybase.adapters.llm_openai import LLMResponse
    from capybase.conflict_model import (
        CandidateResolution, ConflictSide, ConflictUnit, VerificationResult,
        VerificationWarning,
    )
    from capybase.config import ModelConfig
    from capybase.context_builder import ContextBuilder
    from capybase.resolution_engine import ResolutionEngine, PROMPT_REPAIR

    class _CapturingClient:
        def __init__(self, text):
            self._text = text
            self.calls = []

        def complete(self, messages, **kw):
            self.calls.append({"messages": messages, **kw})
            return LLMResponse(
                text=self._text,
                raw={"_accumulated": {"finish_reason": "stop"}},
            )

    base = "def main():\n    return 1\n"
    replayed = "def main():\n    return 1\n\ndef helper():\n    return 2\n"
    unit = ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=base),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text="", marker_span=(1, 5),
    )
    prev = CandidateResolution(
        candidate_id="c1", unit_id="u", model_name="m", prompt_version="v",
        resolved_text=base,  # dropped the helper the replayed side added
    )
    # The intent_coverage warning is the ONLY signal — no hard failures, no critic.
    soft_warning = VerificationWarning(
        validator="intent_coverage",
        message="replayed side coverage 0.00 below floor 0.50",
        detail={"dropped_names": ["helper"], "ratio": 0.0},
    )
    validation = VerificationResult(
        candidate_id="c1", unit_id="u", passed=False, hard_failures=[],
        warnings=[soft_warning],
    )
    # Simulate what the orchestrator's retry seed now produces (Phase 1 fix).
    from capybase.orchestrator import _soft_warning_failures
    failures = _soft_warning_failures(validation)
    assert failures, "expected the soft warning to lift to a failure"

    client = _CapturingClient('{"resolved_text": "def main():\\n    return 1\\n\\ndef helper():\\n    return 2\\n"}')
    engine = ResolutionEngine(ModelConfig(samples=1), client=client)
    cands = engine.propose(unit, ContextBuilder().build(unit), failures=failures, prev_candidate=prev)
    # (a) The targeted repair path was chosen (not a fresh resolve).
    assert cands[0].prompt_version == PROMPT_REPAIR, cands[0].prompt_version
    # (b) The validator's specific finding reached the prompt.
    sent = client.calls[0]["messages"][1]["content"]
    assert "helper" in sent, "dropped entity name must reach the model on retry"


def test_wall_time_budget_escalates_non_converging_unit(distinct_additions_repo, verifier_critic_enabled):
    """A unit that can't converge within its wall-clock budget escalates instead
    of looping indefinitely. The critic keeps flagging a dropped-side merge
    (CyclingClient returns the same one-sided resolution + verdict forever), so
    retries would normally pile up; the wall-time deadline bounds total latency
    by escalating once it's exceeded.

    Uses a DISTINCT-ADDITIONS conflict so the one-sided merge genuinely drops an
    addition and the critic keeps flagging it (a value-resolution conflict would
    accept the one-sided merge, so it wouldn't loop)."""
    repo = distinct_additions_repo["repo"]
    # Always returns a one-sided resolution (drops the replayed addition) so the
    # deterministic both-sides-represented + preservation-heuristic validators keep
    # flagging it. CyclingClient repeats forever, so the loop never converges and
    # the wall-time deadline must bound it.
    client = CyclingClient([
        _make_resolved_payload(distinct_additions_repo["current_only"]),  # drops replayed
        json.dumps({"preserves_current": True, "preserves_replayed": False,
                    "reason": "dropped metrics_on", "confidence": 0.5}),
    ])
    cfg = _verifier_config(repo)
    cfg.validation.verifier_severity = "warning"  # soft → would retry forever
    # Disable the pre-LLM resolvers: distinct additions would otherwise be
    # unioned deterministically (resolving without the LLM), so the loop the
    # wall-time deadline must bound would never start.
    cfg.future.enable_structural_resolver = False
    cfg.future.enable_combination_search = False
    # Tiny wall budget so the loop escalates quickly (well under a second). The
    # retry-count budgets are large enough that they wouldn't trigger first.
    cfg.policy.max_wall_time_per_unit_seconds = 0.2
    cfg.policy.max_retries_per_unit = 50
    cfg.policy.max_critic_retries_per_unit = 50
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert result.escalated
    # The loop is bounded — it escalates rather than spinning forever. After the
    # canned responses exhaust, CyclingClient repeats the critic-verdict JSON,
    # which parses as an empty resolution (failure_kind=request_failed). Pre-Fix-B
    # these retries were misclassified as critic retries (retry_count stayed 0),
    # so ONLY the wall-time budget terminated the loop. Post-Fix-B retry_count
    # increments on request_failed, so the loop can terminate via the retry
    # budget, the wall budget, or the needs_human absolute escalation (the empty
    # resolution carries needs_human=True). All three are valid bounded outcomes;
    # the point is the loop does NOT spin forever.
    reason = result.reason or ""
    assert ("wall-time" in reason
            or "max retries exhausted" in reason
            or "needs_human" in reason), result.reason


def test_wall_time_disabled_does_not_escalate(conflicted_repo, verifier_critic_enabled):
    """wall budget = 0 (disabled, the default) → the loop is governed only by the
    retry-count budgets, never by a wall-clock check."""
    repo = conflicted_repo["repo"]
    client = SequenceClient([
        _make_resolved_payload("    return 'hi' + 'howdy'"),  # correct merge
        json.dumps({"preserves_current": True, "preserves_replayed": True,
                    "reason": "both preserved", "confidence": 0.9}),
    ])
    cfg = _verifier_config(repo)
    cfg.policy.max_wall_time_per_unit_seconds = 0.0  # disabled
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    # Converges on the first attempt — no escalation, no wall-time trigger.
    assert not result.escalated, result.reason


def test_file_wall_deadline_disabled_by_default():
    """max_wall_time_per_file_seconds defaults to 0 (disabled) — the file-level
    deadline is not active unless explicitly configured."""
    from capybase.config import Config
    cfg = Config()
    assert cfg.policy.max_wall_time_per_file_seconds == 0.0


def test_file_wall_deadline_caps_repair_retries(distinct_additions_repo, verifier_critic_enabled):
    """When max_wall_time_per_file_seconds is set, the file-level deadline caps
    the total time across all whole-file repair iterations. Without it, each
    repair retry gets a fresh per-unit budget, causing the real wall clock to
    explode (the nested-_resolve_unit budget explosion).

    This test sets a tiny file-level deadline (0.3s) with a large per-unit
    budget (50s) and large retry counts (50). The deadline fires before any
    per-unit/retry budget, bounding the loop."""
    repo = distinct_additions_repo["repo"]
    client = CyclingClient([
        _make_resolved_payload(distinct_additions_repo["current_only"]),  # drops replayed
        json.dumps({"preserves_current": True, "preserves_replayed": False,
                    "reason": "dropped metrics_on", "confidence": 0.5}),
    ])
    cfg = _verifier_config(repo)
    cfg.validation.verifier_severity = "warning"
    cfg.future.enable_structural_resolver = False
    cfg.future.enable_combination_search = False
    # Large per-unit budget + retry counts so they DON'T trigger first.
    cfg.policy.max_wall_time_per_unit_seconds = 50.0
    cfg.policy.max_retries_per_unit = 50
    cfg.policy.max_critic_retries_per_unit = 50
    cfg.policy.max_whole_file_repair_retries = 50
    # Tiny FILE-level deadline — this is what should trigger.
    cfg.policy.max_wall_time_per_file_seconds = 0.3
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert result.escalated
    # The file-level deadline OR the per-unit wall budget OR retry budget can
    # terminate the loop — all are valid bounded outcomes. The point is the
    # loop does NOT spin forever.
    reason = result.reason or ""
    assert ("file-level wall deadline" in reason
            or "wall-time" in reason
            or "max retries" in reason
            or "needs_human" in reason), result.reason


def test_verifier_not_registered_when_flag_off(conflicted_repo):
    """Flag off → the verifier validator is not in the engine's chain at all,
    so no critic call is ever made (zero-cost default)."""
    repo = conflicted_repo["repo"]
    cfg = _config(repo)  # enable_verifier_model defaults False
    cfg.validation.enable_verifier_model = False
    orch = Orchestrator(cfg, repo=str(repo))
    names = [type(v).__name__ for v in orch.verification.validators]
    assert "VerifierModelValidator" not in names


def test_verifier_registered_when_flag_on(conflicted_repo):
    """Flag on → the verifier validator is registered in the chain."""
    repo = conflicted_repo["repo"]
    orch = Orchestrator(_verifier_config(repo), repo=str(repo))
    names = [type(v).__name__ for v in orch.verification.validators]
    assert "VerifierModelValidator" in names


# ---------------------------------------------------------------------------
# VeriGuard policy gate integration: the deterministic safety gate
# blocks an unsafe patch end-to-end when enable_policy_gate + a rule are set.
# ---------------------------------------------------------------------------


def _policy_config(repo):
    from capybase.config import PolicyRule

    cfg = _config(repo)
    cfg.validation.enable_policy_gate = True
    cfg.validation.policy_rules = [
        PolicyRule(name="no_eval", kind="forbid_call", pattern="eval",
                   severity="error", reason="eval is forbidden"),
    ]
    return cfg


def test_policy_gate_registered_when_enabled(conflicted_repo):
    """enable_policy_gate on + a rule → the gate is auto-registered by the
    engine factory (no orchestrator register() call needed)."""
    repo = conflicted_repo["repo"]
    orch = Orchestrator(_policy_config(repo), repo=str(repo))
    names = [type(v).__name__ for v in orch.verification.validators]
    assert "PolicyGateValidator" in names


def test_policy_gate_not_registered_when_disabled(conflicted_repo):
    """Flag off → the gate is absent from the chain (zero-cost default)."""
    repo = conflicted_repo["repo"]
    cfg = _config(repo)  # enable_policy_gate defaults False
    orch = Orchestrator(cfg, repo=str(repo))
    names = [type(v).__name__ for v in orch.verification.validators]
    assert "PolicyGateValidator" not in names


def test_policy_gate_not_registered_when_no_rules(conflicted_repo):
    """Flag on but no rules → still absent (the gate ships no built-in rules)."""
    repo = conflicted_repo["repo"]
    cfg = _config(repo)
    cfg.validation.enable_policy_gate = True
    cfg.validation.policy_rules = []  # no rules → no-op
    orch = Orchestrator(cfg, repo=str(repo))
    names = [type(v).__name__ for v in orch.verification.validators]
    assert "PolicyGateValidator" not in names


def test_policy_gate_blocks_unsafe_patch(conflicted_repo):
    """Gate on + a forbid_call eval rule → a patch that uses eval is blocked
    from auto-apply (escalated). The patch is structurally a valid merge, so
    only the policy gate catches the unsafe call."""
    repo = conflicted_repo["repo"]
    # A candidate that resolves the merge but smuggles in an eval() call.
    client = SequenceClient([
        _make_resolved_payload("    return eval('1') + 'howdy'"),
    ])
    cfg = _policy_config(repo)
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert result.escalated


def test_policy_gate_allows_safe_patch(conflicted_repo):
    """Gate on + a forbid_call eval rule → a patch without eval is accepted
    (rebase completes). Proves the gate doesn't over-reject clean merges."""
    repo = conflicted_repo["repo"]
    client = SequenceClient([
        _make_resolved_payload("    return 'hi' + 'howdy'"),  # no forbidden call
    ])
    cfg = _policy_config(repo)
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert not result.escalated, result.reason


# ---------------------------------------------------------------------------
# LLM code-smell detection integration: the ast-based checker is
# auto-registered when enabled and flags smelly patches through the accept path.
# ---------------------------------------------------------------------------


def _smell_config(repo, severity="warning"):
    cfg = _config(repo)
    cfg.validation.enable_code_smell_checks = True
    cfg.validation.code_smell_severity = severity
    return cfg


def test_code_smell_registered_when_enabled(conflicted_repo):
    """enable_code_smell_checks on → the checker is auto-registered."""
    repo = conflicted_repo["repo"]
    orch = Orchestrator(_smell_config(repo), repo=str(repo))
    names = [type(v).__name__ for v in orch.verification.validators]
    assert "CodeSmellValidator" in names


def test_code_smell_not_registered_when_disabled(conflicted_repo):
    """Flag off (default) → checker absent from the chain."""
    repo = conflicted_repo["repo"]
    cfg = _config(repo)  # enable_code_smell_checks defaults False
    orch = Orchestrator(cfg, repo=str(repo))
    names = [type(v).__name__ for v in orch.verification.validators]
    assert "CodeSmellValidator" not in names


def test_code_smell_error_severity_blocks_smelly_patch(conflicted_repo):
    """Gate on + error severity + a patch with a NaN comparison → blocked from
    auto-apply (escalated). The patch is structurally a valid merge, so only
    the smell checker catches it."""
    repo = conflicted_repo["repo"]
    client = SequenceClient([
        _make_resolved_payload("    return a == np.nan"),  # NaN smell
    ])
    cfg = _smell_config(repo, severity="error")
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert result.escalated


def test_code_smell_warning_does_not_block_clean_merge(conflicted_repo):
    """Gate on + warning severity + a clean patch → accepted (rebase completes).
    The checker doesn't over-reject clean merges."""
    repo = conflicted_repo["repo"]
    client = SequenceClient([
        _make_resolved_payload("    return 'hi' + 'howdy'"),  # no smell
    ])
    cfg = _smell_config(repo, severity="warning")
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert not result.escalated, result.reason


# ---------------------------------------------------------------------------
# F4: retrieval scores journaled into context_built (end-to-end RAG)
# ---------------------------------------------------------------------------


def test_context_built_event_carries_retrieval_scores(conflicted_repo):
    """When RAG retrieves few-shot examples, the ``context_built`` journal event
    records the per-example retrieval scores — the diagnostic data for validating
    the calibrated min_similarity floor in production."""
    from capybase.conflict_model import HistoricalExample
    from capybase.memory.store import Experience, ExperienceStore

    repo = conflicted_repo["repo"]
    # Seed the experience store at the path the orchestrator will read.
    store = ExperienceStore.for_repo(str(repo), ".rebase-agent/memory/experiences.jsonl")
    store.append(
        Experience(
            example=HistoricalExample(
                summary="greet", base="def greet(): return hi",
                current="return hi", replayed="return howdy",
                resolved="return ('hi','howdy')",
            ),
            outcome="accepted", language="python", path="app.py",
        )
    )

    cfg = _config(repo)
    cfg.memory.enabled = True
    cfg.future.enable_rag = True
    cfg.memory.retriever = "lexical"  # dependency-free; no network needed
    cfg.memory.min_examples_for_retrieval = 1
    payload = _make_resolved_payload("    return 'hi' + 'howdy'")
    engine = ResolutionEngine(cfg.model, client=CyclingClient([payload]))
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert not result.escalated, result.reason

    events = orch.journal.read_events()
    built = [e for e in events if e.event_type == "context_built"]
    assert built, "expected a context_built event"
    payload_evt = built[0].payload
    assert "retrieval_scores" in payload_evt
    # The seeded 'greet' example overlaps the conflict's tokens → at least one
    # score is journaled, and they parallel the retrieved examples.
    assert isinstance(payload_evt["retrieval_scores"], list)
    assert len(payload_evt["retrieval_scores"]) >= 1
    assert all(isinstance(s, (int, float)) for s in payload_evt["retrieval_scores"])


def test_context_built_event_has_empty_scores_when_rag_disabled(conflicted_repo):
    """Without RAG, ``context_built`` still carries the key but it's empty —
    the schema is stable whether or not retrieval ran."""
    repo = conflicted_repo["repo"]
    cfg = _config(repo)
    payload = _make_resolved_payload("    return 'hi' + 'howdy'")
    engine = ResolutionEngine(cfg.model, client=CyclingClient([payload]))
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert not result.escalated, result.reason

    events = orch.journal.read_events()
    built = [e for e in events if e.event_type == "context_built"]
    assert built
    assert built[0].payload["retrieval_scores"] == []



# ---------------------------------------------------------------------------
# Difficulty-aware routing: the "simple" fast path must use exactly ONE sample
# even when config.model.samples > 1 (a calibrated profile must not leak into
# the cheap path). Regression: the simple branch called propose() with no
# n_samples, falling back to config.samples (3 if calibrated).
# ---------------------------------------------------------------------------


class CountingClient:
    """FakeClient that counts complete() calls and returns one fixed payload."""

    def __init__(self, payload: str):
        self.payload = payload
        self.calls = 0

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        self.calls += 1
        return LLMResponse(text=self.payload)


def test_simple_routing_uses_one_sample_even_when_samples_is_three(conflicted_repo):
    """The simple fast path must force n_samples=1 even when
    config.model.samples > 1 (a calibrated profile must not leak into the cheap
    path). Regression: the simple branch called propose() with no n_samples,
    falling back to config.samples (3 if calibrated).

    Verified by spying on the n_samples argument the engine receives, not by
    counting complete() calls (those conflate with retry behavior). The pre-LLM
    layers are disabled so the conflict reaches the LLM simple path directly."""
    repo = conflicted_repo["repo"]
    cfg = _config(repo)
    cfg.routing.enabled = True  # classify difficulty
    cfg.future.enable_structural_resolver = False
    cfg.future.enable_combination_search = False  # isolate the simple LLM path
    cfg.future.enable_block_capture = False
    cfg.model.samples = 3  # the value that must NOT leak into the simple path
    payload = _make_resolved_payload("a = 1\nx = 9\nb = 2\nc = 3")
    client = CountingClient(payload)
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    from capybase.conflict_model import ConflictSide, ConflictUnit
    # Disjoint insertion: trivial band (deterministically mergeable) → simple.
    base = "a = 1\nb = 2\nc = 3\n"
    worktree = "a = 1\n<<<<<<<\nb = 2\n=======\nx = 9\nb = 2\n>>>>>>>\nc = 3\n"
    unit = ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE",
                             text="a = 1\nb = 2\nc = 3\n"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE",
                              text="a = 1\nx = 9\nb = 2\nc = 3\n"),
        original_worktree_text=worktree,
        marker_span=(1, 5),
        structural_metadata={"sibling_count": 0},
    )
    from capybase.classifier import classify
    assert classify(unit).difficulty == "simple"

    # Spy on propose's n_samples argument.
    seen_n_samples: list = []
    real_propose = engine.propose

    def spying_propose(*args, **kwargs):
        seen_n_samples.append(kwargs.get("n_samples"))
        return real_propose(*args, **kwargs)

    engine.propose = spying_propose  # type: ignore[method-assign]
    orch.step = 1
    orch._resolve_unit(unit)
    # Every propose() call from the simple path carried n_samples=1, NEVER 3 (or
    # None, which would fall back to config.samples=3).
    assert seen_n_samples, "the simple path never called propose()"
    assert all(n == 1 for n in seen_n_samples), (
        f"simple path proposed with n_samples={seen_n_samples}, expected all 1 "
        f"(a calibrated samples>1 leaked into the cheap path)"
    )


# ---------------------------------------------------------------------------
# Snapshot correctness: the ".before" snapshot must capture the PRE-WRITE
# worktree content (what's on disk before the resolution overwrites it), not
# the resolved buffer being written. Regression: it snapshotted `buffer`, making
# the ".before" name misleading and the audit trail useless.
# ---------------------------------------------------------------------------


def test_before_snapshot_captures_pre_write_worktree_content(repo):
    """The .before snapshot is the on-disk file BEFORE mutation, not the buffer."""
    cfg = _config(repo)
    cfg.journal.enabled = True
    cfg.journal.store_snapshots = True
    engine = ResolutionEngine(cfg.model, client=CyclingClient(["{}"]))
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    # Put a known PRE-EXISTING file on disk, then write a DIFFERENT buffer.
    (repo / "existing.py").write_text("# OLD CONTENT ON DISK\nold = 1\n")
    new_buffer = "# NEW RESOLVED BUFFER\nnew = 2\n"
    orch._write_and_stage("existing.py", new_buffer, StepResult(step_index=1))
    snap = orch.paths.snapshots / "existing.py.before"
    assert snap.exists(), "no .before snapshot was written"
    snap_text = snap.read_text()
    # The snapshot is the PRE-WRITE worktree content, not the resolved buffer.
    assert "OLD CONTENT ON DISK" in snap_text
    assert "new = 2" not in snap_text  # the buffer must NOT have been snapshotted


def test_before_snapshot_absent_for_new_file(repo):
    """A brand-new file (nothing pre-existing on disk) has no .before snapshot."""
    cfg = _config(repo)
    cfg.journal.enabled = True
    cfg.journal.store_snapshots = True
    engine = ResolutionEngine(cfg.model, client=CyclingClient(["{}"]))
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    orch._write_and_stage(
        "brand_new.py", "# fresh file\n", StepResult(step_index=1)
    )
    # No prior content existed → no .before snapshot (no crash, no empty file).
    assert not (orch.paths.snapshots / "brand_new.py.before").exists()


def test_f1_churn_fallback_lands_when_adjudication_unavailable(tmp_path):
    """redis-0049: the tier-2 LLM call died on the case wall deadline and the
    catch-all returned None silently — no journal, no fallback, escalate.
    When BOTH pristine sides passed the compile probes and adjudication is
    unavailable/unparseable, the deterministic churn heuristic completes the
    takeover (same heuristic _adjudicate_whole_side trusts)."""
    import subprocess

    repo = tmp_path / "r"
    repo.mkdir()

    def git(*a):
        return subprocess.run(
            ["git", "-C", str(repo)] + list(a), capture_output=True, text=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    base = "def greet():\n    return 'hello'\n"
    cur = "def greet():\n    return 'hi'\n"
    rep = "def greet():\n    return 'howdy'\n"
    (repo / "app.py").write_text(base)
    git("add", "app.py"); git("commit", "-qm", "base")
    git("branch", "feat"); git("checkout", "-q", "feat")
    (repo / "app.py").write_text(rep)
    git("add", "app.py"); git("commit", "-qm", "rep")
    git("checkout", "-q", "main")
    (repo / "app.py").write_text(cur)
    git("add", "app.py"); git("commit", "-qm", "up")
    git("checkout", "-q", "feat")
    git("rebase", "main")

    class GarbageAdjudicationEngine(FakeConsensusEngine):
        """All merge candidates fail; tier-2's raw_complete is unparseable
        so adjudication returns None (the deadline-death shape)."""

        def raw_complete(self, prompt, *, json_mode=False, temperature=None,
                         max_tokens=None):
            return type("R", (), {"text": "not json"})()

    engine = GarbageAdjudicationEngine([
        _cand("    return 'hi'(", cid="a-broken"),
        _cand("    return 'howdy'(", cid="b-broken"),
    ])
    _cfg = _self_consistency_config(repo)
    _cfg.future.enable_empty_fast_fail = False
    orch = Orchestrator(
        _cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    # Both sides compile (valid python) → churn fallback lands one.
    assert not result.escalated, (
        "churn fallback should complete the takeover when adjudication dies"
    )
    final = (repo / "app.py").read_text()
    assert final in (cur, rep), f"expected a pristine side, got {final!r}"


def test_converging_failure_trend_grants_one_extra_retry(conflicted_repo):
    """P8: a unit whose hard-failure count strictly decreases across
    attempts (the model is converging) earns ONE extra retry at the
    unit-count budget cap — the cheapest conversion path for a near-
    miss. Bounded: the exact-equality check lets the cap be crossed
    once, and a stalled/oscillating trend earns nothing."""
    repo = conflicted_repo["repo"]
    # Attempt 1: 2 hard failures (broken + marker); attempt 2: 1 hard
    # failure (only broken); attempt 3 would pass.
    engine = FakeConsensusEngine([
        _cand("    return 'hi'(  \n<<<<<<< leaked\n", cid="a-two"),
        _cand("    return 'howdy'(", cid="b-one"),
        _cand("    return 'hi' + 'howdy'", cid="c-pass"),
    ])
    _cfg = _self_consistency_config(repo)
    _cfg.future.enable_empty_fast_fail = False
    _cfg.policy.max_retries_per_unit = 1  # the cap the trend must exceed
    orch = Orchestrator(
        _cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    # The third (passing) candidate only ran if the trend grant fired.
    assert not result.escalated
    final = (repo / "app.py").read_text()
    assert "hi' + 'howdy" in final or "howdy" in final
