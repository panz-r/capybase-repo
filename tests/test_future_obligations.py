"""Tests for future obligations (#9 step 3).

Future obligations are derived structurally from future source-commit patches
(never from the LLM): symbol survival, imports, and config-key edits a later
commit depends on. The validation rejects a candidate that locally satisfies
both sides but drops a symbol a later commit still needs.
"""

from __future__ import annotations

from capybase.future_obligations import (
    FutureObligation,
    extract_future_obligations,
    obligations_satisfied,
)
from capybase.history import ReplayCommit


def _commit(oid, subject="later"):
    return ReplayCommit(
        oid=oid, parent_oid="p", subject=subject, body_summary="",
        touched_files=["cfg.py"], diffstat={}, patch_id="", index=1,
    )


def _patch(body: str) -> bytes:
    """Wrap added lines as a minimal unified diff (only ``+`` lines matter)."""
    lines = body.split("\n")
    added = "\n".join("+" + l for l in lines)
    return f"--- a/cfg.py\n+++ b/cfg.py\n@@ -1,1 +1,{len(lines)} @@\n{added}\n".encode()


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------


def test_no_future_commits_yields_no_obligations():
    obls = extract_future_obligations(
        resolved_text="def foo():\n    pass\n",
        future_commits=[], patches={},
    )
    assert obls.empty


def test_symbol_survival_derived_when_future_commit_references_defined_symbol():
    """A future commit calling parse_config → survival obligation."""
    resolved = "def parse_config():\n    return {}\n"
    fut = _commit("f1", "use config")
    # The future commit references parse_config (a call) but doesn't define it.
    patch = _patch("x = parse_config()\n")
    obls = extract_future_obligations(
        resolved_text=resolved, future_commits=[fut], patches={"f1": patch},
    )
    kinds = {(o.kind, o.symbol) for o in obls.obligations}
    assert ("symbol_survival", "parse_config") in kinds
    assert "parse_config" in obls.required_symbols


def test_import_obligation_derived_for_python_import():
    """A future commit importing a name the region defines → import obligation."""
    resolved = "def normalize_path(p):\n    return p\n"
    fut = _commit("f1", "add importer")
    patch = _patch("from utils import normalize_path\n")
    obls = extract_future_obligations(
        resolved_text=resolved, future_commits=[fut], patches={"f1": patch},
    )
    kinds = {(o.kind, o.symbol) for o in obls.obligations}
    assert ("import", "normalize_path") in kinds
    assert "normalize_path" in obls.required_symbols


def test_self_definition_does_not_create_survival_obligation():
    """A future commit that DEFINES its own helper doesn't depend on us for it."""
    resolved = "def helper():\n    pass\n"
    fut = _commit("f1", "add own helper")
    # The future commit defines its OWN helper with the same name — no survival
    # obligation, because it doesn't depend on our resolution for it.
    patch = _patch("def helper():\n    return 1\n")
    obls = extract_future_obligations(
        resolved_text=resolved, future_commits=[fut], patches={"f1": patch},
    )
    assert ("symbol_survival", "helper") not in {
        (o.kind, o.symbol) for o in obls.obligations
    }


def test_key_edit_obligation_is_advisory_not_required():
    """A future commit editing a config key → advisory key_edit (not required)."""
    resolved = "strict_mode = false\n"
    fut = _commit("f1", "toggle strict")
    patch = _patch("strict_mode = true\n")
    obls = extract_future_obligations(
        resolved_text=resolved, future_commits=[fut], patches={"f1": patch},
    )
    key_edits = [o for o in obls.obligations if o.kind == "key_edit"]
    assert any(o.key == "strict_mode" for o in key_edits)
    # Key edits are advisory — not in required_symbols.
    assert "strict_mode" not in obls.required_symbols
    assert "strict_mode" in obls.expected_keys


def test_obligations_deduped_across_commits():
    """Two future commits referencing the same symbol → one survival obligation."""
    resolved = "def parse():\n    pass\n"
    f1 = _commit("f1", "use 1")
    f2 = _commit("f2", "use 2")
    patches = {
        "f1": _patch("a = parse()\n"),
        "f2": _patch("b = parse()\n"),
    }
    obls = extract_future_obligations(
        resolved_text=resolved, future_commits=[f1, f2], patches=patches,
    )
    survivals = [o for o in obls.obligations if o.kind == "symbol_survival" and o.symbol == "parse"]
    assert len(survivals) == 1


def test_render_block_lists_obligations():
    resolved = "def parse_config():\n    pass\n"
    fut = _commit("f1", "use config")
    obls = extract_future_obligations(
        resolved_text=resolved, future_commits=[fut],
        patches={"f1": _patch("x = parse_config()\n")},
    )
    block = obls.render_block()
    assert "Future obligations" in block
    assert "parse_config" in block
    assert "use config" in block  # the commit subject appears


def test_render_block_empty_when_no_obligations():
    obls = extract_future_obligations(
        resolved_text="def foo():\n    pass\n", future_commits=[], patches={},
    )
    assert obls.render_block() == ""


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def test_candidate_keeping_symbol_passes():
    resolved = "def parse_config():\n    return {}\n"
    fut = _commit("f1", "use config")
    obls = extract_future_obligations(
        resolved_text=resolved, future_commits=[fut],
        patches={"f1": _patch("x = parse_config()\n")},
    )
    # Candidate still defines parse_config.
    candidate = "def parse_config():\n    return {'a': 1}\n"
    ok, dropped = obligations_satisfied(obls, candidate)
    assert ok
    assert dropped == []


def test_candidate_dropping_symbol_fails():
    """A candidate that deletes parse_config violates the survival obligation."""
    resolved = "def parse_config():\n    return {}\n"
    fut = _commit("f1", "use config")
    obls = extract_future_obligations(
        resolved_text=resolved, future_commits=[fut],
        patches={"f1": _patch("x = parse_config()\n")},
    )
    # Candidate dropped parse_config entirely.
    candidate = "# config removed\npass\n"
    ok, dropped = obligations_satisfied(obls, candidate)
    assert not ok
    assert "parse_config" in dropped


def test_candidate_renaming_symbol_fails():
    """A candidate that RENAMES parse_config breaks the later call."""
    resolved = "def parse_config():\n    return {}\n"
    fut = _commit("f1", "use config")
    obls = extract_future_obligations(
        resolved_text=resolved, future_commits=[fut],
        patches={"f1": _patch("x = parse_config()\n")},
    )
    candidate = "def load_config():\n    return {}\n"
    ok, dropped = obligations_satisfied(obls, candidate)
    assert not ok
    assert "parse_config" in dropped


def test_no_required_symbols_always_satisfied():
    obls = extract_future_obligations(
        resolved_text="x = 1\n", future_commits=[], patches={},
    )
    ok, dropped = obligations_satisfied(obls, "anything")
    assert ok and dropped == []
