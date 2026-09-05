"""Sprint 18 WS3: deletion-respect swap (pre-stage resurrection repair).

tokio-0037/0046 shape: every conflict unit resolves correctly
(current_only), but git's auto-merge context OUTSIDE the markers
re-introduces content the upstream branch deleted. The end-of-rebase
resurrection scan stops the rebase — correct, but too late to repair. The
deletion-respect swap runs PRE-STAGE (merge-index stages still readable):
when the buffer carries upstream-deleted blocks and is otherwise the
upstream side, swap in the verified upstream side.
"""

from __future__ import annotations

from pathlib import Path

from capybase.config import Config
from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.orchestrator import Orchestrator

from tests.conftest import git
from tests.multistep_builder import CommitEdit, build_multistep_rebase


DEAD_BLOCK = "\n".join(f"def dead_{i}():\n    return {i}" for i in range(4))
# A realistic live region: enough unique lines that the dead block is a small
# fraction of the buffer (tokio-0037: 12 dead lines vs ~200 live lines).
LIVE = "\n".join(f"live_{i} = {i}" for i in range(90))
BASE = f"{LIVE}\n\n{DEAD_BLOCK}\n\ntail = 2\n"
CUR = f"{LIVE}\n\ntail = 2\n"                      # upstream deleted the block
REP = f"{LIVE}\n\n{DEAD_BLOCK}\n\ntail = 3\n"     # replayed kept it + edit


def _repo_at_conflict(repo: Path) -> None:
    git(repo, "init", "-q", "-b", "main")
    build_multistep_rebase(
        repo,
        base_files={"app.py": BASE},
        feat_commits=[CommitEdit("feat: keep block + edit tail",
                                 {"app.py": REP})],
        main_commits=[CommitEdit("main: delete dead block",
                                 {"app.py": CUR})],
        stop_early=True,
    )


def _unit(repo: Path) -> ConflictUnit:
    worktree = (repo / "app.py").read_text()
    return ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="app.py:1:0",
        unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=BASE),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=CUR),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=REP),
        original_worktree_text=worktree, marker_span=(2, 4),
    )


def _orch(repo: Path) -> Orchestrator:
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    return Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)


def test_swap_replaces_context_resurrection(tmp_path: Path):
    repo = tmp_path / "r"
    repo.mkdir()
    _repo_at_conflict(repo)
    orch = _orch(repo)
    unit = _unit(repo)
    # The buffer the per-unit flow produced: correct current_only units, but
    # git's auto-merge context carried the dead block back in.
    buffer = CUR.replace("tail = 2", f"\n{DEAD_BLOCK}\ntail = 2")
    out = orch._try_deletion_respect_swap("app.py", "python", [unit], buffer)
    assert out is not None, "context resurrection should swap to upstream"
    assert out[0][1].resolved_text == CUR
    assert out[0][1].provenance == "deterministic_source_current_only"
    events = [e.event_type for e in orch.journal.read_events()]
    assert "deletion_respect_swap" in events


def test_no_swap_when_buffer_is_clean(tmp_path: Path):
    repo = tmp_path / "r"
    repo.mkdir()
    _repo_at_conflict(repo)
    orch = _orch(repo)
    unit = _unit(repo)
    assert orch._try_deletion_respect_swap(
        "app.py", "python", [unit], CUR) is None
    # no probe event either — nothing resurrected
    events = [e.event_type for e in orch.journal.read_events()]
    assert "deletion_respect_swap_probe" not in events


def test_no_swap_for_woven_merge(tmp_path: Path):
    """A buffer weaving REAL replayed-side features (not just git context)
    must not be swapped: containment in the upstream side is too low, the
    end-of-rebase scan decides."""
    repo = tmp_path / "r"
    repo.mkdir()
    _repo_at_conflict(repo)
    orch = _orch(repo)
    unit = _unit(repo)
    woven = (
        "live = 1\n"
        + "replayed_feature = True\n" * 20  # substantial replayed-side content
        + f"\n{DEAD_BLOCK}\ntail = 3\n"
    )
    assert orch._try_deletion_respect_swap(
        "app.py", "python", [unit], woven) is None


