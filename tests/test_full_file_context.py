"""Full-file context propagation — whole-file asymmetry signals.

Region-level units carry FRAGMENT side texts; the reverted
parent_deletion_override rule (14681db) failed because fragment-level
signals can't answer "which side rewrote the file". These tests pin the
pure full-file analyses in merge_intent (churn, context dict, takeover
gates) and their stamping on every unit at extraction — including entity
sub-units. The gate fixtures mirror the corpus cases that calibrate the
thresholds: 0073 (wholesale deletion → fires), 0061 (symmetric additive →
declines), 0046 (high ratio, tiny churn → declines), and a merge that
respects the deletion (declines — no harm, no takeover).
"""

from __future__ import annotations

from capybase.conflict_extractor import ConflictExtractor
from capybase.git_backend import GitBackend
from capybase.merge_intent import (
    FULL_FILE_ASYMMETRY_RATIO,
    FULL_FILE_STALE_FRACTION,
    FULL_FILE_STALE_MIN_LINES,
    asymmetry_takeover_gates,
    full_file_context,
    side_churn,
)

from tests.conftest import git


# ---------------------------------------------------------------------------
# side_churn / full_file_context
# ---------------------------------------------------------------------------

def test_side_churn_counts_both_directions():
    base = "a\nb\nc\nd\n"
    side = "a\nx\nc\n"          # b→x replace, d deleted
    # replace: 1 removed + 1 added; delete: 1 removed → 3
    assert side_churn(base, side) == 3
    assert side_churn(base, base) == 0


def test_full_file_context_wholesale_deletion_0073_shape():
    base = "\n".join(f"line{i}" for i in range(100)) + "\n"
    current = "line0\nline1\nline2\n"                      # deleted 97
    replayed = "\n".join(f"line{i}" for i in range(98)) + "\nnew\n"  # ~3 churn
    ctx = full_file_context(base, current, replayed)
    assert ctx["asymmetry_side"] == "current"
    assert ctx["deleting_side"] == "current"
    assert ctx["churn_ratio"] >= FULL_FILE_ASYMMETRY_RATIO
    assert ctx["current_churn"] > ctx["replayed_churn"]
    assert ctx["base_lines"] == 100
    assert ctx["current_lines"] == 3


def test_full_file_context_symmetric_additive_0061_shape():
    base = "\n".join(f"b{i}" for i in range(50)) + "\n"
    current = base + "cur_add\n"
    replayed = base + "rep_add\n"
    ctx = full_file_context(base, current, replayed)
    assert ctx["asymmetry_side"] is None
    assert ctx["deleting_side"] is None
    assert ctx["churn_ratio"] == 0.0


def test_full_file_context_net_adder_has_no_deleting_side():
    # Asymmetric (current rewrote wholesale) but current GREW — the
    # deleting_side framing doesn't apply; asymmetry_side still set.
    base = "\n".join(f"b{i}" for i in range(50)) + "\n"
    current = "\n".join(f"x{i}" for i in range(80)) + "\n"
    replayed = base + "r\n"
    ctx = full_file_context(base, current, replayed)
    assert ctx["asymmetry_side"] == "current"
    assert ctx["deleting_side"] is None


# ---------------------------------------------------------------------------
# asymmetry_takeover_gates — corpus-calibrated separation
# ---------------------------------------------------------------------------

def _mkfile(n, prefix="l"):
    return "\n".join(f"{prefix}{i}" for i in range(n)) + "\n"


def test_gates_fire_on_stale_loser_content():
    # 0073 shape: current rewrote wholesale; the merge keeps the stale
    # side's conflict-region content on top — 36% of the merge is absent
    # from the winner. Corpus separation: failing merges ~0.36, good
    # merges <= 0.021.
    base = _mkfile(100)
    current = "l0\nl1\n"                      # winner deleted 98 lines
    replayed = _mkfile(98)                    # near-base
    merged = _mkfile(40)                      # merge kept stale content
    g = asymmetry_takeover_gates(base, current, replayed, merged)
    assert g["ratio_ok"] and g["dominance_ok"] and g["stale_ok"]
    assert g["winner"] == "current"
    assert g["fires"] is True


