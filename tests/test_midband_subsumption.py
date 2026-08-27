"""Mid-band subsumption takeover — the jsonc-0004 class.

Between the >= 0.90 wholesale band (where taking the churn winner verbatim
is safe on numbers alone) and the symmetric middle, one side's churn can
dominate the other's by a large multiple. Corpus measurement: 100/116
mid-band oracles equal the winner, but 16 counter-examples are genuine
both-sides merges indistinguishable on shape metrics — so the takeover
requires an LLM subsumption adjudication on top of the numeric gates.
These tests pin the pure gate math, the adjudication prompt, the verdict
parsing, and the wiring inside _try_true_side_portfolio's Phase-1 branch.
No network; the engine is always a fake.
"""

from __future__ import annotations

from types import SimpleNamespace

from capybase.merge_intent import (
    FULL_FILE_MIDBAND_DOMINANCE_MULT,
    FULL_FILE_MIDBAND_RATIO_MIN,
    midband_subsumption_gates,
)
from capybase.orchestrator import (
    Orchestrator,
    _subsumption_adjudication_prompt,
)


# ---------------------------------------------------------------------------
# midband_subsumption_gates — pure numeric gates
# ---------------------------------------------------------------------------

def _texts(cur_churn: int, rep_churn: int, base_n: int = 400):
    """Base of base_n distinct lines; each side rewrites its churn budget."""
    base = [f"base line {i}" for i in range(base_n)]
    cur = [f"cur line {i}" if i < cur_churn else f"base line {i}"
           for i in range(base_n)]
    rep = [f"rep line {i}" if i < rep_churn else f"base line {i}"
           for i in range(base_n)]
    return "\n".join(base) + "\n", "\n".join(cur) + "\n", "\n".join(rep) + "\n"


def test_gates_in_band_0004_shape():
    # current churn 200 vs replayed 52 on a 243-line base: ratio 0.74, mult 3.8
    base, cur, rep = _texts(200, 52, 243)
    g = midband_subsumption_gates(base, cur, rep)
    assert g["in_band"] is True
    assert g["winner"] == "current"
    assert g["loser"] == "replayed"
    assert FULL_FILE_MIDBAND_RATIO_MIN <= g["churn_ratio"] < 0.90
    assert g["churn_mult"] >= FULL_FILE_MIDBAND_DOMINANCE_MULT


def test_gates_exclude_wholesale_band():
    # ratio >= 0.90 is the phase1 fast path's territory, not mid-band
    base, cur, rep = _texts(665, 12, 756)  # jsonc-0013 shape: ratio 0.98
    g = midband_subsumption_gates(base, cur, rep)
    assert g["churn_ratio"] >= 0.90
    assert g["in_band"] is False


def test_gates_exclude_symmetric_shape():
    # jsonc-0017 shape: both sides churn similarly → no dominance, no band
    base, cur, rep = _texts(71, 63, 149)
    g = midband_subsumption_gates(base, cur, rep)
    assert g["in_band"] is False


def test_gates_exclude_low_dominance():
    # mid ratio but winner churn < 2.5x the loser's → adjudication cannot
    # be trusted to separate; keep the per-unit cascade
    base, cur, rep = _texts(116, 38, 291)  # mult ~3 — in band actually
    assert midband_subsumption_gates(base, cur, rep)["in_band"] is True
    base, cur, rep = _texts(100, 45, 291)  # mult ~2.2 → below the gate
    g = midband_subsumption_gates(base, cur, rep)
    assert g["churn_mult"] < FULL_FILE_MIDBAND_DOMINANCE_MULT
    assert g["in_band"] is False


def test_gates_winner_can_be_replayed():
    base, cur, rep = _texts(52, 200, 243)
    g = midband_subsumption_gates(base, cur, rep)
    assert g["in_band"] is True
    assert g["winner"] == "replayed"
    assert g["loser"] == "current"


# ---------------------------------------------------------------------------
# _subsumption_adjudication_prompt
# ---------------------------------------------------------------------------