def test_no_swap_when_detection_disabled(tmp_path: Path):
    repo = tmp_path / "r"
    repo.mkdir()
    _repo_at_conflict(repo)
    cfg = Config()
    cfg.model.model = "fake"
    cfg.validation.enable_resurrection_detection = False
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    unit = _unit(repo)
    buffer = CUR.replace("tail = 2", f"\n{DEAD_BLOCK}\ntail = 2")
    assert orch._try_deletion_respect_swap(
        "app.py", "python", [unit], buffer) is None


# ---------------------------------------------------------------------------
# Sprint 18 WS4: side-collapse guard (sea-orm-0027 class)
# ---------------------------------------------------------------------------

COLLAPSE_BASE = "\n".join(f"orig_{i} = {i}" for i in range(40)) + "\n"
COLLAPSE_CUR = "\n".join(f"cur_{i} = {i}" for i in range(30)) + "\n" + \
    "\n".join(f"orig_{i} = {i}" for i in range(30, 40)) + "\n"   # rewrote 75%
COLLAPSE_REP = "\n".join(f"rep_{i} = {i}" for i in range(28)) + "\n" + \
    "\n".join(f"orig_{i} = {i}" for i in range(28, 40)) + "\n"   # rewrote 70%


def _collapse_repo(repo: Path) -> None:
    """Both sides rewrote most of a 40-line file (deep in the woven band);
    git stops at the conflict."""
    git(repo, "init", "-q", "-b", "main")
    build_multistep_rebase(
        repo,
        base_files={"app.py": COLLAPSE_BASE},
        feat_commits=[CommitEdit("feat: rewrite most", {"app.py": COLLAPSE_REP})],
        main_commits=[CommitEdit("main: rewrite more", {"app.py": COLLAPSE_CUR})],
        stop_early=True,
    )


def _collapse_unit(repo: Path) -> ConflictUnit:
    return ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="app.py:1:0", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=COLLAPSE_BASE),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=COLLAPSE_CUR),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=COLLAPSE_REP),
        original_worktree_text=(repo / "app.py").read_text(),
        marker_span=(0, 5),
    )


def test_detect_side_collapse_fires_on_verbatim_side():
    from capybase.orchestrator import _detect_side_collapse
    det = _detect_side_collapse(COLLAPSE_BASE, COLLAPSE_CUR, COLLAPSE_REP,
                                COLLAPSE_REP)  # buffer = replayed verbatim
    assert det is not None and det["collapsed_to"] == "replayed"
    assert det["current_new_kept"] <= 0.10


def test_detect_side_collapse_declines_on_woven_merge():
    from capybase.orchestrator import _detect_side_collapse
    woven = ("\n".join(f"cur_{i} = {i}" for i in range(15)) + "\n"
             + "\n".join(f"rep_{i} = {i}" for i in range(14)) + "\n"
             + "\n".join(f"orig_{i} = {i}" for i in range(28, 40)) + "\n")
    assert _detect_side_collapse(COLLAPSE_BASE, COLLAPSE_CUR, COLLAPSE_REP,
                                 woven) is None


def test_detect_side_collapse_declines_outside_both_rewrite():
    """One side barely touched the file (small loser churn): verbatim winner
    picks are frequently correct there (79 corpus cases) — no collapse."""
    from capybase.orchestrator import _detect_side_collapse
    small_edit = COLLAPSE_BASE.replace("orig_0 = 0", "orig_0 = 42")
    det = _detect_side_collapse(COLLAPSE_BASE, COLLAPSE_REP, small_edit,
                                COLLAPSE_REP)
    assert det is None


