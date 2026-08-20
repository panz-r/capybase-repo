#!/usr/bin/env python3
"""Harvest triage — one compact section per non-PASS case (S20.E9).

Reads the harvest results JSON (+ optional flights journals) and
categorizes every non-PASS case into the sprint-21 backlog:

  era-dead         — ESCALATE_TOOLCHAIN (un-passable; not a defect)
  environmental    — GATE_UNAVAILABLE / build-system / SAFE_SKIP noise
  model-capability — honest model limits (empty/refusal/near-miss)
  mechanism-gap    — a sprint-20 mechanism class recurring at scale
  investigate      — everything else (the actual work list)

Offline + sampled-safe: safe to run against partial results while the
harvest is in flight.

    python scripts/triage_harvest.py --results <results.json> [--limit N]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _categorize(r: dict) -> str:
    v = r.get("verdict") or "?"
    reason = (r.get("reason") or "") + " " + (r.get("terminal_reason") or "")
    rl = reason.lower()
    if v == "ESCALATE_TOOLCHAIN" or r.get("toolchain_dead"):
        return "era-dead"
    if v == "GATE_UNAVAILABLE":
        return "environmental"
    if v == "ESCALATE":
        if "safe" in rl and "stop" in rl:
            return "mechanism-gap"  # safety stops recurring = design question
        # environmental FIRST (defect review 2026-08-20: a couldn't-read-a-
        # file gate error was mis-bucketed as model-capability via a
        # coincidental 'timeout' in the reason text).
        if any(k in rl for k in (
                "build is not a directory", "cmake", "no conflict",
                "setup failed", "couldn't read", "could not read",
                "no such file")):
            return "environmental"
        if any(k in rl for k in (
                "empty", "needs_human", "model", "oversized", "timeout",
                "capability")):
            return "model-capability"
        return "investigate"
    if v in ("ORACLE_DIVERGENT", "NEAR_MATCH", "WORKING", "?"):
        return "investigate"
    return "investigate"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--limit", type=int, default=None,
                    help="cap sections printed per category")
    args = ap.parse_args()

    try:
        results = json.loads(Path(args.results).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"unreadable results: {exc}")
        return 1

    buckets: dict[str, list[dict]] = {}
    for r in results:
        if r.get("verdict") == "PASS":
            continue
        buckets.setdefault(_categorize(r), []).append(r)

    order = ["investigate", "mechanism-gap", "model-capability",
             "environmental", "era-dead"]
    print(f"== harvest triage: {sum(len(v) for v in buckets.values())} "
          f"non-PASS of {len(results)} ==")
    print("  " + " | ".join(f"{k}: {len(buckets.get(k, []))}" for k in order))
    for cat in order:
        items = buckets.get(cat, [])
        if not items:
            continue
        print(f"\n### {cat} ({len(items)})")
        for r in (items if args.limit is None else items[:args.limit]):
            sim = r.get("matches_oracle")
            print(f"- {r['id']} [{r.get('verdict')}] sim={sim}"
                  f" skel={r.get('skeleton_similarity')}")
            reason = str(r.get("reason") or "").strip()
            if reason:
                print(f"    {reason[:150]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
