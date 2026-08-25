#!/usr/bin/env python3
"""Evaluate F1 tier-2 adjudicator accuracy from specimen flights.

For every f1_tier2_adjudication journal event, compare the
adjudicator's choice against the oracle-parent proximity (the ground
truth from the archaeology). Reports: correct takeovers, incorrect
takeovers (would have produced a wrong PASS), and correct weave
declines.

    .venv/bin/python scripts/f1_tier2_eval.py <flights_dir> [<extracts_dir>]
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path


def token_jaccard(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    return len(sa & sb) / max(1, len(sa | sb))


def main() -> int:
    flights_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        "/var/tmp/capybase-live/s23/flights-bc")
    corpus_dir = Path(__file__).resolve().parent.parent / "extracted-testdata/realworld"

    events = []
    for f in glob.glob(str(flights_dir / "flights/*/*/journal.jsonl")):
        case_id = f.split("/flights/")[1].split("/")[0]
        for line in open(f, errors="replace"):
            if '"f1_tier2_adjudication"' not in line:
                continue
            e = json.loads(line)
            p = e.get("payload") or {}
            events.append({
                "case": case_id,
                "choice": p.get("choice", ""),
                "confidence": p.get("confidence", 0.0),
                "reason": p.get("reason", ""),
            })

    if not events:
        print("No f1_tier2_adjudication events found.")
        return 0

    print(f"F1 tier-2 adjudicator evaluation ({len(events)} events):\n")
    correct = wrong_pass = correct_weave = wrong_weave = 0
    for ev in events:
        case_file = corpus_dir / f"{ev['case']}.json"
        if not case_file.exists():
            continue
        d = json.loads(case_file.read_text())
        o = d["expected_resolved"]
        oc = token_jaccard(o, d["current"])
        orp = token_jaccard(o, d["replayed"])
        best_side = "current" if oc >= orp else "replayed"
        best_sim = max(oc, orp)
        is_side_choice = best_sim >= 0.90

        if ev["choice"] in ("current", "replayed"):
            if ev["choice"] == best_side and is_side_choice:
                correct += 1
                verdict = "✓ CORRECT (took the oracle-side)"
            elif not is_side_choice:
                wrong_pass += 1
                verdict = f"✗ WRONG-PASS (oracle is weave at {best_sim:.2f})"
            else:
                wrong_pass += 1
                verdict = f"✗ WRONG-SIDE (took {ev['choice']}, oracle is {best_side})"
        else:  # weave
            if not is_side_choice:
                correct_weave += 1
                verdict = "✓ CORRECT (declined on a true weave)"
            else:
                wrong_weave += 1
                verdict = f"✗ MISSED-SIDE (oracle is {best_side} at {best_sim:.2f})"

        print(f"  {ev['case']:26s} chose={ev['choice']:9s} conf={ev['confidence']:.2f}  {verdict}")
        if ev["reason"]:
            print(f"    reason: {ev['reason'][:120]}")

    total = correct + wrong_pass + correct_weave + wrong_weave
    print(f"\nSummary: {total} decisions")
    print(f"  correct takeovers:       {correct}")
    print(f"  wrong (false PASS):      {wrong_pass}")
    print(f"  correct weave declines:  {correct_weave}")
    print(f"  missed side-choice:      {wrong_weave}")
    if total:
        print(f"  accuracy: {(correct + correct_weave) / total:.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