def test_gates_decline_symmetric_additive_0061_shape():
    base = _mkfile(50)
    current = base + "cur\n"
    replayed = base + "rep\n"
    merged = base + "cur\nrep\n"
    g = asymmetry_takeover_gates(base, current, replayed, merged)
    assert g["ratio_ok"] is False
    assert g["fires"] is False


def test_gates_decline_small_churn_high_ratio_0046_shape():
    # Ratio clears 0.90 but the winner churned only 20 of 300 lines — both
    # sides made minor edits; a takeover would drop the other side's real work.
    base = _mkfile(300)
    current = _mkfile(299)                    # 1-line deletion (churn 1)
    replayed = _mkfile(282) + "rep1\nrep2\n"  # ~20-line change (churn ~20)
    merged = _mkfile(280) + "cur\nrep1\nrep2\n"
    g = asymmetry_takeover_gates(base, current, replayed, merged)
    assert g["ratio_ok"] is True          # high ratio...
    assert g["dominance_ok"] is False     # ...but no dominant rewrite
    assert g["fires"] is False


def test_gates_decline_when_merge_matches_winner():
    # Wholesale deletion, dominant churn — and the merge ≈ the winner with
    # only tiny fresh additions (< 15% stale): no takeover, keep the merge.
    base = _mkfile(100)
    current = _mkfile(20)                     # winner deleted 80 lines
    replayed = _mkfile(98)
    merged = _mkfile(20) + "new_line\n"       # one fresh line (~5% stale)
    g = asymmetry_takeover_gates(base, current, replayed, merged)
    assert g["ratio_ok"] and g["dominance_ok"]
    assert g["stale_fraction"] < FULL_FILE_STALE_FRACTION
    assert g["fires"] is False


def test_gates_absolute_floor_prevents_tiny_merge_fires():
    # Winner reduced to 5 lines; the merge adds 1 legitimate loser line →
    # 17% stale by fraction, but only 1 stale line — below the absolute
    # floor, so no takeover.
    base = _mkfile(100)
    current = _mkfile(5)
    replayed = _mkfile(98)
    merged = _mkfile(5) + "fix_line\n"
    g = asymmetry_takeover_gates(base, current, replayed, merged)
    assert g["stale_fraction"] >= FULL_FILE_STALE_FRACTION
    assert g["stale_lines"] < FULL_FILE_STALE_MIN_LINES
    assert g["fires"] is False


def test_gates_whitespace_drift_is_not_stale():
    # The same lines with different indentation must not count as stale —
    # only genuinely absent content does.
    base = _mkfile(100)
    current = _mkfile(20)
    replayed = _mkfile(98)
    merged = "\n".join("    " + f"l{i}" for i in range(20)) + "\n"
    g = asymmetry_takeover_gates(base, current, replayed, merged)
    assert g["stale_fraction"] == 0.0
    assert g["fires"] is False


def test_gates_journal_sample_lists_stale_lines():
    base = _mkfile(100)
    current = _mkfile(5)
    replayed = _mkfile(98)
    merged = _mkfile(50)                      # 45 stale lines
    g = asymmetry_takeover_gates(base, current, replayed, merged)
    assert g["fires"] is True
    assert g["stale_lines"] >= FULL_FILE_STALE_MIN_LINES
    assert 1 <= len(g["stale_sample"]) <= 3


# ---------------------------------------------------------------------------
# Edge shapes in full_file_context
# ---------------------------------------------------------------------------