def test_prompt_contains_both_diffs_and_strict_json():
    base = "int a;\nint b;\nint c;\n"
    winner = "int a;\nint b2;\nint c;\n"
    loser = "int a;\nint b;\nint c;\nint d;\n"
    p = _subsumption_adjudication_prompt(
        "f.c", "c", base, "current", winner, loser)
    assert "-int b;" in p and "+int b2;" in p       # winner diff present
    assert "+int d;" in p                            # loser diff present
    assert '"verdict": "keep" or "superseded"' in p   # strict JSON contract
    assert "CURRENT (upstream, being rebased onto) rewrote the file heavily" in p
    assert "REPLAYED (the commit being applied on top) made smaller changes" in p


def test_prompt_labels_swap_for_replayed_winner():
    base = "int a;\n"
    winner = "int a;\nint new;\n"
    loser = "int a2;\n"
    p = _subsumption_adjudication_prompt(
        "f.c", "c", base, "replayed", winner, loser)
    assert "REPLAYED (the commit being applied on top) rewrote the file heavily" in p
    assert "CURRENT (upstream, being rebased onto) made smaller changes" in p


def test_prompt_clips_oversized_diffs():
    base = "\n".join(f"l{i}" for i in range(500)) + "\n"
    winner = "\n".join(f"w{i}" if i % 2 else f"l{i}" for i in range(500)) + "\n"
    p = _subsumption_adjudication_prompt(
        "f.c", "c", base, "current", winner, base, max_diff_lines=50)
    assert "more diff lines truncated" in p


# ---------------------------------------------------------------------------
# _adjudicate_subsumption — verdict parsing (stub orchestrator, fake engine)
# ---------------------------------------------------------------------------

class _RecJournal:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, payload, **_kw):
        self.events.append((event, payload))


class _FakeEngine:
    def __init__(self, texts):
        # Cycle-C: adjudication draws 3 self-consistency samples; cycle a
        # list of responses (a bare string broadcasts to every call).
        self._texts = [texts] if isinstance(texts, str) else list(texts)
        self.calls: list[dict] = []
        self.config = SimpleNamespace(max_tokens=8192)

    def raw_complete(self, prompt, *, json_mode=False, temperature=None,
                     max_tokens=None):
        self.calls.append({"prompt": prompt, "max_tokens": max_tokens})
        text = self._texts.pop(0) if self._texts else ""
        return SimpleNamespace(text=text)


def _stub_orchestrator(engine) -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch.resolution_engine = engine
    orch.journal = _RecJournal()
    orch.step = 1
    return orch


def test_adjudicate_parses_superseded():
    engine = _FakeEngine(
        '{"verdict": "superseded", "confidence": 0.9, "reason": "cosmetic"}')
    orch = _stub_orchestrator(engine)
    out = orch._adjudicate_subsumption(
        "f.c", "c", "int a;\n", {"current": "int a;\n", "replayed": "int b;\n"},
        "current")
    assert out["verdict"] == "superseded"
    assert out["confidence"] == 0.9  # unanimous × 1.0 agreement
    assert out["reason"] == "cosmetic"
    assert out["agreement"] == 1.0
    assert len(engine.calls) == 3  # self-consistency draws three samples
    # decision prompts must clear the local server's hidden pre-fill budget
    assert engine.calls[0]["max_tokens"] >= 1024


def test_adjudicate_unanimous_keep():
    """Unanimous keep is a positive verdict (the cascade stays on)."""
    orch = _stub_orchestrator(_FakeEngine(
        '{"verdict": "keep", "confidence": 0.95, "reason": "real feature"}'))
    out = orch._adjudicate_subsumption(
        "f.c", "c", "int a;\n", {"current": "int a;\n", "replayed": "int b;\n"},
        "current")
    assert out["verdict"] == "keep"
    assert out["confidence"] == 0.95


