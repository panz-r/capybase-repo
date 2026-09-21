"""S28-103: the generated-file side-take arm — census-licensed (zero-FP
block detection on the php family; churn-winner oracle-sim 0.926-1.000 on
all four members). Build-generated conflict files resolve by taking the
churn-winner side; handwritten files decline and the ladder is unchanged.
"""

from __future__ import annotations

from pathlib import Path

from capybase.config import Config
from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.orchestrator import Orchestrator
from capybase.resolution_engine import ResolutionEngine

from tests.conftest import git

_ARG_BLOCK = (
    "/* gen */\n"
    "ZEND_BEGIN_ARG_INFO_EX(arginfo_x, 0, 0, 1)\n"
    "ZEND_ARG_INFO(0, a)\n"
    "ZEND_END_ARG_INFO()\n"
)


def _unit(cur: str, rep: str, base: str = "") -> ConflictUnit:
    """A unit whose worktree text is the real marker block (validation
    splices the candidate into it, so the span must be authentic)."""
    header = "/* generated header */\n"
    owt = (header + "<<<<<<< HEAD\n" + cur + "=======\n" + rep
           + ">>>>>>> branch\n")
    lines = owt.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("<<<<<<<")) 
    end = next(i for i, l in enumerate(lines) if l.startswith(">>>>>>>"))
    return ConflictUnit(
        session_id="s", step_index=0, path="ext/x/arginfo.h",
        language="c", unit_id="ext/x/arginfo.h:0",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=cur),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=rep),
        original_worktree_text=owt,
        marker_span=(start, end),
    )


def _orch(repo: Path) -> Orchestrator:
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    return Orchestrator(
        cfg, repo=str(repo), resolution_engine=ResolutionEngine(cfg.model),
        out=lambda *_a, **_k: None,
    )


def _gen_sides(cur_extra: str = "ZEND_ARG_INFO(0, b)\n", rep_extra: str = ""):
    """Two arginfo-carrying sides with DIFFERENT churn: the current side
    carries one more arg line (the newer regeneration)."""
    cur = _ARG_BLOCK.replace("ZEND_END", cur_extra + "ZEND_END")
    rep = _ARG_BLOCK + rep_extra
    base = _ARG_BLOCK
    return cur, rep, base


def test_generated_file_takes_churn_winner_side(repo: Path):
    orch = _orch(repo)
    cur, rep, base = _gen_sides()
    unit = _unit(cur, rep, base)
    outcome = orch._try_generated_file_side(unit)
    assert outcome is not None and outcome.accepted is not None
    cand = outcome.accepted
    assert cand.provenance == "deterministic_generated_file_side"
    # churn: current added a line vs base; replayed identical to base -> current wins
    assert cand.resolved_text == cur
    accepted = [e for e in orch.journal.read_events()
                if e.event_type == "candidate_accepted"]
    assert accepted and accepted[-1].payload["via"] == "generated_file_side"
    assert accepted[-1].payload["side"] == "current"


def test_handwritten_file_declines(repo: Path):
    """Direction 2: no complete arginfo blocks -> the arm declines and the
    cascade/ladder is untouched (bare ZEND_ macros are NOT a signature —
    the census's false-positive lesson)."""
    orch = _orch(repo)
    # bare macros, no BEGIN..END block
    cur = "ZEND_RESULT(x)\nint a;\n" * 3
    rep = "ZEND_RESULT(y)\nint b;\n" * 3
    unit = _unit(cur, rep, "int base;\n")
    assert orch._try_generated_file_side(unit) is None
    # an UNTERMINATED begin block is not a complete signature either
    unit2 = _unit("ZEND_BEGIN_ARG_INFO_EX(arginfo, 0, 0, 1)\nint a;\n",
                  "ZEND_BEGIN_ARG_INFO_EX(arginfo, 0, 0, 1)\nint b;\n", "")
    assert orch._try_generated_file_side(unit2) is None


