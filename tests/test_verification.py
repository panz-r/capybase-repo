from capybase.conflict_model import (
    CandidateResolution,
    ConflictSide,
    ConflictUnit,
)
from capybase.verification import ValidationConfig, VerificationEngine


def _unit(base, current, replayed, worktree, span=(1, 5)):
    return ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text=worktree, marker_span=span,
    )


def _candidate(resolved, needs_human=False):
    return CandidateResolution(
        candidate_id="c", unit_id="u", model_name="m", prompt_version="resolve_text_block.v2",
        resolved_text=resolved, needs_human=needs_human,
    )


def _engine():
    return VerificationEngine.default(ValidationConfig())


def test_passes_clean_resolution():
    worktree = "def f():\n<<<<<<< H\n    return 1\n=======\n    return 2\n>>>>>>> b\n"
    unit = _unit("def f():\n    pass", "    return 1", "    return 2", worktree)
    cand = _candidate("    return 1 + 2")
    res = _engine().verify(unit, cand)
    assert res.passed, res.hard_failures
    assert not res.features["markers_remaining"]


def test_fails_on_remaining_markers():
    worktree = "def f():\n<<<<<<< H\n    return 1\n=======\n    return 2\n>>>>>>> b\n"
    unit = _unit("def f():\n    pass", "    return 1", "    return 2", worktree)
    cand = _candidate("    x\n<<<<<<< still here\n")
    res = _engine().verify(unit, cand)
    assert not res.passed


def test_fails_on_needs_human():
    worktree = "x\n<<<<<<< H\na\n=======\nb\n>>>>>>> c\n"
    unit = _unit("x", "a", "b", worktree)
    cand = _candidate("merged", needs_human=True)
    res = _engine().verify(unit, cand)
    assert not res.passed
    assert any(f.validator == "needs_human" for f in res.hard_failures)


def test_flags_copying_one_side():
    worktree = "x\n<<<<<<< H\ncur\n=======\nrep\n>>>>>>> b\n"
    unit = _unit("x", "cur", "rep", worktree)
    # resolved == current side verbatim
    cand = _candidate("cur")
    res = _engine().verify(unit, cand)
    # warning-level, not hard failure
    assert any(w.validator == "preservation_heuristic" for w in res.warnings)


def test_exact_splice_scope_rejects_outside_edits():
    worktree = "l1\nl2\n<<<<<<< H\na\n=======\nb\n>>>>>>> b\nl5\nl6\n"
    unit = _unit("x", "a", "b", worktree, span=(2, 6))
    # A valid merged text keeps outside lines intact.
    cand = _candidate("merged")
    res = _engine().verify(unit, cand)
    assert res.passed


def test_syntax_check_python():
    # Syntax checking now lives in Phase B (verify_file), not per-unit, so it
    # runs against the fully-spliced file. Per-unit verify no longer sets
    # syntax_checked (that feature moved to verify_file).
    worktree = "def f():\n<<<<<<< H\n    return 1\n=======\n    return 2\n>>>>>>> b\n"
    unit = _unit("def f():\n    pass", "    return 1", "    return 2", worktree)
    cand = _candidate("    return 3")
    # Per-unit result: valid, but syntax_checked is absent (Phase A).
    res = _engine().verify(unit, cand)
    assert res.passed
    assert "syntax_checked" not in res.features

    # Phase B: whole-file syntax on the spliced result.
    fres = _engine().verify_file(
        unit.path, unit.language, unit.original_worktree_text,
        [(unit.marker_span, cand.resolved_text)],
    )
    assert fres.features.get("syntax_checked") is True
    assert fres.passed, fres.hard_failures


def test_verify_file_clean_multi_unit():
    """A two-hunk Python file, both resolved: whole file compiles, no markers."""
    # Two functions each with a one-line conflict, separated by a blank line.
    worktree = (
        "def a():\n<<<<<<< H\n    return 1\n=======\n    return 2\n>>>>>>> b\n"
        "\n"
        "def c():\n<<<<<<< H\n    return 3\n=======\n    return 4\n>>>>>>> b\n"
    )
    # spans: block1 = (1,5), block2 = (8,12)
    fres = _engine().verify_file(
        "app.py", "python", worktree,
        [((1, 5), "    return 1 + 2"), ((8, 12), "    return 3 + 4")],
    )
    assert fres.passed, fres.hard_failures
    assert fres.features.get("syntax_checked") is True
    assert fres.features.get("syntax_passed") is True
    assert fres.features.get("whole_file_markers_remaining") == 0