class _AdjClient:
    """Engine client whose subsumption adjudication verdict is scripted."""

    def __init__(self, payload: str):
        self._payload = payload

    def complete(self, messages, **kw):
        from capybase.adapters.llm_openai import LLMResponse
        return LLMResponse(text=self._payload)


def _collapse_orch(repo: Path, adj_payload: str | None):
    from capybase.resolution_engine import ResolutionEngine
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    engine = (ResolutionEngine(cfg.model, client=_AdjClient(adj_payload))
              if adj_payload is not None else None)
    return Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)


class _StepResult:
    escalated = False
    reason: str | None = None
    step_index = 1


def test_collapse_guard_escalates_on_keep_verdict(tmp_path: Path):
    import json as _json
    repo = tmp_path / "r"
    repo.mkdir()
    _collapse_repo(repo)
    orch = _collapse_orch(
        repo, _json.dumps({"verdict": "keep", "confidence": 0.9,
                           "reason": "current's rewrite adds real API"}))
    unit = _collapse_unit(repo)
    res = _StepResult()
    fired = orch._check_side_collapse("app.py", "python", [unit],
                                      COLLAPSE_REP, res)
    assert fired and res.escalated
    assert "side collapse" in res.reason and "current" in res.reason
    events = [e.event_type for e in orch.journal.read_events()]
    assert "side_collapse_probe" in events
    assert "side_collapse_adjudication" in events


def test_collapse_guard_accepts_on_superseded_verdict(tmp_path: Path):
    import json as _json
    repo = tmp_path / "r"
    repo.mkdir()
    _collapse_repo(repo)
    orch = _collapse_orch(
        repo, _json.dumps({"verdict": "superseded", "confidence": 0.9,
                           "reason": "replayed subsumes current"}))
    unit = _collapse_unit(repo)
    res = _StepResult()
    assert not orch._check_side_collapse("app.py", "python", [unit],
                                         COLLAPSE_REP, res)
    assert not res.escalated


def test_collapse_guard_accepts_without_endpoint(tmp_path: Path):
    """No endpoint → adjudication unavailable → conservative accept (the
    guard must not hard-escalate on churn numbers alone)."""
    repo = tmp_path / "r"
    repo.mkdir()
    _collapse_repo(repo)
    orch = _collapse_orch(repo, adj_payload=None)
    unit = _collapse_unit(repo)
    res = _StepResult()
    assert not orch._check_side_collapse("app.py", "python", [unit],
                                         COLLAPSE_REP, res)
    assert not res.escalated


def test_collapse_guard_disabled_by_flag(tmp_path: Path):
    import json as _json
    repo = tmp_path / "r"
    repo.mkdir()
    _collapse_repo(repo)
    orch = _collapse_orch(
        repo, _json.dumps({"verdict": "keep", "confidence": 0.9}))
    orch.config.future.enable_side_collapse_guard = False
    unit = _collapse_unit(repo)
    res = _StepResult()
    assert not orch._check_side_collapse("app.py", "python", [unit],
                                         COLLAPSE_REP, res)


# ---------------------------------------------------------------------------
# Sprint 18 WS5: oversized empty fast-fail (skip dead retries)
# ---------------------------------------------------------------------------

