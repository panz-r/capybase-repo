#!/usr/bin/env python3
"""Arm-accuracy census — the S28-172 side-pick accuracy table.

Joins each accepted candidate's arm token (from the flight journals'
candidate_accepted events) with the row's oracle similarity, producing
the per-arm accuracy table that arbitrates the side-pick convention:
true_side_stage / f1_tier1 / f1_tier2 x current|replayed, plus the
wholesale/takeover families. The sprint-28 finding: f1_tier2:replayed
averaged 0.752 vs its current twin 0.919 — the replayed preference's
measured cost (S28-160's coin-flip, with data).

INTERPRETATION CAVEAT (S28-172): row sim reflects the WHOLE file, not
the arm's unit — low-sim HIGH-preservation WORKING rows are acceptable
alternatives, not arm failures. Judge arms only alongside the
preservation fields (unit-level attribution is S28-172(a)).

Standalone + offline: runs against any results/flights pair.

    python scripts/arm_accuracy_census.py --results <results.json> \
        --flights <flights-dir> [--json-out <path>] [--min-sessions 5]
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

#: The arm token lives at the end of the accepted candidate_id. New arms
#: append here (the census degrades gracefully — unknown arms group as
#: themselves via the generic tail match).
_ARM_PATTERNS = re.compile(
    r":(true_side_stage:[a-z]+|f1_tier[12]:[a-z]+|sidepick-[cr]"
    r"|current_only_wf:sidepick-[cr]|source_[a-z_]+|whole_file[a-z_]*"
    r"|lockfile[a-z_]*|banner[a-z_]*|comment[a-z_]*)$")


def _load_results(path: str) -> dict:
    rows = json.loads(Path(path).read_text())
    if not isinstance(rows, list):
        rows = rows.get("results", rows.get("rows", []))
    return {r.get("id"): r for r in rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--results", required=True)
    ap.add_argument("--flights", required=True)
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--min-sessions", type=int, default=5)
    args = ap.parse_args()

    rows = _load_results(args.results)
    flights = Path(args.flights)
    arm_stats: dict = defaultdict(
        lambda: {"n": 0, "sim_sum": 0.0, "pass": 0, "good": 0, "bad": []})
    for j in flights.glob("flights/*/*/journal.jsonl"):
        m = re.search(r"flights/([^/]+)/", str(j))
        if not m:
            continue
        cid = m.group(1)
        r = rows.get(cid)
        if not r:
            continue
        sim = r.get("matches_oracle") or 0
        verdict = r.get("verdict")
        arms = set()
        for line in j.read_text().splitlines():
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("event_type") != "candidate_accepted":
                continue
            token = (e.get("payload") or {}).get("candidate_id") or ""
            m2 = _ARM_PATTERNS.search(token)
            if m2:
                arms.add(m2.group(1))
        for arm in arms:
            st = arm_stats[arm]
            st["n"] += 1
            st["sim_sum"] += sim
            st["pass"] += 1 if verdict == "PASS" else 0
            st["good"] += 1 if sim >= 0.95 else 0
            if sim < 0.80:
                st["bad"].append((cid, verdict, round(sim, 3)))

    hdr = (f"{'arm':34s} {'sess':>4s} {'avg_sim':>7s} {'pass':>5s} "
           f"{'>=0.95':>6s}  sub-0.80 cases (deduped)")
    print(hdr)
    out = []
    for arm, st in sorted(arm_stats.items(), key=lambda x: -x[1]["n"]):
        if st["n"] < args.min_sessions:
            continue
        deduped = sorted({(c, v, s) for c, v, s in st["bad"]})
        print(f"{arm:34s} {st['n']:4d} {st['sim_sum'] / st['n']:7.3f} "
              f"{st['pass']:5d} {st['good']:6d}  {deduped[:5]}")
        out.append({
            "arm": arm, "sessions": st["n"],
            "avg_sim": round(st["sim_sum"] / st["n"], 4),
            "pass": st["pass"], "good": st["good"],
            "sub_080_cases": deduped,
        })
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(out, indent=2))
        print(f"\nwritten: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
