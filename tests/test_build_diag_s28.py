"""S28-225 — the harness build telemetry (the S28-217 follow-up).

The fmt-0003 slices contradicted each other (one slice's runner build
PASS on the tree, the next slice's 0.6s FAIL) and the rows carried no
OUTPUT to attribute it. The last failing build's output head now rides
the row's harness_builds entry.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "live_eval_build_diag_s28",
        Path(__file__).resolve().parent.parent / "scripts" / "live_eval_realworld.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["live_eval_build_diag_s28"] = mod
    spec.loader.exec_module(mod)  # type: ignore[arg-type]
    return mod


def test_failing_build_leaves_a_diagnostic(monkeypatch, tmp_path):
    mod = _load_module()
    mod._LAST_C_BUILD_DIAG.pop("case-d", None)
    mod._DETECTED_BUILD_CMD["case-d"] = "make"

    def _fake_run(cmd, cwd, timeout, env=None):
        return subprocess.CompletedProcess(
            cmd, 2, stdout="", stderr="test/chrono-test.cc:304:6: error: redefinition")

    monkeypatch.setattr(mod, "_run_shell_tree", _fake_run)
    case = SimpleNamespace(id="case-d", dataset="d",
                           path="test/chrono-test.cc")
    assert mod._c_builds(tmp_path, case) is False
    diag = mod._LAST_C_BUILD_DIAG.get("case-d", "")
    assert "redefinition" in diag
    mod._DETECTED_BUILD_CMD.pop("case-d", None)
    mod._LAST_C_BUILD_DIAG.pop("case-d", None)


def test_passing_build_records_no_diagnostic(monkeypatch, tmp_path):
    mod = _load_module()
    mod._LAST_C_BUILD_DIAG.pop("case-p", None)
    mod._DETECTED_BUILD_CMD["case-p"] = "make"

    def _fake_run(cmd, cwd, timeout, env=None):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(mod, "_run_shell_tree", _fake_run)
    case = SimpleNamespace(id="case-p", dataset="d", path="a.cpp")
    assert mod._c_builds(tmp_path, case) is True
    assert "case-p" not in mod._LAST_C_BUILD_DIAG
    mod._DETECTED_BUILD_CMD.pop("case-p", None)
