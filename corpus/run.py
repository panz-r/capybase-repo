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


def main() -> int:
    subset = sys.argv[1] if len(sys.argv) > 1 else "all"
    cases = load_realworld_cases()
    if subset != "all":
        cases = [c for c in cases if c.language == subset]
    print(f"corpus: {len(cases)} cases ({subset})")
    if not cases:
        print("no data present — fetch via scripts/fetch_mergeconflict_datasets.py (corpus/README.md)")
        return 0

    from corpus import checks
    failures: list[tuple[str, str]] = []
    ran = skipped = 0

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
                    status, msg = _run_check(fn, case, Path(td))
                    if status == "skip":
                        skipped += 1
                        continue
                    ran += 1
                    if status == "fail":
                        failures.append((f"{case.id}:{name}", msg))

    with tempfile.TemporaryDirectory(prefix="corpus-run-") as td:
        for i, case in enumerate(cases, 1):
            for name, fn in checks.checks_for(case):
                status, msg = _run_check(fn, case, Path(td))
                if status == "skip":
                    skipped += 1
                    continue
                ran += 1
                if status == "fail":
                    failures.append((f"{case.id}:{name}", msg))
            if i % 200 == 0:
                print(f"  ... {i}/{len(cases)} ({len(failures)} failures so far)")
    print(f"\ncorpus: {ran} checks ran, {skipped} skipped, "
          f"{len(failures)} failures")
    for name, msg in failures[:20]:
        print(f"  FAIL {name}: {msg}")
    if len(failures) > 20:
        print(f"  ... and {len(failures) - 20} more")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
