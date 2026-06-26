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


# ---------------------------------------------------------------------------
# VeriGuard-style deterministic policy gate (survey §4): the only check that
# inspects WHAT a patch introduces (imports/calls), not just its structure.
# ---------------------------------------------------------------------------


def _rule(name, kind, pattern, severity="error", reason=""):
    from capybase.config import PolicyRule

    return PolicyRule(name=name, kind=kind, pattern=pattern, severity=severity, reason=reason)


def _gate_engine(rules):
    """A VerificationEngine with the policy gate enabled and the given rules."""
    cfg = ValidationConfig(enable_policy_gate=True, policy_rules=tuple(rules))
    return VerificationEngine.default(cfg)


def _py_unit():
    return _unit(
        "def f():\n    pass", "    return 1", "    return 2",
        "def f():\n<<<<<<< H\n    return 1\n=======\n    return 2\n>>>>>>> b\n",
    )


def test_extract_policy_facts_imports_and_calls():
    """The ast extractor collects plain/dotted imports and plain/dotted calls."""
    from capybase.verification import _extract_policy_facts

    facts = _extract_policy_facts(
        "import os\n"
        "from subprocess import run, Popen\n"
        "import urllib.request\n"
        "\n"
        "def f():\n"
        "    eval('1')\n"
        "    os.system('ls')\n"
        "    subprocess.run(['x'])\n",
        "python",
    )
    assert "os" in facts.imports
    assert "subprocess" in facts.imports          # from-import module
    assert "urllib.request" in facts.imports       # dotted import
    assert "eval" in facts.calls
    assert "os.system" in facts.calls
    assert "subprocess.run" in facts.calls


def test_extract_policy_facts_empty_for_unparseable():
    """Unparseable text yields empty facts (no crash); the syntax validator
    catches the syntax error separately."""
    from capybase.verification import _extract_policy_facts

    facts = _extract_policy_facts("def f(:\n  broken", "python")
    assert facts.imports == set()
    assert facts.calls == set()


def test_extract_policy_facts_handles_splice_fragment():
    """The resolved_text is a splice FRAGMENT, not a whole module — it may
    contain a bare return or leading-indent that isn't valid at module scope.
    The extractor must still find the calls/imports inside it (wrapping in a
    dummy function body). This is the real production path: candidates carry
    fragments, and without this the gate would see every fragment as empty."""
    from capybase.verification import _extract_policy_facts

    # Bare return with an eval call — invalid as a module, valid as a fragment.
    facts = _extract_policy_facts("    return eval('1') + 'howdy'", "python")
    assert "eval" in facts.calls
    # Import inside a fragment still detected.
    facts2 = _extract_policy_facts("    import subprocess\n    x = 1", "python")
    assert "subprocess" in facts2.imports


def test_extract_policy_facts_empty_for_non_python():
    """The gate is Python-only; other languages yield empty facts (no-op)."""
    from capybase.verification import _extract_policy_facts

    facts = _extract_policy_facts("fn main() { let x = 1; }", "rust")
    assert facts.imports == set()
    assert facts.calls == set()


def test_policy_gate_inert_when_disabled():
    """enable_policy_gate off → the gate is a no-op: passed, policy_checked
    False, and risk_tags untouched. This is the zero-cost default."""
    cfg = ValidationConfig(enable_policy_gate=False)
    engine = VerificationEngine.default(
        cfg,
        extra_validators=[PolicyGateValidator()],
    )
    unit = _py_unit()
    res = engine.verify(unit, _candidate("import subprocess\nx = 1"))
    assert res.passed
    assert res.features["policy_checked"] is False
    assert unit.risk_tags == []  # not tagged


def test_policy_gate_inert_when_no_rules():
    """Enabled but no rules → still a no-op (the code ships no rules)."""
    cfg = ValidationConfig(enable_policy_gate=True, policy_rules=())
    engine = VerificationEngine.default(
        cfg,
        extra_validators=[PolicyGateValidator()],
    )
    res = engine.verify(_py_unit(), _candidate("import subprocess\nx = 1"))
    assert res.passed
    assert res.features["policy_checked"] is False


