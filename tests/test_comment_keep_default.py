"""STAGED S28-23 prong-1 tests — move into tests/ when implementing.

Design (insertion spec in apply_s28_23_prong1.md): when a plan's ONLY
verifier failures are UNACCOUNTED_COMMENT, extend the plan with `keep`
actions for the unaccounted lineages, re-apply + re-verify once; clean
→ success (journal `comment_unaccounted_defaulted_keep` with the ids);
anything else (other failure kinds, ApplyError on re-apply) → the
existing reject path unchanged.

Run:  .venv/bin/python -m pytest tests/test_comment_keep_default.py -q
"""
from capybase.comment_reconciler import (
    CommentAction, CommentPlan, LedgerEntry, run_comment_cegis,
)
from capybase.adapters.comment_classifier import CommentClass


def _frontier(n: int = 5) -> list[LedgerEntry]:
    # Five deferred comments at distinct offsets in a python buffer. Spans
    # EXCLUDE the trailing newline (the real enumerate_comment_spans
    # convention — a rewrite replaces exactly the comment text).
    out = []
    pos = 0
    for i in range(1, n + 1):
        text = f"# comment {i}"
        out.append(LedgerEntry(
            lineage_id=f"LC{i}", version="resolved", text=text,
            cls=CommentClass.DEFERRED, start=pos, end=pos + len(text),
        ))
        pos += len(text) + 1 + len(f"x = {i}\n")
    return out


def _buffer(entries: list[LedgerEntry]) -> str:
    parts = []
    for i, e in enumerate(entries, 1):
        parts.append(e.text + "\n")
        parts.append(f"x = {i}\n")
    return "".join(parts)


def _run(propose_responses: list[str], entries=None):
    entries = entries or _frontier()
    buf = _buffer(entries)
    calls = {"n": 0}

    def propose(prompt: str) -> str:
        resp = propose_responses[calls["n"]]
        calls["n"] += 1
        return resp

    return run_comment_cegis(
        buffer=buf, frontier=entries, base=buf, current=buf,
        replayed=buf, lang="python", propose=propose, budget=1,
    ), calls["n"]


def _plan_json(actions: list[dict]) -> str:
    import json
    return json.dumps({"actions": actions})


# 1. Salvageable prefix: truncated plan covers LC1 (rewrite) + LC2 (keep);
#    LC3-5 unaccounted → default to keep, apply the rewrite, succeed.
def test_truncated_prefix_defaults_keep_and_applies_rewrite():
    # plan `text` is the comment CONTENT (the formatter re-applies the
    # comment syntax prefix).
    raw = _plan_json([
        {"lineage_id": "LC1", "operation": "rewrite", "text": "rewritten one"},
        {"lineage_id": "LC2", "operation": "keep"},
    ])
    outcome, n_calls = _run([raw])
    assert outcome.succeeded, [e for e in outcome.events if e[0] == "comment_reconciliation_failed"]
    assert "# rewritten one" in outcome.buffer
    # executable code untouched
    assert "x = 1" in outcome.buffer and "x = 5" in outcome.buffer
    defaulted = [p.get("defaulted") for name, p in outcome.events
                 if name == "comment_reconciled_partial"]
    # partial is NOT a success event: comment_reconciled must not fire
    assert not any(name == "comment_reconciled" for name, _ in outcome.events)
    assert defaulted and defaulted[0] == ["LC3", "LC4", "LC5"]
    assert n_calls == 1  # no retry burned on a salvageable plan


# 2. Pure-keep prefix: plan covers LC1 only with keep; everything else
#    defaults to keep → no-op success.
def test_pure_keep_prefix_is_noop_success():
    raw = _plan_json([{"lineage_id": "LC1", "operation": "keep"}])
    outcome, n_calls = _run([raw])
    assert outcome.succeeded
    assert n_calls == 1


# 3. Other failure kinds still reject: a rewrite on a non-frontier lineage
#    (INVALID_ANCHOR) must NOT be rescued by keep-defaulting.
def test_invalid_anchor_still_rejects():
    raw = _plan_json([
        {"lineage_id": "LC1", "operation": "keep"},
        {"lineage_id": "LC99", "operation": "rewrite", "text": "# ghost"},
    ])
    outcome, _ = _run([raw])
    assert not outcome.succeeded
    assert not any(name == "comment_unaccounted_defaulted_keep"
                   for name, _ in outcome.events)
