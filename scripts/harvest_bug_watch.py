#!/usr/bin/env python3
"""Harvest bug-watch: flip audit against the sprint-22 reround baseline.

The bug signal during a harvest run is REGRESSION — cases that PASSed
(or WORKING) in the reround and now escalate. Honest-case failures were
already attributed; flips are either (a) resolver bugs in the new code,
(b) environment drift, or (c) sampling variance on borderline cases —
each needs eyes. Also surfaces infrastructure errors (timeouts, no-
result workers) that masquerade as case failures.

Usage:
    python scripts/harvest_bug_watch.py --results <harvest.json> \
        --baseline-dir docs/results/s22r2 [--verbose]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

# Verdicts that carried real value in the reround — losing one is a flip.
_GOOD = {"PASS", "WORKING"}


def _load_baseline(baseline_dir: str) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for f in sorted(Path(baseline_dir).glob("r2-*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            rows[r["id"]] = r
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--baseline-dir", default="docs/results/s22r2")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    harvest = json.loads(Path(args.results).read_text(encoding="utf-8"))
    cases = harvest if isinstance(harvest, list) else harvest.get("cases", [])
    baseline = _load_baseline(args.baseline_dir)

    flips: list[dict] = []
    infra_errors: list[dict] = []
    new_passes: list[dict] = []
    still_pending = 0

    h_ids = set()
    for c in cases:
        cid = c["id"]
        h_ids.add(cid)
        v = c.get("verdict", "")
        b = baseline.get(cid)
        bv = (b or {}).get("verdict", "?")
        if v in _GOOD:
            if bv not in _GOOD and b is not None:
                new_passes.append({"id": cid, "was": bv, "now": v})
            continue
        if "timeout" in (c.get("reason") or "") or \
                "worker produced no result" in (c.get("reason") or ""):
            infra_errors.append(
                {"id": cid, "verdict": v, "reason": (c.get("reason") or "")[:100]})
        if bv in _GOOD and b is not None:
            flips.append({
                "id": cid, "was": bv, "now": v,
                "reason": (c.get("reason") or "")[:160],
                "repeats": c.get("repeat_verdicts") or [],
            })

    done = len(cases)
    print(f"== harvest bug watch: {done} results vs "
          f"{len(baseline)} baseline rows ==")
    print(f"  flips (baseline PASS/WORKING → now not): {len(flips)}")
    for f in flips:
        print(f"    FLIP {f['id']}: {f['was']} -> {f['now']} "
              f"repeats={f['repeats']}")
        if args.verbose:
            print(f"      reason: {f['reason']}")
    print(f"  new good verdicts (baseline not-good → now PASS/WORKING): "
          f"{len(new_passes)}")
    print(f"  infrastructure errors (timeout / no-result): "
          f"{len(infra_errors)}")
    for e in infra_errors[:10]:
        print(f"    INFRA {e['id']}: {e['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