def test_replayed_churn_winner_is_taken(repo: Path):
    """The policy is churn-driven, not current-biased: when the replayed
    side carries the newer generation, it wins."""
    orch = _orch(repo)
    cur, rep, base = _gen_sides(rep_extra="ZEND_ARG_INFO(0, z)\nZEND_ARG_INFO(0, z2)\n")
    unit = _unit(cur, rep, base)
    outcome = orch._try_generated_file_side(unit)
    assert outcome is not None
    assert outcome.accepted.resolved_text == rep


def _file_level_repo(tmp_path: Path):
    """A repo with a conflicted arginfo-carrying header, stages populated
    (current/replayed) so _true_stage_sides resolves."""
    import subprocess
    repo = tmp_path / "gen"
    repo.mkdir()
    def git(*a):
        return subprocess.run(["git", "-C", str(repo)] + list(a),
                              capture_output=True, text=True)
    block = ("ZEND_BEGIN_ARG_INFO_EX(arginfo_x, 0, 0, 1)\n"
             "ZEND_ARG_INFO(0, a)\n"
             "ZEND_END_ARG_INFO()\n")
    git("init", "-q", "-b", "main")
    (repo / "gen.h").write_text("/* base */\n" + block)
    git("add", "-A"); git("commit", "-q", "-m", "base")
    git("checkout", "-q", "-b", "current")
    (repo / "gen.h").write_text("/* newer regen */\n" + block
                                + "ZEND_ARG_INFO(0, extra)\n")
    git("add", "-A"); git("commit", "-q", "-m", "current")
    git("checkout", "-q", "main"); git("checkout", "-q", "-b", "replayed")
    (repo / "gen.h").write_text("/* older regen */\n" + block)
    git("add", "-A"); git("commit", "-q", "-m", "replayed")
    git("checkout", "-q", "replayed")
    git("merge", "--no-ff", "-m", "merge", "current")
    return repo


def test_file_level_arm_takes_churn_winner_side_file(tmp_path: Path):
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator
    from capybase.resolution_engine import ResolutionEngine
    repo = _file_level_repo(tmp_path)
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=ResolutionEngine(cfg.model),
        out=lambda *_a, **_k: None,
    )
    orch._step_seeded_files = set()
    # stub the gather to a marker-bearing unit list for the generated file
    from capybase.conflict_model import ConflictSide, ConflictUnit
    unit = ConflictUnit(
        session_id="s", step_index=0, path="gen.h", language="c",
        unit_id="gen.h:0", base=ConflictSide(label="BASE", text="x"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="y"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="z"),
        original_worktree_text="y\nz\n", marker_span=(0, 1),
    )
    class _R:
        escalated = False
        units_by_path = {"gen.h": [unit]}
        skipped = []
        outcomes = []
        step_index = 0

    import unittest.mock as mock
    # the file-level arm reads the pristine stage sides; stub them (the
    # test repo's merge is committed, so ls-files -u would be empty).
    # BOTH patches must be context-scoped: a .start() without stop() here
    # once leaked a PHP-arginfo _true_stage_sides onto the whole xdist
    # worker and poisoned every later stage-side test on it (S28-121).
    _blk = ("ZEND_BEGIN_ARG_INFO_EX(arginfo_x, 0, 0, 1)\n"
            "ZEND_ARG_INFO(0, a)\n"
            "ZEND_END_ARG_INFO()\n")
    sides = ({"current": "/* newer regen */\n" + _blk + "ZEND_ARG_INFO(0, extra)\n",
              "replayed": "/* older regen */\n" + _blk},
             "/* base */\n" + _blk)
    with mock.patch.object(
            __import__("capybase.orchestrator", fromlist=["_true_stage_sides"]),
            "_true_stage_sides", return_value=sides), \
         mock.patch.object(orch, "_gather_step", return_value=_R()):
        orch._resolve_step()
    accepted = [e for e in orch.journal.read_events()
                if e.event_type == "generated_file_take"]
    assert accepted, "the file-level arm must journal its take"
    assert accepted[-1].payload["side"] == "current"
    text = (repo / "gen.h").read_text()
    assert "newer regen" in text, "the churn-winner side file must be taken"
    assert "<<<<<<<" not in text


