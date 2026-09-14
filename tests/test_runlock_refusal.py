"""s27-86: a LIVE run lock must produce a clean `capybase: error:`
refusal — the s27-86 catch guarded the run_lock_guard constructor
(cannot raise) instead of __enter__, and the refusal escaped as a raw
traceback (stillborn fix #9 in the series).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from capybase.runlock import write_lock


def test_live_lock_refusal_is_clean(tmp_path: Path, monkeypatch,
                                    real_profile_loader):
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"],
                   capture_output=True)
    # forge a lock naming THIS live process with THIS repo:
    # live_lock() must classify it as live.
    from capybase.runlock import live_lock
    write_lock(repo)
    record = live_lock(repo)
    assert record is not None, "fixture lock must classify as live"
    assert record["pid"] == os.getpid()

    import io
    # The calibration gate resolves the provider + profile BEFORE the lock —
    # the twelfth-pass reviewer verified both pre-lock gates fire first.
    # A minimal valid provider + profile pair satisfies them.
    prov_dir = tmp_path / "xdg-config" / "capybase" / "providers"
    prov_dir.mkdir(parents=True, exist_ok=True)
    (prov_dir / "ci-lock-probe.json").write_text(json.dumps({
        "name": "ci-lock-probe", "model": "chat",
        "base_url": "http://127.0.0.1:9/v1", "api_key": "sk-test",
        "profile": "test",
    }))
    (tmp_path / "xdg-config" / "capybase" / "model_profile.test.json").write_text(
        json.dumps({
            "model": "chat",
            "max_tokens": 8192,
            "prompt": {"output_layout": "json_v6"},
        }))

    from capybase.cli import main
    err = io.StringIO()
    import contextlib
    err_cap = contextlib.redirect_stderr(err)
    with err_cap:
        rc = main(["--repo", str(repo), "--provider", "ci-lock-probe",
                   "rebase", "main"])
    assert rc == 2
    assert "another capybase run is live" in err.getvalue(), (
        err.getvalue())