def test_full_file_context_empty_winner_side():
    # The winner deleted the file to nothing: churn = base, dominance set,
    # deleting_side = winner. No division-by-zero anywhere.
    base = _mkfile(50)
    ctx = full_file_context(base, "", base + "x\n")
    assert ctx["asymmetry_side"] == "current"
    assert ctx["deleting_side"] == "current"
    assert ctx["current_churn"] == 50
    assert 0.0 <= ctx["churn_ratio"] <= 1.0


def test_full_file_context_zero_base_add_add():
    # Both sides added the file (no base): near-symmetric churn, no
    # asymmetry, no crash.
    ctx = full_file_context("", "a\nb\n", "a\nc\n")
    assert ctx["base_lines"] == 0
    assert ctx["asymmetry_side"] is None
    assert ctx["deleting_side"] is None


# ---------------------------------------------------------------------------
# _try_whole_file_portfolio shape fix (fragment-base units + stage texts)
# ---------------------------------------------------------------------------

def test_whole_file_portfolio_uses_stage_texts_not_fragment_base():
    from capybase.conflict_model import CandidateResolution, ConflictSide, ConflictUnit
    from capybase.orchestrator import _try_whole_file_portfolio

    base = "\n".join(f"b{i}" for i in range(60)) + "\n"
    rep_frag = "\n".join(f"r{i}" for i in range(30))
    original = (
        "b0\n<<<<<<< A\nfrag_cur\n=======\n" + rep_frag
        + "\n>>>>>>> B\n" + "\n".join(f"b{i}" for i in range(1, 40))
        + "\n<<<<<<< A\nfrag_cur2\n=======\n" + rep_frag
        + "\n>>>>>>> B\nb41\n"
    )
    # Sub-units: fragment base (empty — add/add split semantics), tiny sides.
    def _unit(uid, span):
        return ConflictUnit(
            session_id="s", step_index=0, path="f.cc", language="cpp",
            unit_id=uid, unit_kind="text_marker_block",
            base=ConflictSide(label="BASE", text=""),
            current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="frag_cur"),
            replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=rep_frag),
            original_worktree_text=original, marker_span=span,
        )

    units = [_unit("f.cc:1:0#s0", (1, 34)), _unit("f.cc:1:1#s0", (76, 109))]
    accepted = [(
        units[0],
        CandidateResolution(
            candidate_id="c0", unit_id=units[0].unit_id, model_name="t",
            prompt_version="t", resolved_text="frag_cur", provenance="t"),
    ), (
        units[1],
        CandidateResolution(
            candidate_id="c1", unit_id=units[1].unit_id, model_name="t",
            prompt_version="t", resolved_text="frag_cur2", provenance="t"),
    )]

    events = []

    class _J:
        def emit(self, name, payload, **kw):
            events.append((name, payload))

    # Without stage texts the function no-ops (fragment base) — the old bug.
    assert _try_whole_file_portfolio(
        units, accepted, original, journal=_J(), path="f.cc") is None
    assert not any(n == "whole_file_portfolio_gate" for n, _ in events)
    # With stage texts it computes and journals the true file shapes.
    true_sides = (
        {"current": base.replace("b59", "b59_cur"), "replayed": base + "extra\n"},
        base,
    )
    _try_whole_file_portfolio(
        units, accepted, original, journal=_J(), path="f.cc",
        true_sides=true_sides)
    gate = next(p for n, p in events if n == "whole_file_portfolio_gate")
    assert gate["sides_source"] == "stages"
    assert gate["base_lines"] == 60


