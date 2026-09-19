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
