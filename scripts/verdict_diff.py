#!/usr/bin/env python3
"""Verdict diff — regression audit between eval result sets (S20.E1).

Compares a NEW results JSON (the harvest) against one or more OLD
baselines, case by case, flagging every verdict flip. The audit target
is PASS→non-PASS (regressions); the reverse direction (improvements) is
reported for context. Later baselines win for a case appearing in
several (the most recent verdict is the honest baseline).

    python scripts/verdict_diff.py --new <results.json> \
        --old <baseline1.json> [<baseline2.json> ...] [--json-out <path>]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_VERDICT_ORDER = {
    "PASS": 0, "WORKING": 1, "NEAR_MATCH": 2,
    "ORACLE_DIVERGENT": 3, "GATE_UNAVAILABLE": 4,
    "ESCALATE": 5, "ESCALATE_TOOLCHAIN": 6, "?": 7,
}


def _load(path: str) -> dict[str, dict]:
    try:
        rs = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {r.get("id"): r for r in rs if r.get("id")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--new", required=True)
    ap.add_argument("--old", action="append", required=True)
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    new = _load(args.new)
    if not new:
        print(f"no results at {args.new}")
        return 1

    # Later baselines override earlier ones per case.
    baseline: dict[str, dict] = {}
    baselines_used: dict[str, str] = {}
    for old_path in args.old:
        for cid, rec in _load(old_path).items():
            baseline[cid] = rec
            baselines_used[cid] = Path(old_path).name

    overlaps = sorted(set(new) & set(baseline))
    # "?" baselines are non-comparable: journal-only/calibration runs that
    # never produced a verdict (257 of the s18 midband set). Counted, not
    # diffed — comparing against "?" would drown the audit in noise.
    comparable = [cid for cid in overlaps
                  if (baseline[cid].get("verdict") or "?") != "?"]
    no_baseline = len(overlaps) - len(comparable)
    regressions, improvements, same, changed_other = [], [], 0, []
    for cid in comparable:
        nv = new[cid].get("verdict") or "?"
        ov = baseline[cid].get("verdict") or "?"
        if nv == ov:
            same += 1
            continue
        no, oo = _VERDICT_ORDER.get(nv, 9), _VERDICT_ORDER.get(ov, 9)
        entry = {
            "id": cid, "old": ov, "new": nv,
            "baseline_from": baselines_used[cid],
            "old_reason": str(baseline[cid].get("reason") or "")[:110],
            "new_reason": str(new[cid].get("reason") or "")[:110],
            "new_sim": new[cid].get("matches_oracle"),
        }
        if ov == "PASS" and nv != "PASS":
            regressions.append(entry)
        elif nv == "PASS" and ov != "PASS":
            improvements.append(entry)
        else:
            changed_other.append(entry)

    print(f"== verdict diff: {len(overlaps)} overlapping cases "
          f"(comparable: {len(comparable)}, no-baseline '?': {no_baseline}; "
          f"new={len(new)}, baseline={len(baseline)}) ==")
    print(f"  unchanged: {same}")
    print(f"  REGRESSIONS (PASS->non-PASS): {len(regressions)}")
    for r in regressions:
        print(f"    {r['id']}: {r['old']} -> {r['new']} "
              f"[{r['baseline_from']}] sim={r['new_sim']}")
        print(f"      reason: {r['new_reason']}")
    print(f"  improvements (non-PASS->PASS): {len(improvements)}")
    for r in improvements:
        print(f"    {r['id']}: {r['old']} -> {r['new']} "
              f"[{r['baseline_from']}] sim={r['new_sim']}")
    print(f"  other flips: {len(changed_other)}")
    for r in changed_other[:15]:
        print(f"    {r['id']}: {r['old']} -> {r['new']} "
              f"[{r['baseline_from']}]")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps({
            "overlaps": len(overlaps), "unchanged": same,
            "regressions": regressions, "improvements": improvements,
            "other_flips": changed_other,
        }, indent=1), encoding="utf-8")
        print(f"json: {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