def test_whole_file_portfolio_skips_asymmetric_files():
    # The min()-coverage metric mis-scores asymmetric files (a merge that
    # correctly drops the inert side's lines scores 0.0; a whole-side swap
    # then picks the stale side as often as the right one). Such files are
    # the asymmetry-takeover's territory — the portfolio must decline.
    from capybase.conflict_model import CandidateResolution, ConflictSide, ConflictUnit
    from capybase.orchestrator import _try_whole_file_portfolio

    base = _mkfile(100)
    rep_frag = _mkfile(10)
    original = (
        "l0\n<<<<<<< A\nstub\n=======\n" + rep_frag
        + "\n>>>>>>> B\nshared1\nshared2\n"
        + "<<<<<<< A\nstub\n=======\n" + rep_frag + "\n>>>>>>> B\ntail\n"
    )
    units, accepted = [], []
    for k, span in enumerate([(1, 14), (17, 30)]):
        u = ConflictUnit(
            session_id="s", step_index=0, path="f.cc", language="cpp",
            unit_id=f"f.cc:1:{k}", unit_kind="text_marker_block",
            base=ConflictSide(label="BASE", text=""),
            current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="stub"),
            replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=rep_frag),
            original_worktree_text=original, marker_span=span,
        )
        units.append(u)
        accepted.append((u, CandidateResolution(
            candidate_id=f"c{k}", unit_id=u.unit_id, model_name="t",
            prompt_version="t", resolved_text="stub", provenance="t")))

    events = []

    class _J:
        def emit(self, name, payload, **kw):
            events.append((name, payload))

    true_sides = ({"current": _mkfile(3), "replayed": base}, base)
    assert _try_whole_file_portfolio(
        units, accepted, original, journal=_J(), path="f.cc",
        true_sides=true_sides) is None
    gate = next(p for n, p in events if n == "whole_file_portfolio_gate")
    assert gate["skipped"] == "asymmetric_file"
    assert gate["churn_ratio"] >= 0.90


# ---------------------------------------------------------------------------
# Extraction stamping
# ---------------------------------------------------------------------------

def _wholesale_repo(repo, n: int = 120) -> dict:
    """A repo stopped at a UU conflict shaped like protobuf-0073: current
    (upstream) reduced the file to a stub, replayed kept it near-base."""
    base = _mkfile(n)
    current = "l0\nl1\nl2\n"
    replayed = _mkfile(n - 2) + "tail\n"
    path = "big.cc"

    (repo / path).write_text(base)
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    (repo / path).write_text(replayed)
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", "replayed touch")
    git(repo, "checkout", "-q", "main")
    (repo / path).write_text(current)
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", "upstream rewrite")
    git(repo, "checkout", "-q", "feat")
    r = git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected a rebase conflict"
    return {"repo": repo, "path": path}


def test_full_file_context_stamped_on_units(repo):
    info = _wholesale_repo(repo)
    ex = ConflictExtractor(GitBackend(info["repo"]))
    units = ex.extract_file_units(info["path"], step_index=1, session_id="s1")
    assert units
    for u in units:
        ffc = u.structural_metadata.get("full_file_context")
        assert ffc is not None, f"{u.unit_id} missing full_file_context"
        assert ffc["base_lines"] == 120
        assert ffc["asymmetry_side"] == "current"
        assert ffc["deleting_side"] == "current"


def test_context_survives_entity_split(repo):
    # Same shape in Python with many top-level defs so the marker unit
    # entity-splits; every sub-unit must still carry the full-file context.
    n = 120
    base = "\n".join(f"def f{i}():\n    return {i}\n" for i in range(n // 2))
    current = "def f0():\n    return 0\n"
    replayed = base + "\ndef extra():\n    return 42\n"
    path = "many.py"

    (repo / path).write_text(base)
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    (repo / path).write_text(replayed)
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", "replayed add")
    git(repo, "checkout", "-q", "main")
    (repo / path).write_text(current)
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", "upstream delete")
    git(repo, "checkout", "-q", "feat")
    r = git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected a rebase conflict"

    ex = ConflictExtractor(GitBackend(repo))
    units = ex.extract_file_units(path, step_index=1, session_id="s1")
    assert units
    for u in units:
        ffc = u.structural_metadata.get("full_file_context")
        assert ffc is not None, f"{u.unit_id} missing full_file_context"
        assert ffc["asymmetry_side"] == "current"
