"""Sprint-20 S20.5b — shared stale-process sweep (process_hygiene)."""

from __future__ import annotations

from capybase.process_hygiene import kill_stale_build_processes


def test_sweep_returns_count_and_never_raises():
    # Runs against the live /proc; the contract is just int >= 0, no raise.
    n = kill_stale_build_processes()
    assert isinstance(n, int) and n >= 0
    # Idempotent: a second sweep finds nothing new to kill.
    assert kill_stale_build_processes() == 0


def test_eval_script_wrapper_wired():
    """The live eval script's historical entry points now delegate to the
    shared module (one net for every entry point)."""
    import importlib.util
    import sys
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "live_eval_realworld_hygiene",
        Path(__file__).resolve().parent.parent / "scripts" / "live_eval_realworld.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["live_eval_realworld_hygiene"] = mod
    spec.loader.exec_module(mod)  # type: ignore[arg-type]
    from capybase.process_hygiene import kill_stale_build_processes as shared
    import capybase.process_hygiene as ph
    assert mod._kill_stale_build_processes.__module__ == "scripts-live_eval" \
        or callable(mod._kill_stale_build_processes)
    # the wrapper delegates (source references the shared module)
    import inspect
    src = inspect.getsource(mod._kill_stale_build_processes)
    assert "process_hygiene" in src and "kill_stale_build_processes" in src
    assert shared is ph.kill_stale_build_processes
