"""S28-105: the D10 oracle probe extended to the degraded-gate syntax path.

When a case's tree gate is degraded ("true" — S28-78's honest degrade for
families whose configure cannot run), the resolver's whole-file verdicts came
from the standalone syntax fallback. The oracle probe now runs the SAME
check: oracle-fails-too => oracle_builds=False => GATE_UNAVAILABLE for
sim >= 0.95 rows (the php arginfo band's honest classification).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "live_eval_d10_probe",
        Path(__file__).resolve().parent.parent / "scripts" / "live_eval_realworld.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["live_eval_d10_probe"] = mod
    spec.loader.exec_module(mod)  # type: ignore[arg-type]
    return mod


def _case(lang="c", path="ext/x/arginfo.h", oracle="#include \"no_such_header_zz.h\"\n"):
    return SimpleNamespace(
        language=lang, path=path, dataset="php-history",
        expected_resolved=oracle, id="php-history-9999",
    )


def test_degraded_gate_oracle_fails_syntax_probe(tmp_path, monkeypatch):
    mod = _load_module()
    (tmp_path / "ext" / "x").mkdir(parents=True)  # the probe writes the oracle there
    # force the degraded gate: no detected command, no dataset entry
    monkeypatch.setitem(mod._DETECTED_BUILD_CMD, "php-history-9999", "true")
    monkeypatch.setitem(mod.C_BUILD_COMMANDS, "php-history", "")
    case = _case()
    out = mod._oracle_builds(tmp_path, case, None)
    assert out is False, (
        "an oracle that fails the same standalone syntax check the resolver "
        "faced must probe False (GATE_UNAVAILABLE's precondition)")


def test_degraded_gate_oracle_passes_syntax_probe(tmp_path, monkeypatch):
    mod = _load_module()
    (tmp_path / "ext" / "x").mkdir(parents=True)
    monkeypatch.setitem(mod._DETECTED_BUILD_CMD, "php-history-9999", "true")
    monkeypatch.setitem(mod.C_BUILD_COMMANDS, "php-history", "")
    case = _case(oracle="int oracle_ok(int x) { return x + 1; }\n")
    out = mod._oracle_builds(tmp_path, case, None)
    assert out is True, (
        "an oracle that passes the syntax check is genuinely buildable "
        "content — no GATE_UNAVAILABLE")


def test_verdict_chain_gate_unavailable_on_degraded_probe():
    mod = _load_module()
    r = SimpleNamespace(
        escalated=True, marker_free=True, compiles=False,
        matches_oracle=0.96, toolchain_dead=False, oracle_builds=False,
    )
    assert mod._verdict_chain(r) == "GATE_UNAVAILABLE"
    # below the 0.95 bar: stays ESCALATE (honest — content genuinely far)
    r2 = SimpleNamespace(
        escalated=True, marker_free=True, compiles=False,
        matches_oracle=0.76, toolchain_dead=False, oracle_builds=False,
    )
    assert mod._verdict_chain(r2) == "ESCALATE"


def _mini_clone(tmp_path: Path, with_member_in_replayed: bool):
    import subprocess
    clone = tmp_path / "clone"
    clone.mkdir()
    f = clone / "src.hpp"

    def git(*a):
        return subprocess.run(["git", "-C", str(clone)] + list(a),
                              capture_output=True, text=True)
    # current edits src.hpp; replayed adds a SEPARATE file (carrying the
    # drifted member when licensed) — the merge then auto-resolves and
    # produces a real merge commit with two parents.
    git("init", "-q", "-b", "main")
    f.write_text("struct Api { int common(); }\n")
    git("add", "-A"); git("commit", "-q", "-m", "base")
    git("checkout", "-q", "-b", "current")
    f.write_text("struct Api { int current_only(); }\n")
    git("add", "-A"); git("commit", "-q", "-m", "current")
    git("checkout", "-q", "main"); git("checkout", "-q", "-b", "replayed")
    g2 = clone / "drifted.hpp"
    g2.write_text("int drifted(int);\n" if with_member_in_replayed
                  else "int unrelated(void);\n")
    git("add", "-A"); git("commit", "-q", "-m", "replayed")
    git("checkout", "-q", "replayed")
    git("merge", "--no-ff", "-m", "merge", "current")
    sha = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    return clone, sha


def test_api_drift_probe_detects_member_only_in_replayed(tmp_path):
    """S28-110 built: a member present only in the replayed tree is drift —
    the validated 2-grep probe returns the evidence string."""
    import sys as _sys
    clone, sha = _mini_clone(tmp_path, with_member_in_replayed=True)
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "ler_drift",
        Path(__file__).resolve().parent.parent / "scripts" / "live_eval_realworld.py")
    mod = importlib.util.module_from_spec(spec)
    _sys.modules["ler_drift"] = mod
    spec.loader.exec_module(mod)
    out = mod._api_drift_probe(
        clone=clone, merge_sha=sha, path="src.hpp",
        expected_current="struct Api { int current_only(); }\n",
        expected_replayed="struct Api { int replayed_only(); }\n",
        escalated_reason="error: 'drifted' was not declared in this scope")
    assert out is not None and "drifted" in out, (
        "the probe must detect the member present only in the replayed tree")
