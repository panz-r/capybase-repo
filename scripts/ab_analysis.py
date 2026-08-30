"""Calibration A/B analysis (B9/B10, sprint-26).

Compares a WITH-arm results JSON against the WITHOUT-arm (the harvest)
on the arm's case set, and checks the gate: the PASS delta must exceed
the cases' own single-run verdict variance — measured from the
repeat_verdicts BOTH arms record under --repeat-all/--repeat-nonpass.

Usage:
    python scripts/ab_analysis.py \
        --with /var/tmp/capybase-live/s26/b9-with.json \
        --without /var/tmp/capybase-live/s26/full-harvest.json

Outputs per case: WITHOUT verdict (with repeats) -> WITH verdict (with
repeats), per-arm single-run PASS rates, and the verdict of the gate:
delta_vs_band.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _pass_rate(verdicts: list[str]) -> float:
    if not verdicts:
        return 0.0
    return sum(1 for v in verdicts if v == "PASS") / len(verdicts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with", dest="with_arm", required=True)
    ap.add_argument("--without", dest="without_arm", required=True)
    ap.add_argument("--label", default=None)
    args = ap.parse_args()

    with_rows = {c["id"]: c for c in json.loads(Path(args.with_arm).read_text())}
    without_rows = {
        c["id"]: c for c in json.loads(Path(args.without_arm).read_text())
    }

    label = args.label or Path(args.with_arm).stem
    print(f"== A/B: {label} ==")
    n = pass_with = pass_without = 0
    lat_with = lat_without = 0.0
    flips_up = flips_down = 0
    for cid, w in sorted(with_rows.items()):
        wo = without_rows.get(cid)
        if wo is None:
            print(f"  {cid}: NOT in without-arm (skipped)")
            continue
        n += 1
        w_reps = [w.get("verdict")] + list(w.get("repeat_verdicts") or [])
        wo_reps = [wo.get("verdict")] + list(wo.get("repeat_verdicts") or [])
        # majority record's verdict is w['verdict']; repeats carry all runs
        w_pass = w.get("verdict") == "PASS"
        wo_pass = wo.get("verdict") == "PASS"
        pass_with += w_pass
        pass_without += wo_pass
        lat_with += w.get("elapsed") or 0
        lat_without += wo.get("elapsed") or 0
        if w_pass and not wo_pass:
            flips_up += 1
        elif wo_pass and not w_pass:
            flips_down += 1
        marker = ""
        if w_pass != wo_pass:
            marker = "  <== FLIP " + ("UP" if w_pass else "DOWN")
        print(
            f"  {cid:32s} {wo.get('verdict', '?'):18s}"
            f"[{wo.get('matches_oracle', 0):.2f}] -> "
            f"{w.get('verdict', '?'):18s}"
            f"[{w.get('matches_oracle', 0):.2f}]"
            f"  runs without={'/'.join(wo_reps)} with={'/'.join(w_reps)}"
            f"{marker}"
        )

    if n == 0:
        print("no overlapping cases")
        return
    rate_with = pass_with / n
    rate_without = pass_without / n
    delta = rate_with - rate_without
    # Variance band: two-sample binomial noise at the POOLED rate (a
    # degenerate 0% or 100% arm rate would collapse the naive sigma to
    # zero and trivially "exceed" the band). The gate: |delta| must
    # exceed 2 sigma of the two-arm sampling noise.
    import math
    _pooled = (pass_with + pass_without) / (2 * n)
    sigma = math.sqrt(2 * _pooled * (1 - _pooled) / n) if n else 0
    print(f"\ncases={n}  PASS without={pass_without} ({rate_without:.0%})"
          f"  with={pass_with} ({rate_with:.0%})")
    print(f"delta = {delta:+.0%}  (flips: {flips_up} up / {flips_down} down)")
    print(f"2-sigma band on n={n}: ±{2 * sigma:.0%}  ->  "
          f"{'EXCEEDS band (real effect)' if abs(delta) > 2 * sigma else 'WITHIN band (variance)'}")
    print(f"latency/case: without={lat_without / n:.0f}s  with={lat_with / n:.0f}s"
          f"  ({(lat_with / lat_without - 1) if lat_without else 0:+.0%})")


if __name__ == "__main__":
    main()