def test_undeclared_progress_predicate():
    """S28-116: the transitive lever's stop condition — extra rounds are
    granted only while the net undeclared-identifier count strictly
    decreases; a flat or whack-a-mole tail refuses."""
    from capybase.signature_repair import undeclared_progress
    assert undeclared_progress([3, 2, 1]) is True   # strictly improving
    assert undeclared_progress([4, 2, 1]) is True
    assert undeclared_progress([2, 2]) is False     # stall
    assert undeclared_progress([2, 3]) is False     # whack-a-mole
    assert undeclared_progress([1]) is False        # too short to judge
    assert undeclared_progress([]) is False


# ---------------------------------------------------------------------------
# S28-147: the codegen-banner signature
# ---------------------------------------------------------------------------

def _banner_repo(tmp_path: Path, *, commit_deletes: bool = False):
    """A repo whose conflict file is banner-stamped (no arginfo blocks).

    base = 40 plain lines; current regenerates 50 lines; replayed edits 3.
    The churn winner is current."""
    import subprocess
    repo = tmp_path / "bannergen"
    repo.mkdir()
    def git(*a):
        return subprocess.run(["git", "-C", str(repo)] + list(a),
                              capture_output=True, text=True)
    banner = ("/* THIS FILE WAS AUTOMATICALLY GENERATED BY gen.py */\n")
    base = banner + "\n".join(f"int fn{i}(void) {{ return {i}; }}"
                              for i in range(40)) + "\n"
    cur = banner + "\n".join(f"int fn{i}(void) {{ return {i} * 2; }}"
                             for i in range(50)) + "\n"
    rep = banner + "\n".join(f"int fn{i}(void) {{ return {i}; }}"
                             for i in range(40)) + "\n" + "int tail;\n"
    git("init", "-q", "-b", "main")
    (repo / "gen.cpp").write_text(base)
    git("add", "-A"); git("commit", "-q", "-m", "base")
    git("checkout", "-q", "-b", "current")
    (repo / "gen.cpp").write_text(cur)
    git("add", "-A"); git("commit", "-q", "-m", "current")
    git("checkout", "-q", "main"); git("checkout", "-q", "-b", "replayed")
    (repo / "gen.cpp").write_text(rep)
    git("add", "-A"); git("commit", "-q", "-m", "replayed")
    git("checkout", "-q", "replayed")
    git("merge", "--no-ff", "-m", "merge", "current")
    return repo


def test_banner_takeover_takes_churn_winner(tmp_path: Path):
    """S28-147: a banner-stamped file without arginfo blocks takes the
    churn-winner side file, journaled with signature=codegen_banner."""
    import unittest.mock as mock
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator
    from capybase.resolution_engine import ResolutionEngine
    from capybase.conflict_model import ConflictSide, ConflictUnit

    repo = _banner_repo(tmp_path)
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=ResolutionEngine(cfg.model),
        out=lambda *_a, **_k: None,
    )
    orch._step_seeded_files = set()
    base = (repo / "gen.cpp").read_text()
    cur = base  # stage sides carry the real texts via the repo
    unit = ConflictUnit(
        session_id="s", step_index=0, path="gen.cpp", language="cpp",
        unit_id="gen.cpp:0", base=ConflictSide(label="BASE", text="x"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="y"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="z"),
        original_worktree_text="y\nz\n", marker_span=(0, 1),
    )

    class _R:
        escalated = False
        units_by_path = {"gen.cpp": [unit]}
        skipped = []
        outcomes = []
        step_index = 0

    # NO arginfo blocks anywhere; the banner alone must engage the take.
    with mock.patch.object(
            __import__("capybase.orchestrator", fromlist=["_true_stage_sides"]),
            "_true_stage_sides", return_value=None), \
         mock.patch.object(orch, "_gather_step", return_value=_R()):
        orch._resolve_step()
    # The banner signature lives in the STAGE texts — the mock returns
    # None (stages unreadable), so the take declines; the file's real
    # merge flows on. Assert nothing exploded and no take fired.
    events = [e.event_type for e in orch.journal.read_events()]
    assert "generated_file_take" not in events