def test_adjudicate_split_vote_settles_to_keep_below_fire_bar():
    """clickhouse-0021's shape: 2-1 split at high confidence. The majority
    (keep) wins but the agreement-scaled confidence (0.95 × 2/3 = 0.63)
    falls below the caller's 0.70 bar — no takeover on a borderline shape."""
    orch = _stub_orchestrator(_FakeEngine([
        '{"verdict": "keep", "confidence": 0.95, "reason": "feature absent"}',
        '{"verdict": "superseded", "confidence": 0.95, "reason": "integrated"}',
        '{"verdict": "keep", "confidence": 0.95, "reason": "feature absent"}',
    ]))
    out = orch._adjudicate_subsumption(
        "f.c", "c", "int a;\n", {"current": "int a;\n", "replayed": "int b;\n"},
        "current")
    assert out["verdict"] == "keep"
    assert out["confidence"] < 0.70
    assert abs(out["agreement"] - 2 / 3) < 0.01
    assert out["samples"] == [
        {"v": "keep", "c": 0.95},
        {"v": "superseded", "c": 0.95},
        {"v": "keep", "c": 0.95},
    ]


def test_adjudicate_tie_settles_to_keep():
    """Only two valid samples, one each way — a tie settles to keep
    (conservative no-takeover)."""
    orch = _stub_orchestrator(_FakeEngine([
        '{"verdict": "keep", "confidence": 0.9, "reason": "a"}',
        '{"verdict": "superseded", "confidence": 0.9, "reason": "b"}',
        "unparseable",
    ]))
    out = orch._adjudicate_subsumption(
        "f.c", "c", "int a;\n", {"current": "int a;\n", "replayed": "int b;\n"},
        "current")
    assert out["verdict"] == "keep"


def test_adjudicate_empty_response_is_keep():
    # finish_reason=length returns empty content — no verdict, no takeover
    orch = _stub_orchestrator(_FakeEngine(""))
    out = orch._adjudicate_subsumption(
        "f.c", "c", "int a;\n", {"current": "int a;\n", "replayed": "int b;\n"},
        "current")
    assert out is None


def test_adjudicate_garbage_json_is_keep():
    orch = _stub_orchestrator(_FakeEngine("I think the merge should keep it"))
    out = orch._adjudicate_subsumption(
        "f.c", "c", "int a;\n", {"current": "int a;\n", "replayed": "int b;\n"},
        "current")
    assert out is None


# ---------------------------------------------------------------------------
# Wiring — _try_true_side_portfolio's Phase-1 mid-band branch
# ---------------------------------------------------------------------------

class _FakeGit:
    def __init__(self, stages: dict[int, str]):
        self._stages = stages
        self.repo = "/tmp/fake-repo"

    def read_stage_blob(self, path: str, stage: int) -> bytes:
        if stage not in self._stages:
            raise RuntimeError(f"no stage {stage}")
        return self._stages[stage].encode()


class _AlwaysPassVerification:
    def verify_file(self, path, language, original, units, *, repo_root=None,
                    whole_text=None):
        return SimpleNamespace(passed=True, hard_failures=[])


class _KeepEngine(_FakeEngine):
    def __init__(self):
        super().__init__(
            '{"verdict": "keep", "confidence": 1.0, "reason": "new feature"}')


class _SupersededEngine(_FakeEngine):
    def __init__(self):
        super().__init__(
            '{"verdict": "superseded", "confidence": 0.95, "reason": "cosmetic"}')


# The 0013 shape: wholesale churn ratio (>= 0.90 + dominance) on a small base.
_BASE_W, _CUR_W, _REP_W = _texts(400, 4, 300)


def _wiring_orchestrator(engine, base=None, cur=None, rep=None) -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch.resolution_engine = engine
    orch.journal = _RecJournal()
    orch.step = 1
    orch.git = _FakeGit({1: base or _BASE, 2: cur or _CUR, 3: rep or _REP})
    orch.verification = _AlwaysPassVerification()
    orch.config = SimpleNamespace(
        future=SimpleNamespace(
            enable_true_side_asymmetry_takeover=True,
            enable_midband_subsumption_takeover=True,
            enable_wholesale_winner_floor=True),
    )
    orch._write_worktree_only = lambda *a, **k: None
    # No per-file build target by default; the build fail-fast tests
    # override these.
    orch._resolve_per_file_build = lambda path: ""
    orch._run_raw_test = lambda cmd: (True, "")
    return orch


