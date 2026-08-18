#!/usr/bin/env python3
"""Offline calibration for extending the whole-file fast path below 0.90.

The whole-file fast path (asymmetry takeover / wholesale winner floor) fires
when one side rewrote the file (churn_ratio >= 0.90 + dominance + staleness).
Sprint-18 WS2 asks whether the band can extend to [0.80, 0.90): in that band,
is the human oracle still one side verbatim, or a woven merge?

This needs no model endpoint: churn_ratio/winner are pure functions of the
case's three sides, and the oracle is the case's expected_resolved. Verdicts
are joined from existing live-eval result JSONs (majority-of-N where
available). Output: the mid-band census + the separation analysis for
lowering the threshold.

Usage:
  PYTHONPATH=src .venv/bin/python scripts/calibrate_midband_fastpath.py \
      --verdicts /tmp/capybase-live/s17/baseline-r3.json \
                 /tmp/capybase-live/s17/census/python.json \
      [--band-lo 0.75] [--threshold 0.80]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from capybase.merge_intent import full_file_context  # noqa: E402


def _token_jaccard(a: str, b: str) -> float:
    """The eval harness's similarity metric (live_eval_realworld._token_jaccard).

    Duplicated here so the calibration measures the SAME quantity the PASS
    verdict uses."""
    import re

    def toks(t: str) -> set[str]:
        return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+", t))

    ta, tb = toks(a), toks(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def load_verdicts(paths: list[str]) -> dict[str, dict]:
    """case_id -> latest result record (later files win)."""
    out: dict[str, dict] = {}
    for p in paths:
        try:
            data = json.load(open(p))
        except Exception as exc:  # noqa: BLE001
            print(f"  (skipping unreadable verdict file {p}: {exc})", file=sys.stderr)
            continue
        if isinstance(data, list):
            for rec in data:
                if isinstance(rec, dict) and rec.get("id"):
                    out[rec["id"]] = rec
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases-dir", default="extracted-testdata/realworld")
    ap.add_argument("--verdicts", action="append", default=[])
    ap.add_argument("--band-lo", type=float, default=0.75,
                    help="report cases with churn_ratio in [band-lo, 1.0]")
    ap.add_argument("--threshold", type=float, default=0.80,
                    help="candidate new fast-path lower bound")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    verdicts = load_verdicts(args.verdicts)
    rows = []
    for f in sorted(Path(args.cases_dir).glob("*.json")):
        try:
            case = json.load(open(f))
        except Exception:  # noqa: BLE001
            continue
        base, cur, rep = case.get("base"), case.get("current"), case.get("replayed")
        oracle = case.get("expected_resolved")
        if not all(isinstance(x, str) and x for x in (base, cur, rep, oracle)):
            continue
        ctx = full_file_context(base, cur, rep)
        ratio = ctx["churn_ratio"]
        if ratio < args.band_lo:
            continue
        # NB: ctx["asymmetry_side"] is None below 0.90 by construction — the
        # calibration must compute the higher-churn side itself, else every
        # mid-band row is silently skipped.
        winner = "current" if ctx["current_churn"] >= ctx["replayed_churn"] else "replayed"
        w_text = cur if winner == "current" else rep
        l_text = rep if winner == "current" else cur
        j_w = _token_jaccard(oracle, w_text)
        j_l = _token_jaccard(oracle, l_text)
        verdict_rec = verdicts.get(case["id"], {})
        rows.append({
            "id": case["id"],
            "dataset": case.get("dataset"),
            "ratio": round(ratio, 4),
            "winner": winner,
            "dominance_ok": ctx["dominant_churn"] >= 0.30 * max(ctx["base_lines"], 1),
            "both_changed": min(ctx["current_churn"], ctx["replayed_churn"]) >= 5,
            "oracle_jaccard_to_winner": round(j_w, 4),
            "oracle_jaccard_to_loser": round(j_l, 4),
            "verdict": verdict_rec.get("verdict", "?"),
            "matches_oracle": verdict_rec.get("matches_oracle"),
            # A fast path at this ratio would take the winner verbatim:
            # flippable when the oracle IS the winner; a woven oracle means
            # the fast path would BREAK a merge the cascade got right.
            "fastpath_would_pass": j_w >= 0.95,
            "fastpath_would_break": j_w < 0.80,
        })

    rows.sort(key=lambda r: r["ratio"])
    print(f"{'case':38} {'ratio':>6} {'win':>7} {'dom':>4} {'j_win':>6} {'j_lose':>6} "
          f"{'verdict':>12} {'flip':>5}")
    for r in rows:
        print(f"{r['id']:38} {r['ratio']:>6.3f} {r['winner']:>7} "
              f"{'y' if r['dominance_ok'] else 'n':>4} "
              f"{r['oracle_jaccard_to_winner']:>6.3f} {r['oracle_jaccard_to_loser']:>6.3f} "
              f"{r['verdict']:>12} {'y' if r['fastpath_would_pass'] else '':>5}")

    # Separation analysis at the candidate threshold.
    def sep(lo: float) -> dict:
        band = [r for r in rows if lo <= r["ratio"] < 0.90
                and r["dominance_ok"] and r["both_changed"]]
        flippable = [r for r in band if r["fastpath_would_pass"]
                     and r["verdict"] not in ("PASS", "?")]
        breakable = [r for r in band if r["verdict"] == "PASS"
                     and r["fastpath_would_break"]]
        return {"threshold": lo, "band_size": len(band),
                "flippable_nonpass": sorted(r["id"] for r in flippable),
                "pass_would_break": sorted(r["id"] for r in breakable)}

    print()
    for lo in (0.75, 0.80, 0.85):
        s = sep(lo)
        print(f"threshold {lo:.2f}: band={s['band_size']} "
              f"flippable_nonpass={len(s['flippable_nonpass'])} "
              f"pass_would_break={len(s['pass_would_break'])}")
        if s["flippable_nonpass"]:
            print(f"   flip: {', '.join(s['flippable_nonpass'])}")
        if s["pass_would_break"]:
            print(f"   BREAK: {', '.join(s['pass_would_break'])}")

    if args.json_out:
        json.dump(rows, open(args.json_out, "w"), indent=1)
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
