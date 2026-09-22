#!/usr/bin/env python3
"""Build-probe census — the S28-168 pass wall-time anatomy.

The ten slowest s28 PASS rows are ALL duckdb at 930-1086s and their
accepted mechanisms are deterministic — the time is not model latency.
The suspect is the validation-build cadence: verify_file's per-file
build x Phase-2 re-validation x the exhaustion endgame's probes, each a
cold or ccache-warm cmake run. This census reads the preserved flight
journals and answers, per PASS row:

1. How many builds ran, how many seconds did they cost, and what share
   of the case's wall clock is that?
2. How many builds repeat the SAME command within one case (the
   build-result memoization question — repeats with unchanged inputs
   are what a path+content-hash memo converts into lookups)?
3. The cap-burn pattern: probes that died at the build timeout cap and
   whether another full build followed anyway (the duckdb-0145 shape:
   two consecutive 300s probes + a third test-gate build).

Standalone + offline: runs against any results/flights pair.

    python scripts/build_probe_census.py --results <results.json> \
        --flights <flights-dir> [--verdicts PASS] [--dataset duckdb] \
        [--json-out <path>] [--top 15]
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path


def _load_results(path: str) -> list[dict]:
    rows = json.loads(Path(path).read_text())
    return rows if isinstance(rows, list) else rows.get("results", rows.get("rows", []))


def _find_journal(flights: Path, case_id: str, session_id: str) -> Path | None:
    for base in (flights, flights / "flights"):
        cand = base / case_id / session_id / "journal.jsonl"
        if cand.exists():
            return cand
    return None


def _parse_ts(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def _census_case(journal: Path) -> dict:
    probes: list[dict] = []
    side_probes: list[dict] = []
    test_builds: list[dict] = []
    degrades: list[str] = []
    endgame: Counter = Counter()
    validation_reused = 0
    tests_open: dict[str, list[float]] = {}
    for line in journal.read_text().splitlines():
        try:
            e = json.loads(line)
        except Exception:
            continue
        t = e.get("event_type")
        p = e.get("payload") or {}
        if t == "build_probe":
            probes.append(p)
        elif t == "whole_side_probe":
            side_probes.append(p)
        elif t == "build_state":
            degrades.append(str(p.get("state")))
        elif t == "tests_started":
            cmd = str(p.get("command") or "")
            if "--build" in cmd or "make" in cmd:
                tests_open.setdefault(str(p.get("label")), []).append(_parse_ts(e["timestamp"]))
        elif t == "tests_finished":
            starts = tests_open.get(str(p.get("label")))
            if starts:
                dur = _parse_ts(e["timestamp"]) - starts.pop(0)
                test_builds.append({"label": p.get("label"), "duration_s": dur,
                                    "passed": p.get("passed"), "rc": p.get("returncode")})
        elif t in ("wholesale_winner_floor", "side_collapse_probe",
                   "deletion_respect_swap_probe", "majority_side_rescue",
                   "f1_tier2_side_build_declined", "phase2_build_fallback_full"):
            endgame[t] += 1
        elif t == "candidate_validation_reused":
            validation_reused += 1
    same_cmd = Counter(str(p.get("cmd")) for p in probes)
    build_s = sum(float(p.get("duration_s") or 0) for p in probes) + \
        sum(float(p.get("duration_s") or 0) for p in side_probes) + \
        sum(b["duration_s"] for b in test_builds)
    return {
        "build_probes": len(probes),
        "probe_seconds": round(sum(float(p.get("duration_s") or 0) for p in probes), 1),
        "probe_timeouts": sum(1 for p in probes if p.get("outcome") == "timeout"),
        "probe_passes": sum(1 for p in probes if p.get("outcome") == "pass"),
        "probe_fails": sum(1 for p in probes if p.get("outcome") == "fail"),
        "side_probes": len(side_probes),
        "side_probe_seconds": round(sum(float(p.get("duration_s") or 0) for p in side_probes), 1),
        "test_builds": len(test_builds),
        "test_build_seconds": round(sum(b["duration_s"] for b in test_builds), 1),
        "build_seconds": round(build_s, 1),
        "degrades": Counter(degrades),
        "endgame_events": dict(endgame),
        "validation_reused": validation_reused,
        "same_cmd_max": max(same_cmd.values(), default=0),
        "distinct_cmds": len(same_cmd),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--results", required=True)
    ap.add_argument("--flights", required=True)
    ap.add_argument("--verdicts", default="PASS", help="comma list; 'all' for every row")
    ap.add_argument("--dataset", default=None, help="substring filter, e.g. duckdb")
    ap.add_argument("--top", type=int, default=15, help="rows in the printed table")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    flights = Path(args.flights)
    wanted = (None if args.verdicts == "all"
              else {v.strip() for v in args.verdicts.split(",")})
    rows = _load_results(args.results)
    out_rows: list[dict] = []
    missing = 0
    for r in rows:
        if wanted is not None and r.get("verdict") not in wanted:
            continue
        if args.dataset and args.dataset not in (r.get("dataset") or ""):
            continue
        cid, sid = r.get("id"), r.get("session_id")
        j = _find_journal(flights, cid, sid) if cid and sid else None
        if j is None:
            missing += 1
            continue
        c = _census_case(j)
        c.update(case_id=cid, verdict=r.get("verdict"), wall=round(r.get("elapsed") or 0.0, 1))
        c["build_share"] = round(c["build_seconds"] / c["wall"], 3) if c["wall"] else None
        out_rows.append(c)
    out_rows.sort(key=lambda c: -c["wall"])

    n = len(out_rows)
    if n:
        walls = [c["wall"] for c in out_rows]
        builds = [c["build_seconds"] for c in out_rows]
        shares = [c["build_share"] for c in out_rows if c["build_share"] is not None]
        print(f"population: {n} rows (journals missing: {missing})")
        print(f"wall s:      sum={sum(walls):.0f}  median={statistics.median(walls):.0f}  max={max(walls):.0f}")
        print(f"build s:     sum={sum(builds):.0f}  median={statistics.median(builds):.0f}  max={max(builds):.0f}"
              f"  share-of-wall median={statistics.median(shares):.0%}  max={max(shares):.0%}")
        caps_burned = sum(c["probe_timeouts"] for c in out_rows)
        memo = [c for c in out_rows if c["same_cmd_max"] >= 3]
        print(f"probe timeouts (cap burns): {caps_burned} across {sum(1 for c in out_rows if c['probe_timeouts'])} cases")
        print(f"same-cmd>=3 (memo candidates): {len(memo)} cases; "
              f"validation_reused events: {sum(c['validation_reused'] for c in out_rows)}")
        print()
        hdr = f"{'case':28s} {'wall':>6s} {'bld_s':>6s} {'shr':>4s} {'prb':>3s} {'to':>2s} {'side':>4s} {'tst':>4s} {'rep':>3s} endgame"
        print(hdr)
        for c in out_rows[: args.top]:
            eg = ",".join(f"{k.replace('_probe','').replace('wholesale_winner','floor')}x{v}"
                          for k, v in sorted(c["endgame_events"].items()))
            dg = f" degrade={list(c['degrades'])}" if c["degrades"] else ""
            print(f"{c['case_id']:28s} {c['wall']:6.0f} {c['build_seconds']:6.0f} "
                  f"{(c['build_share'] or 0):4.0%} {c['build_probes']:3d} {c['probe_timeouts']:2d} "
                  f"{c['side_probe_seconds']:4.0f} {c['test_build_seconds']:4.0f} {c['same_cmd_max']:3d} {eg}{dg}")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(out_rows, indent=2, default=dict))
        print(f"\nwritten: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