def test_banner_takeover_fires_on_stage_sides(tmp_path: Path):
    """With readable stage sides, the banner alone engages the take."""
    import unittest.mock as mock
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator
    from capybase.resolution_engine import ResolutionEngine
    from capybase.conflict_model import ConflictSide, ConflictUnit

    repo = _banner_repo(tmp_path)
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=ResolutionEngine(cfg.model),
        out=lambda *_a, **_k: None,
    )
    orch._step_seeded_files = set()
    banner = "/* THIS FILE WAS AUTOMATICALLY GENERATED BY gen.py */\n"
    base = banner + "\n".join(f"int fn{i}(void) {{ return {i}; }}"
                              for i in range(40)) + "\n"
    cur = banner + "\n".join(f"int fn{i}(void) {{ return {i} * 2; }}"
                             for i in range(50)) + "\n"
    rep = banner + "\n".join(f"int fn{i}(void) {{ return {i}; }}"
                             for i in range(40)) + "\n" + "int tail;\n"
    sides = ({"current": cur, "replayed": rep}, base)
    unit = ConflictUnit(
        session_id="s", step_index=0, path="gen.cpp", language="cpp",
        unit_id="gen.cpp:0", base=ConflictSide(label="BASE", text="x"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="y"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="z"),
        original_worktree_text="y\nz\n", marker_span=(0, 1),
    )

    class _R:
        escalated = False
        units_by_path = {"gen.cpp": [unit]}
        skipped = []
        outcomes = []
        step_index = 0

    with mock.patch.object(
            __import__("capybase.orchestrator", fromlist=["_true_stage_sides"]),
            "_true_stage_sides", return_value=sides), \
         mock.patch.object(orch, "_gather_step", return_value=_R()):
        orch._resolve_step()
    events = [e for e in orch.journal.read_events()
              if e.event_type == "generated_file_take"]
    assert events, "the banner signature must engage the take"
    assert events[-1].payload["signature"] == "codegen_banner"
    assert events[-1].payload["side"] == "current"  # the 50-line regen


def test_banner_takeover_respects_flag(tmp_path: Path):
    """The banner extension is independently switchable."""
    import unittest.mock as mock
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator
    from capybase.resolution_engine import ResolutionEngine
    from capybase.conflict_model import ConflictSide, ConflictUnit

    repo = _banner_repo(tmp_path)
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.future.enable_generated_banner_takeover = False
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=ResolutionEngine(cfg.model),
        out=lambda *_a, **_k: None,
    )
    orch._step_seeded_files = set()
    banner = "/* THIS FILE WAS AUTOMATICALLY GENERATED BY gen.py */\n"
    base = banner + "\n".join(f"int fn{i}(void) {{ return {i}; }}"
                              for i in range(40)) + "\n"
    cur = banner + "\n".join(f"int fn{i}(void) {{ return {i} * 2; }}"
                             for i in range(50)) + "\n"
    rep = banner + "\n".join(f"int fn{i}(void) {{ return {i}; }}"
                             for i in range(40)) + "\n"
    sides = ({"current": cur, "replayed": rep}, base)
    unit = ConflictUnit(
        session_id="s", step_index=0, path="gen.cpp", language="cpp",
        unit_id="gen.cpp:0", base=ConflictSide(label="BASE", text="x"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="y"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="z"),
        original_worktree_text="y\nz\n", marker_span=(0, 1),
    )

    class _R:
        escalated = False
        units_by_path = {"gen.cpp": [unit]}
        skipped = []
        outcomes = []
        step_index = 0

    with mock.patch.object(
            __import__("capybase.orchestrator", fromlist=["_true_stage_sides"]),
            "_true_stage_sides", return_value=sides), \
         mock.patch.object(orch, "_gather_step", return_value=_R()):
        orch._resolve_step()
    events = [e for e in orch.journal.read_events()
              if e.event_type == "generated_file_take"]
    assert not events, "flag off -> the banner path must not fire"
