#!/usr/bin/env python3
"""Era sweep — E2's instrument (S20.E2, built pre-harvest 2026-08-20).

For every ESCALATE_TOOLCHAIN case in a results set:
  1. prints the probe evidence (per-side rc + error signatures) for
     human inspection — era-artifact vs content-defect attribution;
  2. ENFORCES THE INVARIANT: a case that PASSED in any prior baseline
     must never classify era-dead. A flip means a probe bug — the
     harvest's era-adjusted numbers are blocked until investigated
     (pre-registered in docs/sprint21-decision-template.md §E).

    python scripts/era_sweep.py --results <results.json> \
        --baseline <b1.json> [<b2.json> ...]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load(path: str) -> list[dict]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--baseline", action="append", default=[])
    args = ap.parse_args()

    results = _load(args.results)
    prior_pass: dict[str, str] = {}
    for b in args.baseline:
        for r in _load(b):
            if r.get("verdict") == "PASS":
                prior_pass[r.get("id")] = Path(b).name

    dead = [r for r in results if r.get("toolchain_dead")]
    print(f"== era sweep: {len(dead)} toolchain-dead of {len(results)} ==")
    violations = []
    for r in dead:
        probe = r.get("toolchain_probe") or {}
        print(f"\n{r['id']} [{r.get('verdict')}] gate={probe.get('gate')}")
        for side in ("current", "replayed", "oracle"):
            p = (probe.get("probes") or {}).get(side) or {}
            sig = p.get("sig") or []
            print(f"  {side:9s} rc={p.get('rc')} sig={sig[:3]}")
        if r["id"] in prior_pass:
            violations.append((r["id"], prior_pass[r["id"]]))
            print(f"  !! INVARIANT VIOLATION: PASSED in baseline "
                  f"{prior_pass[r['id']]} — investigate as a probe bug")

    if violations:
        print(f"\nERA-ADJUSTED NUMBERS BLOCKED: {len(violations)} "
              f"prior-PASS flips require investigation (template §E)")
        return 2
    print("\ninvariant holds: no prior-PASS case classified era-dead")
    return 0


if __name__ == "__main__":
    sys.exit(main())