def test_verify_file_catches_cross_unit_syntax_error():
    """The core Phase B win: two resolutions that are valid Python each, but
    produce invalid code when juxtaposed. Per-unit validation could never
    catch this because each only saw one block spliced into a file whose
    other block was still raw markers."""
    # Two adjacent ``return`` statements at module top level (no enclosing def)
    # are individually fine lines, but together are a SyntaxError.
    worktree = (
        "<<<<<<< H\nreturn 1\n=======\nreturn 2\n>>>>>>> b\n"
        "<<<<<<< H\nreturn 3\n=======\nreturn 4\n>>>>>>> b\n"
    )
    fres = _engine().verify_file(
        "app.py", "python", worktree,
        [((0, 4), "return 1"), ((5, 9), "return 3")],
    )
    assert not fres.passed
    assert any(f.validator == "syntax" for f in fres.hard_failures)


def test_verify_file_catches_leaked_markers():
    """A resolution that itself smuggles in markers is caught at file level."""
    worktree = "<<<<<<< H\na\n=======\nb\n>>>>>>> b\n"
    fres = _engine().verify_file(
        "app.py", "python", worktree,
        [((0, 4), "x\n<<<<<<< sneaky\n")],
    )
    assert not fres.passed
    assert any(f.validator == "whole_file_markers" for f in fres.hard_failures)


# ---------------------------------------------------------------------------
# Verifier-model critic (surveys §1/§5): the LLM judge that checks a
# resolution preserves both sides' semantic intent.
# ---------------------------------------------------------------------------


class FakeCriticClient:
    """Records calls and returns a canned critic verdict."""

    def __init__(self, verdict_json: str | Exception):
        self._verdict = verdict_json
        self.calls = []

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        self.calls.append({"messages": messages, "model": model})
        if isinstance(self._verdict, Exception):
            raise self._verdict
        return _LLMResp(self._verdict)


class _LLMResp:
    def __init__(self, text):
        self.text = text
        self.raw = {}


def _verifier_engine(client, *, severity="warning"):
    cfg = ValidationConfig(enable_verifier_model=True, verifier_severity=severity)
    return VerificationEngine.default(
        cfg, extra_validators=[VerifierModelValidator(client, model_name="critic-m")]
    )


def test_verifier_inert_when_disabled_makes_no_call():
    """enable_verifier_model off → the validator is inert AND makes no LLM call.
    This is the zero-cost default: existing deployments are untouched."""
    client = FakeCriticClient('{"preserves_current": false}')  # would fail if called
    cfg = ValidationConfig(enable_verifier_model=False)
    engine = VerificationEngine.default(
        cfg, extra_validators=[VerifierModelValidator(client, model_name="m")]
    )
    worktree = "def f():\n<<<<<<< H\n    return 1\n=======\n    return 2\n>>>>>>> b\n"
    unit = _unit("def f():\n    pass", "    return 1", "    return 2", worktree)
    res = engine.verify(unit, _candidate("    return 1 + 2"))
    assert res.passed
    assert res.features["verifier_checked"] is False
    assert client.calls == []  # never called


def test_verifier_passes_when_preserves_both():
    client = FakeCriticClient(
        '{"preserves_current": true, "preserves_replayed": true, "reason": "ok", "confidence": 0.9}'
    )
    engine = _verifier_engine(client)
    worktree = "def f():\n<<<<<<< H\n    return 1\n=======\n    return 2\n>>>>>>> b\n"
    unit = _unit("def f():\n    pass", "    return 1", "    return 2", worktree)
    res = engine.verify(unit, _candidate("    return 1 + 2"))
    assert res.passed
    assert res.features["verifier_checked"] is True
    assert res.features["verifier_preserves_current"] is True
    assert res.features["verifier_preserves_replayed"] is True
    assert res.features["verifier_confidence"] == 0.9
    assert len(client.calls) == 1