def test_first_empty_oversized_prompt_skips_retries(tmp_path: Path):
    """A >= 6K-token prompt whose first LLM response is EMPTY goes straight
    to deterministic recovery — one model call total, not 30-60s of retries
    on a prompt the endpoint will never answer."""
    import json as _json
    from capybase.adapters.llm_openai import LLMResponse
    from capybase.resolution_engine import ResolutionEngine

    class _EmptyClient:
        calls = 0

        def complete(self, messages, **kw):
            type(self).calls += 1
            return LLMResponse(text="")

    # The CONFLICT BLOCK must be huge (the estimator measures the block,
    # not the file): both sides rewrite all ~700 long lines so the whole
    # file is one marker hunk ≈ 40K chars ≈ 10K estimated tokens.
    base = "\n".join(
        f'value_{i:04d} = "x" * 40 + "{i}"' for i in range(700)) + "\n"
    rep = "\n".join(
        f'repval_{i:04d} = "y" * 40 + "{i}"' for i in range(700)) + "\n"
    cur = "\n".join(
        f'curval_{i:04d} = "z" * 40 + "{i}"' for i in range(700)) + "\n"

    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    build_multistep_rebase(
        repo,
        base_files={"app.py": base},
        feat_commits=[CommitEdit("feat: rewrite", {"app.py": rep})],
        main_commits=[CommitEdit("main: rewrite", {"app.py": cur})],
        stop_early=True,
    )
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    cfg.future.enable_source_portfolio = False      # reach the LLM path
    cfg.future.enable_structural_resolver = False
    engine = ResolutionEngine(cfg.model, client=_EmptyClient())
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    # The oversized empty fast-fail fired and recovered deterministically.
    empties = [e.payload for e in orch.journal.read_events()
               if e.event_type == "llm_empty_fragment"]
    assert empties and empties[0]["oversized"] is True
    assert empties[0]["token_estimate"] >= 6000
    # Exactly ONE generation round — count context_built events (client
    # calls also include the side-collapse guard's adjudication of the
    # recovered verbatim side, which is a legitimate consult). The pre-fix
    # flow burned a full retry ladder: 3 rounds of regeneration.
    rounds = [e for e in orch.journal.read_events()
              if e.event_type == "context_built"]
    assert len(rounds) == 1, (
        f"retries must be skipped on the oversized arm; saw {len(rounds)} "
        f"generation rounds")
    assert not result.escalated


# ---------------------------------------------------------------------------
# Sprint 18 (post-validation): transport failures never feed the side pick
# ---------------------------------------------------------------------------

def test_transport_failure_does_not_become_a_side_pick(tmp_path: Path):
    """A request that never completed (failure_kind=request_failed) must NOT
    trigger the first-empty fast-fail's deterministic side pick — the endpoint
    expressed no opinion about the conflict. s18 validation: sea-orm-0027
    shipped a one-side merge (ORACLE_DIVERGENT) during a transient network
    outage because the empty-fallback mistook "no route to host" for an empty
    model verdict. Expected now: escalate honestly after the retry ladder."""
    from capybase.resolution_engine import ResolutionEngine

    class _DeadClient:
        def complete(self, messages, **kw):
            raise ConnectionError("no route to host (simulated outage)")

    base = "def parse():\n    return 1\n"
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    build_multistep_rebase(
        repo,
        base_files={"cfg.py": base},
        feat_commits=[CommitEdit("feat: bump", {"cfg.py": "def parse():\n    return 2\n"})],
        main_commits=[CommitEdit("main: bump too", {"cfg.py": "def parse():\n    return 99\n"})],
        stop_early=True,
    )
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    cfg.future.enable_source_portfolio = False      # reach the LLM path
    cfg.future.enable_structural_resolver = False
    engine = ResolutionEngine(cfg.model, client=_DeadClient())
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    # No deterministic side pick happened on a transport failure...
    accepts = [e.payload for e in orch.journal.read_events()
               if e.event_type == "candidate_accepted"]
    assert not any(a.get("via") == "empty_fast_fail" for a in accepts), (
        "a transport failure must never be converted into a side pick")
    # ...and the empty-fragment journal (if any) records the true cause.
    frags = [e.payload for e in orch.journal.read_events()
             if e.event_type == "llm_empty_fragment"]
    assert all(f.get("failure_kind") == "request_failed" for f in frags)
    # The honest outcome during an outage: escalation, not a silent merge.
    assert result.escalated