_BASE, _CUR, _REP = _texts(200, 52, 243)  # the 0004 shape: ratio 0.74, mult 3.8


def _units(n=1, base=_BASE, cur=_CUR, rep=_REP):
    from capybase.conflict_model import ConflictSide, ConflictUnit
    return [ConflictUnit(
        session_id="s", step_index=1, path="f.c", language="c",
        unit_id=f"f.c:{i}:0", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=cur),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=rep),
        original_worktree_text=cur,
        marker_span=(0, 1),
    ) for i in range(n)]


def test_midband_keep_declines_to_cascade():
    orch = _wiring_orchestrator(_KeepEngine())
    out = orch._try_true_side_portfolio(
        "f.c", "c", _CUR, _units(), phase1_fast_path=True)
    assert out is None
    gates = [e for e in orch.journal.events
             if e[0] == "midband_subsumption_gate"]
    assert gates and gates[0][1]["in_band"] is True
    assert gates[0][1]["fires"] is False


def test_midband_superseded_takes_winner():
    orch = _wiring_orchestrator(_SupersededEngine())
    out = orch._try_true_side_portfolio(
        "f.c", "c", _CUR, _units(), phase1_fast_path=True)
    assert out is not None
    accepted, buffer, val = out
    assert buffer == _CUR  # winner verbatim
    assert val.passed
    swaps = [e for e in orch.journal.events
             if e[0] == "true_side_portfolio"]
    assert swaps and swaps[0][1]["trigger"] == "midband_subsumption"
    assert swaps[0][1]["side"] == "current"


def test_midband_disabled_flag_skips_adjudication():
    orch = _wiring_orchestrator(_SupersededEngine())
    orch.config.future.enable_midband_subsumption_takeover = False
    out = orch._try_true_side_portfolio(
        "f.c", "c", _CUR, _units(), phase1_fast_path=True)
    assert out is None
    # the flag being off means no LLM call at all
    assert not orch.resolution_engine.calls
    gates = [e for e in orch.journal.events
             if e[0] == "midband_subsumption_gate"]
    assert gates and gates[0][1]["enabled"] is False


# ---------------------------------------------------------------------------
# Wholesale small-unit confirmation + build fail-fast (regression fixes)
# ---------------------------------------------------------------------------

def test_wholesale_many_units_fires_without_adjudication():
    # The timeout class (jsonc-0013 live: dozens of units): fire immediately,
    # no LLM call — the cascade is the thing we're protecting against.
    orch = _wiring_orchestrator(_KeepEngine(), _BASE_W, _CUR_W, _REP_W)
    out = orch._try_true_side_portfolio(
        "f.c", "c", _CUR_W, _units(n=12, base=_BASE_W, cur=_CUR_W, rep=_REP_W),
        phase1_fast_path=True)
    assert out is not None
    assert out[1] == _CUR_W
    assert not orch.resolution_engine.calls  # no adjudication ran


def test_wholesale_few_units_keep_declines_to_cascade():
    # sea-orm-0009: 1 live unit, loser carries real features — the cascade
    # is cheap and produces the better merge, so a "keep" verdict declines.
    orch = _wiring_orchestrator(_KeepEngine(), _BASE_W, _CUR_W, _REP_W)
    out = orch._try_true_side_portfolio(
        "f.c", "c", _CUR_W, _units(n=1, base=_BASE_W, cur=_CUR_W, rep=_REP_W),
        phase1_fast_path=True)
    assert out is None
    adj = [e for e in orch.journal.events
           if e[0] == "phase1_fast_path_adjudication"]
    assert adj and adj[0][1]["n_units"] == 1 and adj[0][1]["fires"] is False


