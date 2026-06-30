"""Tests for block-capture resolution (large modify/delete conflicts).

When one side deleted a large block and the keeper kept/modified it, the model
can't reliably reproduce the block as an escaped JSON string — it collapses to
placeholders and corrupts the escaping. Block-capture sidesteps this: the model
makes a keep/accept_deletion/needs_human DECISION and capybase splices the chosen
conflict side verbatim. These exercise the prompt builder + parser (pure) and the
orchestrator's _try_block_capture with a fake client.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from capybase.conflict_model import ConflictSide, ConflictUnit, ContextBundle
from capybase.config import Config
from capybase.resolution_engine import (
    PROMPT_BLOCK_CAPTURE,
    build_block_capture_prompt,
    parse_block_capture_decision,
)


# ---------------------------------------------------------------------------
# A large modify/delete unit (the edit_file.rs shape: upstream deleted a block;
# replayed kept it with a migration, so the structural delete_side rule declines).
# ---------------------------------------------------------------------------


def _large_modify_delete_unit(*, keeper_lines: int = 60) -> ConflictUnit:
    """A modify/delete with a large 'modified' keeper (delete_side declines)."""
    base = "\n".join(f"    #[test]\n    fn test_{i}() {{\n        assert!(true);\n    }}\n" for i in range(keeper_lines))
    # Keeper: same block but with a migration (count_brace_depth → count_brace_stats),
    # so it's 'modified' not 'unchanged' — delete_side declines, block-capture engages.
    keeper = base.replace("assert!(true)", "assert_eq!(stats(), 0)")
    return ConflictUnit(
        session_id="s", step_index=1, path="edit_file.rs", language="rust",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=""),  # deleted
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=keeper),  # kept+modified
        original_worktree_text="",
        marker_span=(0, 0),
        structural_metadata={
            "merge_direction": {
                "kind": "modify_delete",
                "current": "deleted",
                "replayed": "modified",
                "summary": "modify/delete: CURRENT_UPSTREAM_SIDE DELETED this block; "
                           "REPLAYED_COMMIT_SIDE kept/changed it",
                "deleting_side": "current",
            }
        },
    )


# ---------------------------------------------------------------------------
# Prompt builder + parser (pure)
# ---------------------------------------------------------------------------


def test_prompt_is_decision_not_reproduction():
    """The block-capture prompt must NOT ask the model to reproduce the block."""
    u = _large_modify_delete_unit()
    prompt = build_block_capture_prompt(u, ContextBundle(primary_text="", token_estimate=0))
    # It asks for a decision, not the resolved text.
    assert "DECISION" in prompt
    assert "accept_deletion" in prompt
    assert "keep_block" in prompt
    # It does NOT carry the full keeper text (only a summary) — so the model
    # can't be asked to reproduce it. Check the elision marker is present.
    assert "lines elided" in prompt


def test_prompt_shows_the_disambiguation():
    u = _large_modify_delete_unit()
    prompt = build_block_capture_prompt(u, ContextBundle(primary_text="", token_estimate=0))
    assert "CURRENT_UPSTREAM_SIDE DELETED this block" in prompt


def test_parse_decision_accept_deletion():
    raw = '```json\n{"decision": "accept_deletion", "reason": "dead tests"}\n```'
    assert parse_block_capture_decision(raw) == ("accept_deletion", "dead tests")


def test_parse_decision_keep_block():
    raw = '{"decision": "keep_block", "reason": "still used"}'
    assert parse_block_capture_decision(raw) == ("keep_block", "still used")


def test_parse_decision_needs_human_explicit():
    raw = '{"decision": "needs_human", "reason": "ambiguous"}'
    assert parse_block_capture_decision(raw) == ("needs_human", "ambiguous")


def test_parse_decision_garbage_defaults_to_needs_human():
    """Any unparseable/unknown response safely escalates (never guesses)."""
    assert parse_block_capture_decision("totally not json") == ("needs_human", "")
    assert parse_block_capture_decision('{"decision": "bogus"}') == ("needs_human", "")


# ---------------------------------------------------------------------------
# Signature extraction + deleting-commit context (richer summary)
# ---------------------------------------------------------------------------


def test_extract_signatures_finds_rust_tests_and_fns():
    from capybase.resolution_engine import _extract_signatures

    block = (
        "#[test]\n    fn brace_balance_passes() {\n        assert!(true);\n    }\n"
        "    #[test]\n    fn brace_balance_fails() {\n        assert!(false);\n    }\n"
        "    fn helper() {}\n"
        "    pub struct Config;\n"
    )
    sigs = _extract_signatures(block)
    assert "test: brace_balance_passes" in sigs
    assert "test: brace_balance_fails" in sigs
    assert "fn: helper" in sigs
    assert "struct: Config" in sigs
    # A test fn is labeled "test:" not "fn:" (precedence).
    assert not any(s == "fn: brace_balance_passes" for s in sigs)


def test_extract_signatures_finds_python_defs():
    from capybase.resolution_engine import _extract_signatures

    sigs = _extract_signatures("def foo():\n    pass\n\nclass Bar:\n    pass\n")
    assert "def: foo" in sigs
    assert "class: Bar" in sigs


def test_prompt_includes_deleting_commit_subject():
    """The deleting commit's subject (why the block was removed) is shown so the
    model can judge: 'consolidate(tests)' = deliberate cleanup; 'remove dead fn'
    = dead code."""
    u = _large_modify_delete_unit()
    u.structural_metadata["provenance"] = {
        "current": {"sha": "x", "subject": "consolidate(tests): remove verbose tests"},
    }
    prompt = build_block_capture_prompt(u, ContextBundle(primary_text="", token_estimate=0))
    assert "consolidate(tests): remove verbose tests" in prompt
    assert "DELETING commit" in prompt


def test_prompt_includes_entity_signatures():
    """The test/fn names in the kept block are surfaced — the decision signal."""
    u = _large_modify_delete_unit()
    prompt = build_block_capture_prompt(u, ContextBundle(primary_text="", token_estimate=0))
    assert "Entities in the KEPT block" in prompt


def test_prompt_without_provenance_omits_commit_section():
    """No provenance metadata → no DELETING commit section (graceful)."""
    u = _large_modify_delete_unit()
    u.structural_metadata.pop("provenance", None)
    prompt = build_block_capture_prompt(u, ContextBundle(primary_text="", token_estimate=0))
    assert "DELETING commit" not in prompt


# ---------------------------------------------------------------------------
# Orchestrator _try_block_capture (with a fake client)
# ---------------------------------------------------------------------------


class _FakeClient:
    """Returns a scripted decision response."""

    def __init__(self, decision: str, reason: str = "x"):
        self._text = json.dumps({"decision": decision, "reason": reason})

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        from capybase.adapters.llm_openai import LLMResponse
        return LLMResponse(text=self._text)


def _engine_with(client):
    from capybase.resolution_engine import ResolutionEngine
    cfg = Config().model
    return ResolutionEngine(cfg, client=client)


def _orch(client, repo: Path):
    from capybase.orchestrator import Orchestrator
    cfg = Config()
    cfg.tests.required = False
    return Orchestrator(
        cfg, repo=str(repo),
        resolution_engine=_engine_with(client),
        out=lambda *_a, **_k: None,
    )


def test_block_capture_accept_deletion_splices_empty(repo: Path):
    """accept_deletion → splice the deleting (empty) side; accepted."""
    orch = _orch(_FakeClient("accept_deletion"), repo)
    u = _large_modify_delete_unit()
    outcome = orch._try_block_capture(u)
    assert outcome is not None
    assert outcome.accepted is not None
    # Deleting side was empty → resolved text is empty (the deletion stands).
    assert outcome.accepted.resolved_text == ""
    assert outcome.accepted.prompt_version == PROMPT_BLOCK_CAPTURE


def test_block_capture_keep_block_splices_keeper_verbatim(repo: Path):
    """keep_block → splice the keeper side VERBATIM (never reproduced by model)."""
    orch = _orch(_FakeClient("keep_block"), repo)
    u = _large_modify_delete_unit()
    outcome = orch._try_block_capture(u)
    assert outcome is not None and outcome.accepted is not None
    # The spliced text is EXACTLY the keeper's text — not a model reproduction.
    assert outcome.accepted.resolved_text == u.replayed.text


def test_block_capture_needs_human_declines(repo: Path):
    """needs_human → block-capture returns None (falls through to LLM/escalation)."""
    orch = _orch(_FakeClient("needs_human"), repo)
    u = _large_modify_delete_unit()
    assert orch._try_block_capture(u) is None


def test_block_capture_declines_when_block_too_small(repo: Path):
    """Small modify/deletes go to the normal LLM path; block-capture doesn't fire."""
    orch = _orch(_FakeClient("accept_deletion"), repo)
    u = _large_modify_delete_unit(keeper_lines=10)  # < block_capture_min_lines (50)
    assert orch._try_block_capture(u) is None


def test_block_capture_declines_when_not_modify_delete(repo: Path):
    """Non-modify/delete conflicts never engage block-capture."""
    orch = _orch(_FakeClient("accept_deletion"), repo)
    # A both_modify unit (no merge_direction metadata).
    u = ConflictUnit(
        session_id="s", step_index=1, path="a.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="x"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="y"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="z"),
        original_worktree_text="",
    )
    assert orch._try_block_capture(u) is None


def test_block_capture_disabled_when_feature_off(repo: Path):
    """[future] enable_block_capture = false → never engages."""
    from capybase.orchestrator import Orchestrator
    cfg = Config()
    cfg.tests.required = False
    cfg.future.enable_block_capture = False
    orch = Orchestrator(
        cfg, repo=str(repo),
        resolution_engine=_engine_with(_FakeClient("accept_deletion")),
        out=lambda *_a, **_k: None,
    )
    u = _large_modify_delete_unit()
    assert orch._try_block_capture(u) is None