# ---------------------------------------------------------------------------
# EXTEND-95: the deletion-respect PRUNE arm (the mirror shape).
# clickhouse-0013: current is a pure DELETION side, replayed is the REWRITE;
# the buffer carries replayed's rewrite but resurrects current's deleted
# blocks. The swap arm declines (buffer not >=0.90 in current — correctly,
# its side-swap would install the pre-rewrite upstream file); the prune arm
# removes the resurrected blocks: replayed minus current's deletions — the
# formula that reproduces the human oracle at 0.998 token-Jaccard.
# ---------------------------------------------------------------------------

# The mirror fixture: current DELETES the dead block; replayed REWRITES
# (keeps the block region's old content and adds new lines).
M_LIVE = "\n".join(f"live_{i} = {i}" for i in range(90))
M_BASE = f"{M_LIVE}\n\n{DEAD_BLOCK}\n\ntail = 2\n"
M_CUR = f"{M_LIVE}\n\ntail = 2\n"                      # pure deletion side
M_REP = (f"{M_LIVE}\n\n{DEAD_BLOCK}\n\nnew_rewrite_1 = 1\n"
         f"new_rewrite_2 = 2\n\ntail = 3\n")           # rewrite + kept block


def _mirror_repo(repo: Path) -> None:
    git(repo, "init", "-q", "-b", "main")
    build_multistep_rebase(
        repo,
        base_files={"app.py": M_BASE},
        feat_commits=[CommitEdit("feat: rewrite + keep", {"app.py": M_REP})],
        main_commits=[CommitEdit("main: delete dead block",
                                 {"app.py": M_CUR})],
        stop_early=True,
    )


def _mirror_unit(repo: Path) -> ConflictUnit:
    worktree = (repo / "app.py").read_text()
    return ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="app.py:1:0",
        unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=M_BASE),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=M_CUR),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=M_REP),
        original_worktree_text=worktree, marker_span=(2, 4),
    )


def test_prune_repairs_replayed_side_resurrection(tmp_path: Path):
    """Buffer = replayed verbatim (resurrects current's deletions) → the
    prune arm returns replayed-minus-deletions: dead block gone, replayed's
    NEW rewrite lines kept."""
    repo = tmp_path / "r"
    repo.mkdir()
    _mirror_repo(repo)
    orch = _orch(repo)
    unit = _mirror_unit(repo)
    out = orch._try_deletion_respect_swap(
        "app.py", "python", [unit], M_REP)  # buffer IS the replayed side
    assert out is not None
    text = out[0][1].resolved_text
    assert "dead_0" not in text and "dead_3" not in text   # deletions honored
    assert "new_rewrite_1" in text and "new_rewrite_2" in text  # rewrite kept
    assert out[0][1].model_name == "deletion_respect_prune"


def test_no_prune_for_woven_buffer(tmp_path: Path):
    """A buffer not >=0.90 contained in replayed (a real woven merge) is
    neither arm's shape → None."""
    repo = tmp_path / "r"
    repo.mkdir()
    _mirror_repo(repo)
    orch = _orch(repo)
    unit = _mirror_unit(repo)
    woven = f"{M_LIVE}\n\nwoven_new = 42\n\ntail = 3\n"
    assert orch._try_deletion_respect_swap("app.py", "python", [unit], woven) is None


def test_no_prune_when_resurrection_partial(tmp_path: Path):
    """The dead block only PARTIALLY resurrected → no exact run → decline
    (the collapse guard's adjudication remains the honest outcome)."""
    from capybase.merge_intent import find_block_run, detect_resurrection
    # sanity: partial block (half the lines) is detected as a finding but
    # not locatable as an exact run
    partial = "\n".join(DEAD_BLOCK.splitlines()[:2])
    assert find_block_run(f"x()\n{partial}\ny()\n", DEAD_BLOCK) is None
    assert find_block_run(f"x()\n{DEAD_BLOCK}\ny()\n", DEAD_BLOCK) == (1, 1 + len(DEAD_BLOCK.splitlines()))
    # end-to-end: a buffer whose resurrection is partial declines.
    repo = tmp_path / "r"
    repo.mkdir()
    _mirror_repo(repo)
    orch = _orch(repo)
    unit = _mirror_unit(repo)
    half_block = "\n".join(DEAD_BLOCK.splitlines()[:2])
    buf = f"{M_LIVE}\n\n{half_block}\n\nnew_rewrite_1 = 1\n\ntail = 3\n"
    assert detect_resurrection(M_BASE, M_CUR, buf) == []
    assert orch._try_deletion_respect_swap("app.py", "python", [unit], buf) is None