def test_wholesale_few_units_superseded_fires():
    orch = _wiring_orchestrator(_SupersededEngine(), _BASE_W, _CUR_W, _REP_W)
    out = orch._try_true_side_portfolio(
        "f.c", "c", _CUR_W, _units(n=1, base=_BASE_W, cur=_CUR_W, rep=_REP_W),
        phase1_fast_path=True)
    assert out is not None and out[1] == _CUR_W


def test_wholesale_few_units_flag_off_preserves_old_behavior():
    # Without the adjudication flag the pre-regression behavior stands
    # (fire on numbers alone) — deployments that opt out keep af41b2e.
    orch = _wiring_orchestrator(_KeepEngine(), _BASE_W, _CUR_W, _REP_W)
    orch.config.future.enable_midband_subsumption_takeover = False
    out = orch._try_true_side_portfolio(
        "f.c", "c", _CUR_W, _units(n=1, base=_BASE_W, cur=_CUR_W, rep=_REP_W),
        phase1_fast_path=True)
    assert out is not None


def test_fastpath_declines_when_winner_fails_build():
    # redis-0010: the winner fails the per-file build for merge-relevant
    # reasons — decline the swap so the cascade runs, instead of accepting
    # and dying in oversized whole-file repair.
    orch = _wiring_orchestrator(_SupersededEngine(), _BASE_W, _CUR_W, _REP_W)
    orch._resolve_per_file_build = lambda path: "make f.o"
    orch._run_raw_test = lambda cmd: (False, "f.c:12:5: error: use of undeclared identifier 'x'")
    out = orch._try_true_side_portfolio(
        "f.c", "c", _CUR_W, _units(n=1, base=_BASE_W, cur=_CUR_W, rep=_REP_W),
        phase1_fast_path=True)
    assert out is None
    declined = [e for e in orch.journal.events
                if e[0] == "phase1_fast_path_declined"]
    assert declined and declined[0][1]["reason"] == "build"


def test_fastpath_build_environmental_failure_proceeds():
    # Sibling-file errors are infrastructure, not a verdict on the winner.
    orch = _wiring_orchestrator(_SupersededEngine(), _BASE_W, _CUR_W, _REP_W)
    orch._resolve_per_file_build = lambda path: "make f.o"
    orch._run_raw_test = lambda cmd: (False, "other.c:9:1: error: syntax error")
    out = orch._try_true_side_portfolio(
        "f.c", "c", _CUR_W, _units(n=1, base=_BASE_W, cur=_CUR_W, rep=_REP_W),
        phase1_fast_path=True)
    assert out is not None
# ---------------------------------------------------------------------------
# Wholesale winner floor — the sea-orm-0010/0024 + clap-0004 class
# ---------------------------------------------------------------------------

def test_floor_fires_on_degenerate_output():
    # sea-orm-0010 shape: the wholesale gates fired but every fast-path
    # route declined, and the cascade's output kept the loser's small edit
    # while dropping the dominant rewrite (winner preservation ~0.0).
    orch = _wiring_orchestrator(_KeepEngine(), _BASE_W, _CUR_W, _REP_W)
    units = _units(1, base=_BASE_W, cur=_CUR_W, rep=_REP_W)
    out = orch._wholesale_winner_floor("f.c", "c", units, buffer=_REP_W)
    assert out is not None
    (unit, cand), = out
    assert cand.resolved_text == _CUR_W  # the gate winner, not the buffer
    assert unit.unit_kind == "whole_file"
    assert cand.provenance == "deterministic_wholesale_floor_current"
    ev = [e for e in orch.journal.events if e[0] == "wholesale_winner_floor"]
    assert ev and ev[0][1]["winner"] == "current"


def test_floor_silent_on_weaving_output():
    # sea-orm-0009: the oracle weaves the loser's real features INTO the
    # winner; a weaving output preserves the winner's changes and the
    # floor must stay silent.
    orch = _wiring_orchestrator(_KeepEngine(), _BASE_W, _CUR_W, _REP_W)
    units = _units(1, base=_BASE_W, cur=_CUR_W, rep=_REP_W)
    assert orch._wholesale_winner_floor("f.c", "c", units, buffer=_CUR_W) is None