def test_policy_gate_flags_forbidden_import():
    """A patch that imports a forbidden module is flagged + tagged."""
    engine = _gate_engine([_rule("no_subprocess", "forbid_import", "subprocess")])
    unit = _py_unit()
    res = engine.verify(unit, _candidate("import subprocess\nsubprocess.run(['x'])\n"))
    assert not res.passed
    assert res.features["policy_checked"] is True
    assert res.features["policy_violation_count"] == 1
    assert res.features["policy_no_subprocess_violated"] is True
    assert "policy:no_subprocess" in unit.risk_tags


def test_policy_gate_prefix_matches_dotted_call():
    """A forbid_call rule with pattern "subprocess" catches subprocess.run —
    prefix matching is what makes a single rule cover a whole module's calls."""
    engine = _gate_engine([_rule("no_subprocess_call", "forbid_call", "subprocess")])
    res = engine.verify(_py_unit(), _candidate("import subprocess\nsubprocess.run(['x'])\n"))
    assert not res.passed
    assert res.features["policy_no_subprocess_call_violated"] is True


def test_policy_gate_flags_builtin_eval():
    engine = _gate_engine([_rule("no_eval", "forbid_call", "eval")])
    unit = _py_unit()
    res = engine.verify(unit, _candidate("x = eval('1+1')\n"))
    assert not res.passed
    assert "policy:no_eval" in unit.risk_tags


def test_policy_gate_clean_patch_passes():
    """A patch with no forbidden imports/calls passes; risk_tags stay empty."""
    engine = _gate_engine([
        _rule("no_subprocess", "forbid_import", "subprocess"),
        _rule("no_eval", "forbid_call", "eval"),
    ])
    unit = _py_unit()
    res = engine.verify(
        unit, _candidate("import json\nx = json.dumps({'a': 1})\n")
    )
    assert res.passed
    assert res.features["policy_checked"] is True
    assert res.features["policy_violation_count"] == 0
    assert unit.risk_tags == []


def test_policy_gate_warning_does_not_hard_fail():
    """A warning-severity rule flags the patch but does not hard-fail it (the
    result stays passed; it surfaces as a warning for retry/escalate bias)."""
    engine = _gate_engine([_rule("warn_subprocess", "forbid_import", "subprocess", severity="warning")])
    res = engine.verify(_py_unit(), _candidate("import subprocess\nx = 1\n"))
    assert res.passed  # warning → not a hard failure
    assert res.features["policy_violation_count"] == 1


def test_policy_gate_error_dominates_warning():
    """When one rule is error and another is warning, the max severity wins and
    the result hard-fails."""
    engine = _gate_engine([
        _rule("warn_eval", "forbid_call", "eval", severity="warning"),
        _rule("err_subprocess", "forbid_import", "subprocess", severity="error"),
    ])
    res = engine.verify(
        _py_unit(), _candidate("import subprocess\nx = eval('1')\n")
    )
    assert not res.passed  # error-severity violation present
    assert res.features["policy_violation_count"] == 2


# Import the validator used by the helpers above.
from capybase.verification import PolicyGateValidator  # noqa: E402


# ---------------------------------------------------------------------------
# LLM code-smell detection (survey §7): a deterministic ast-based pre-test
# quality filter for smells common in LLM-generated code.
# ---------------------------------------------------------------------------


def _smell_engine(severity="warning"):
    cfg = ValidationConfig(enable_code_smell_checks=True, code_smell_severity=severity)
    return VerificationEngine.default(cfg)


def _py_unit():
    return _unit(
        "def f():\n    pass", "    return 1", "    return 2",
        "def f():\n<<<<<<< H\n    return 1\n=======\n    return 2\n>>>>>>> b\n",
    )


def test_smell_detect_nan_comparison():
    from capybase.verification import _detect_code_smells

    findings = _detect_code_smells("import numpy as np\nx = (a == np.nan)\n", "python")
    names = [f.name for f in findings]
    assert "nan_comparison" in names