def test_collapse_guard_skips_prune_provenance(tmp_path: Path):
    """A buffer accepted via deletion_respect_prune is one-sided BY
    CONSTRUCTION with the deletion side honored — the guard must not
    re-adjudicate it (clickhouse-0013: the 2-1 split would veto the oracle
    at 0.998)."""
    repo = tmp_path / "r"
    repo.mkdir()
    _mirror_repo(repo)
    orch = _orch(repo)
    unit = _mirror_unit(repo)
    pruned = M_REP.replace(DEAD_BLOCK + "\n\n", "")
    accepted = [(unit, type("C", (), {
        "model_name": "deletion_respect_prune",
        "flagged_by_preservation_heuristic": False,
        "resolved_text": pruned})())]
    # _StepResult shim (same as the guard tests above)
    class _R:
        escalated = False
        reason = ""
        step_index = 1
    from capybase.verification import VerificationEngine
    orch.verification = VerificationEngine.__new__(VerificationEngine)
    assert orch._check_side_collapse(
        "app.py", "python", [unit], pruned, _R(), accepted=accepted) is False


def test_prune_handles_interleaved_resurrection(tmp_path):
    """A rewrite that WOVE the deleted block's lines among new content (the
    real clickhouse-0013 shape: 2 of 6 blocks non-contiguous) — span removal
    declines per-block, multiset line removal honors the deletion (one
    occurrence per deleted line), and the compile gate still protects."""
    from capybase.merge_intent import find_block_run
    dead_lines = DEAD_BLOCK.splitlines()
    # The REWRITE itself wove the old block's lines among new additions —
    # the replayed side IS the interleaving (real 0013 shape: 2 of 6 blocks).
    woven_rep = (
        f"{M_LIVE}\n\n"
        f"{dead_lines[0]}\nnew_a = 1\n"
        f"{dead_lines[1]}\nnew_b = 2\n"
        f"{dead_lines[2]}\nnew_c = 3\n"
        f"{dead_lines[3]}\nnew_d = 4\n"
        f"{dead_lines[4]}\nnew_e = 5\n"
        f"{dead_lines[5]}\nnew_f = 6\n"
        f"{dead_lines[6]}\nnew_g = 7\n"
        f"{dead_lines[7]}\nnew_h = 8\n"
        "\ntail = 3\n"
    )
    assert find_block_run(woven_rep, DEAD_BLOCK) is None  # not contiguous
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    build_multistep_rebase(
        repo,
        base_files={"app.py": M_BASE},
        feat_commits=[CommitEdit("feat: woven rewrite", {"app.py": woven_rep})],
        main_commits=[CommitEdit("main: delete dead block",
                                 {"app.py": M_CUR})],
        stop_early=True,
    )
    worktree = (repo / "app.py").read_text()
    unit = ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="app.py:1:0",
        unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=M_BASE),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=M_CUR),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=woven_rep),
        original_worktree_text=worktree, marker_span=(2, 4),
    )
    orch = _orch(repo)
    # Buffer = the woven replayed side verbatim.
    out = orch._try_deletion_respect_swap("app.py", "python", [unit], woven_rep)
    assert out is not None
    text = out[0][1].resolved_text
    for dl in dead_lines:
        assert dl not in text, dl          # every deleted line honored
    for keep in ("new_a = 1", "new_h = 8", "live_0 = 0", "tail = 3"):
        assert keep in text                # the rewrite's additions survive