def test_floor_fires_without_buffer_on_escalation():
    # clap-0004: the cascade gave up with markers unresolved — buffer=None
    # means "about to escalate"; the gate winner is the only whole-file
    # answer left.
    orch = _wiring_orchestrator(_KeepEngine(), _BASE_W, _CUR_W, _REP_W)
    units = _units(1, base=_BASE_W, cur=_CUR_W, rep=_REP_W)
    out = orch._wholesale_winner_floor("f.c", "c", units, buffer=None)
    assert out is not None and out[0][1].resolved_text == _CUR_W


def test_floor_out_of_band_is_silent():
    # The 0004 mid-band shape: no wholesale rewrite, the cascade owns the
    # file and the floor has no opinion.
    orch = _wiring_orchestrator(_KeepEngine())
    units = _units(1)  # base=_BASE, cur=_CUR, rep=_REP — ratio 0.74
    assert orch._wholesale_winner_floor("f.c", "c", units, buffer=_REP) is None


def test_floor_respects_flag_off():
    orch = _wiring_orchestrator(_KeepEngine())
    orch.config.future.enable_wholesale_winner_floor = False
    units = _units(1, base=_BASE_W, cur=_CUR_W, rep=_REP_W)
    assert orch._wholesale_winner_floor("f.c", "c", units, buffer=_REP_W) is None


def test_floor_declines_unbalanced_winner():
    # Never floor to a side that can't even pass brace balance — the one
    # sanity check the fast path's full verification would have caught.
    orch = _wiring_orchestrator(_KeepEngine())
    bad_cur = _CUR_W + "\nint unbalanced(int x {;\n"
    units = _units(1, base=_BASE_W, cur=bad_cur, rep=_REP_W)
    assert orch._wholesale_winner_floor("f.c", "c", units, buffer=_REP_W) is None


def test_floor_uses_merge_stages_not_unit_sides():
    # The stage blobs are the pristine sides; unit side texts can go stale
    # after earlier writes. Give the unit sides junk and the stages truth.
    orch = _wiring_orchestrator(_KeepEngine(), _BASE_W, _CUR_W, _REP_W)
    units = _units(1, base="stale base\n", cur="stale cur\n", rep="stale rep\n")
    out = orch._wholesale_winner_floor("f.c", "c", units, buffer=_REP_W)
    assert out is not None and out[0][1].resolved_text == _CUR_W
# ---------------------------------------------------------------------------
# Non-code files and the brace sanity checks (sprint-17 WS1a)
# ---------------------------------------------------------------------------

def test_floor_skips_brace_check_for_markdown():
    # A markdown wholesale rewrite whose winner contains an unbalanced brace
    # (a code fence, a template placeholder) must still floor — braces in
    # prose have no structural meaning.
    md_cur = _CUR_W + "\nsee the `foo {` template above\n"
    orch = _wiring_orchestrator(_KeepEngine(), _BASE_W, md_cur, _REP_W)
    units = _units(1, base=_BASE_W, cur=md_cur, rep=_REP_W)
    for u in units:
        u.path = "CHANGELOG.md"
        u.language = "markdown"
    out = orch._wholesale_winner_floor(
        "CHANGELOG.md", "markdown", units, buffer=_REP_W)
    assert out is not None and out[0][1].resolved_text == md_cur


def test_portfolio_brace_check_skips_markdown():
    # Same exemption on the true-side portfolio's candidate sanity check:
    # an unbalanced brace in a markdown side must not disqualify it.
    md_cur = _CUR_W + "\nsee the `foo {` template above\n"
    orch = _wiring_orchestrator(_SupersededEngine(), _BASE_W, md_cur, _REP_W)
    units = _units(1, base=_BASE_W, cur=md_cur, rep=_REP_W)
    for u in units:
        u.path = "CHANGELOG.md"
        u.language = "markdown"
    out = orch._try_true_side_portfolio(
        "CHANGELOG.md", "markdown", md_cur, units, phase1_fast_path=True)
    assert out is not None and out[1] == md_cur
