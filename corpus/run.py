#!/usr/bin/env python
"""The corpus suite's own runner (NO pytest — user directive).

Drives the corpus checks (marker parse, marker-free oracle, per-language
verifier verdicts) as a plain Python program with its own loop, its own
reporting, and its own exit contract. pytest is for unit tests that need
nothing external; the corpus suite fetches gigabytes and runs real
toolchains — it gets its own execution model, as intended.

Usage:
    .venv/bin/python corpus/run.py            # everything present
    .venv/bin/python corpus/run.py python     # only the Python subset
    .venv/bin/python corpus/run.py rust       # only the cargo-worktree subset
"""
from __future__ import annotations

import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from corpus.realworld_loader import load_realworld_cases  # noqa: E402


def _run_check(fn, case, *args) -> tuple[str, str]:
    """-> ("pass"|"skip"|"fail", message)."""
    from corpus.checks import SKIP
    try:
        r = fn(case, *args)
        return ("skip", "") if r is SKIP else ("pass", "")
    except AssertionError as exc:
        return "fail", str(exc)[:200]
    except Exception:  # noqa: BLE001 — corpus checks report, never crash the loop
        return "fail", traceback.format_exc(limit=2)[-300:]


def _run_build_pool(items, max_workers: int = 2):
    """Run (name, fn, case) toolchain checks in a bounded thread pool.

    DEF-2: real builds (worktree + configure + make / cargo, 600s timeouts)
    must not run unbounded — concurrent full builds risk OOM (the pytest-era
    serial_build cap existed for exactly this). Two workers balance wall
    time against memory; the checks' own Python is trivial under the GIL —
    the concurrency is in the compiler/cargo subprocesses. Concurrent
    ``git worktree add`` on one clone is safe (unique mkdtemp names, 8/8
    race-test on sqlite-history).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from corpus.checks import SKIP
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fn, case, Path("/tmp")): name
                   for name, fn, case in items}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                r = fut.result()
                results.append((name, ("skip", "") if r is SKIP
                                else ("pass", "")))
            except AssertionError as exc:
                results.append((name, ("fail", str(exc)[:200])))
            except Exception:  # noqa: BLE001
                results.append((name, ("fail",
                                       traceback.format_exc(limit=2)[-300:])))
    return results


def main() -> int:
    subset = sys.argv[1] if len(sys.argv) > 1 else "all"
    if subset == "scenarios":
        # scenario-only run: skip the realworld-case dispatch entirely
        cases = []
    else:
        cases = load_realworld_cases()
        if subset != "all":
            cases = [c for c in cases if c.language == subset]
    print(f"corpus: {len(cases)} cases ({subset})")
    if not cases and subset != "scenarios":
        print("no data present — fetch via scripts/fetch_mergeconflict_datasets.py (corpus/README.md)")
        return 0

    from corpus import checks
    failures: list[tuple[str, str]] = []
    ran = skipped = 0

    # DEF-2: toolchain checks (real builds in worktrees) are collected and
    # run at the end in a bounded pool; everything else runs inline (fast,
    # no subprocesses). Names must match checks.py's checks_for /
    # scenario_checks_for yields.
    build_names = {"build_verdict", "cargo_verdict", "tip_build", "tip_cargo"}
    build_items: list[tuple[str, object, object]] = []

    def record(status_msg: tuple[str, str], name: str):
        nonlocal ran, skipped
        status, msg = status_msg
        if status == "skip":
            skipped += 1
            return
        ran += 1
        if status == "fail":
            failures.append((name, msg))

    # Session cases (extracted-testdata/sessions — no clones needed)
    from corpus.session_loader import load_session_cases
    session_cases = load_session_cases()
    if subset in ("all", "python", "sessions"):
        sess_fns = [
            ("marker_parses", checks.check_session_marker_parses),
            ("sides_round_trip", checks.check_session_sides_round_trip),
            ("merge_marker_free", checks.check_session_resolution_marker_free),
            ("floor_engages", checks.check_session_python_floor_engages),
            ("placeholder_honest", checks.check_session_placeholder_flag_honest),
        ]
        print(f"corpus: {len(session_cases)} session cases")
        with tempfile.TemporaryDirectory(prefix="corpus-run-") as td:
            for case in session_cases:
                for name, fn in sess_fns:
                    record(_run_check(fn, case, Path(td)), f"{case.id}:{name}")

    # Rebase scenarios (mined multi-commit history; clones needed)
    if subset in ("all", "scenarios", "rust", "c"):
        try:
            from corpus.rebase_scenario_loader import load_rebase_scenarios
            scenarios = load_rebase_scenarios()
        except Exception:  # noqa: BLE001 — no mined data
            scenarios = []
        if scenarios:
            print(f"corpus: {len(scenarios)} rebase scenarios")
            for scenario in scenarios:
                for name, fn in checks.scenario_checks_for(scenario):
                    if name in build_names:
                        build_items.append((f"{scenario.id}:{name}", fn, scenario))
                    else:
                        record(_run_check(fn, scenario, Path("/tmp")),
                               f"{scenario.id}:{name}")

    with tempfile.TemporaryDirectory(prefix="corpus-run-") as td:
        for i, case in enumerate(cases, 1):
            for name, fn in checks.checks_for(case):
                if name in build_names:
                    build_items.append((f"{case.id}:{name}", fn, case))
                    continue
                record(_run_check(fn, case, Path(td)), f"{case.id}:{name}")
            if i % 200 == 0:
                print(f"  ... {i}/{len(cases)} ({len(failures)} failures so far)")

    if build_items:
        print(f"  build pool: {len(build_items)} toolchain checks (2 workers)")
        for name, (status, msg) in _run_build_pool(build_items):
            record((status, msg), name)

    print(f"\ncorpus: {ran} checks ran, {skipped} skipped, "
          f"{len(failures)} failures")
    for name, msg in failures[:20]:
        print(f"  FAIL {name}: {msg}")
    if len(failures) > 20:
        print(f"  ... and {len(failures) - 20} more")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