def test_smell_detect_nan_comparison_inequality():
    from capybase.verification import _detect_code_smells

    findings = _detect_code_smells("import numpy as np\nflag = a != np.nan\n", "python")
    assert any(f.name == "nan_comparison" for f in findings)


def test_smell_detect_chain_indexing():
    from capybase.verification import _detect_code_smells

    findings = _detect_code_smells("x = df['a']['b']\n", "python")
    assert any(f.name == "chain_indexing" for f in findings)


def test_smell_detect_unseeded_randomness():
    from capybase.verification import _detect_code_smells

    findings = _detect_code_smells("import random\nx = random.random()\n", "python")
    assert any(f.name == "unseeded_randomness" for f in findings)


def test_smell_clean_code_no_findings():
    """Correct idioms are NOT flagged: np.isnan, .loc indexing, seeded random."""
    from capybase.verification import _detect_code_smells

    findings = _detect_code_smells(
        "import numpy as np\nimport random\n"
        "random.seed(42)\n"
        "flag = np.isnan(a)\n"   # correct idiom, not == np.nan
        "x = df.loc['a', 'b']\n",  # .loc, not chained [][]
        "python",
    )
    assert findings == []


def test_smell_seeded_random_not_flagged():
    """random calls WITH a seed present → no unseeded_randomness finding."""
    from capybase.verification import _detect_code_smells

    findings = _detect_code_smells(
        "import random\nrandom.seed(0)\nx = random.randint(1, 10)\n", "python"
    )
    assert not any(f.name == "unseeded_randomness" for f in findings)


def test_smell_empty_for_non_python():
    from capybase.verification import _detect_code_smells

    assert _detect_code_smells("let x = a == np.nan;", "rust") == []


def test_smell_empty_for_unparseable():
    from capybase.verification import _detect_code_smells

    assert _detect_code_smells("def f(:\n  broken", "python") == []


def test_smell_validator_inert_when_disabled():
    """enable_code_smell_checks off → inert: passed, smell_checked False,
    risk_tags untouched. The zero-cost default."""
    cfg = ValidationConfig(enable_code_smell_checks=False)
    engine = VerificationEngine.default(
        cfg, extra_validators=[CodeSmellValidator()]
    )
    unit = _py_unit()
    res = engine.verify(unit, _candidate("import numpy as np\nx = a == np.nan\n"))
    assert res.passed
    assert res.features["smell_checked"] is False
    assert unit.risk_tags == []


def test_smell_validator_flags_nan_comparison():
    """Gate on + a candidate with == np.nan → flagged + tagged."""
    engine = _smell_engine()
    unit = _py_unit()
    res = engine.verify(unit, _candidate("import numpy as np\nx = a == np.nan\n"))
    # Default severity is warning → not a hard failure, but flagged.
    assert res.features["smell_checked"] is True
    assert res.features["smell_count"] == 1
    assert res.features["smell_nan_comparison"] is True
    assert "smell:nan_comparison" in unit.risk_tags


def test_smell_validator_clean_patch_passes():
    """A clean candidate → passed, smell_checked True, empty tags."""
    engine = _smell_engine()
    unit = _py_unit()
    res = engine.verify(unit, _candidate("x = a + b\n"))
    assert res.passed
    assert res.features["smell_checked"] is True
    assert res.features["smell_count"] == 0
    assert unit.risk_tags == []


def test_smell_validator_error_severity_hard_fails():
    """code_smell_severity='error' → a smelly candidate is hard-blocked."""
    engine = _smell_engine(severity="error")
    res = engine.verify(_py_unit(), _candidate("x = df['a']['b']\n"))
    assert not res.passed
    assert res.features["smell_chain_indexing"] is True


def test_smell_validator_handles_fragment():
    """A bare-return splice fragment with a smell inside is still detected —
    reuses the policy gate's fragment-tolerant parser."""
    engine = _smell_engine()
    unit = _py_unit()
    res = engine.verify(unit, _candidate("    return a == np.nan\n"))
    assert res.features["smell_nan_comparison"] is True


from capybase.verification import CodeSmellValidator  # noqa: E402