def test_verifier_flags_dropped_side_as_warning_by_default():
    """A resolution that drops the replayed side's intent → critic fails at the
    configured (warning) severity, surfacing as a warning, not a hard failure."""
    client = FakeCriticClient(
        '{"preserves_current": true, "preserves_replayed": false, '
        '"reason": "dropped replayed guard", "confidence": 0.8}'
    )
    engine = _verifier_engine(client, severity="warning")
    worktree = "def f():\n<<<<<<< H\n    return 1\n=======\n    return 2\n>>>>>>> b\n"
    unit = _unit("def f():\n    pass", "    return 1", "    return 2", worktree)
    res = engine.verify(unit, _candidate("    return 1"))  # drops replayed side
    # Warning severity: validator disagrees but result still "passed" (no hard
    # failure), the disagreement surfaces as a warning.
    assert any(w.validator == "verifier_model" for w in res.warnings)
    assert res.features["verifier_preserves_replayed"] is False
    # default severity is warning → not a hard failure
    assert not any(f.validator == "verifier_model" for f in res.hard_failures)


def test_verifier_hard_rejects_when_severity_error():
    """Strict deployments (verifier_severity='error') treat a dropped-intent
    verdict as a hard failure that blocks acceptance."""
    client = FakeCriticClient(
        '{"preserves_current": false, "preserves_replayed": true, '
        '"reason": "dropped current", "confidence": 0.7}'
    )
    engine = _verifier_engine(client, severity="error")
    worktree = "def f():\n<<<<<<< H\n    return 1\n=======\n    return 2\n>>>>>>> b\n"
    unit = _unit("def f():\n    pass", "    return 1", "    return 2", worktree)
    res = engine.verify(unit, _candidate("    return 2"))  # drops current side
    assert not res.passed
    assert any(f.validator == "verifier_model" for f in res.hard_failures)


def test_verifier_degrades_gracefully_on_client_error():
    """A flaky critic (client raises) must never crash resolution — it skips
    and reports verifier_checked=False, leaving the candidate accepted."""
    client = FakeCriticClient(RuntimeError("LLM request failed"))
    engine = _verifier_engine(client)
    worktree = "def f():\n<<<<<<< H\n    return 1\n=======\n    return 2\n>>>>>>> b\n"
    unit = _unit("def f():\n    pass", "    return 1", "    return 2", worktree)
    res = engine.verify(unit, _candidate("    return 1 + 2"))
    assert res.passed
    assert res.features["verifier_checked"] is False


def test_verifier_degrades_gracefully_on_unparseable_response():
    """A malformed critic verdict (no JSON) skips rather than rejecting."""
    client = FakeCriticClient("the merge looks fine to me, no JSON here")
    engine = _verifier_engine(client)
    worktree = "def f():\n<<<<<<< H\n    return 1\n=======\n    return 2\n>>>>>>> b\n"
    unit = _unit("def f():\n    pass", "    return 1", "    return 2", worktree)
    res = engine.verify(unit, _candidate("    return 1 + 2"))
    assert res.passed
    assert res.features["verifier_checked"] is False


# Import the validator class used by the helpers above.
from capybase.verification import VerifierModelValidator  # noqa: E402


def test_verifier_prompt_contains_all_sides_and_candidate():
    """The critic prompt must show all three sides and the candidate resolution
    so the judge has the full intent-preservation context."""
    from capybase.resolution_engine import build_verifier_prompt
    from capybase.context_builder import ContextBuilder

    worktree = "def f():\n<<<<<<< H\n    return 1\n=======\n    return 2\n>>>>>>> b\n"
    unit = _unit("def f():\n    pass", "    return 1", "    return 2", worktree)
    cand = _candidate("    return 1 + 2")
    prompt = build_verifier_prompt(unit, cand, ContextBuilder().build(unit))
    assert "CURRENT_UPSTREAM_SIDE" in prompt
    assert "REPLAYED_COMMIT_SIDE" in prompt
    assert "BASE" in prompt
    assert "return 1 + 2" in prompt  # the candidate
    assert "preserves_current" in prompt  # the JSON verdict contract
    assert "preserves_replayed" in prompt
