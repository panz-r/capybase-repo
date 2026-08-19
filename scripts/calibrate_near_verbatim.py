#!/usr/bin/env python3
"""Near-verbatim band calibration — sprint-19 P6 (Q4's second question).

Measure-only. The reviewer question: is there a principled "near-verbatim
band" (oracle ~= one side at token-jaccard >= 0.99) that deserves its own
calibrated path, or is it indistinguishable in-shape from genuine woven
merges that happen to be dominated?

Measured per corpus case (extracted-testdata/realworld/*.json):

1. The oracle's jaccard to each side; the band census at several cutoffs
   (>= 0.95 / >= 0.99 / == 1.0 verbatim), cross-tabulated against the
   churn regime (wholesale / mid / symmetric) to see whether churn
   numbers separate the band from the woven class.
2. Residual concentration for the >= 0.99-but-not-verbatim cases: the
   oracle-vs-side diff's hunk count and changed-line total. Concentrated
   (<= 2 hunks, <= 20 lines) = "take the side plus one thread"; scattered
   = a real weave that happens to be dominated.
3. Deletion-carveout band (P2 follow-up): cases where one side's
   base-relative churn is PURE DELETION (no added lines) — how often is
   the oracle then the OTHER side verbatim/near-verbatim? That is the
   corpus answer to the churn-aware preservation heuristic's premise.

No behavioral changes; prints a report and optionally writes JSON.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from capybase.merge_intent import full_file_context  # noqa: E402


def _token_jaccard(a: str, b: str) -> float:
    """The eval harness's similarity metric (live_eval_realworld._token_jaccard).

    Duplicated here so the calibration measures the SAME quantity the PASS
    verdict uses."""

    def toks(t: str) -> set[str]:
        return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+", t))

    ta, tb = toks(a), toks(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _side_churn_split(base: str, side: str) -> tuple[int, int]:
    """(added_lines, deleted_lines) of one side vs the base."""
    added = deleted = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, base.splitlines(), side.splitlines(),
            autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if tag != "delete":
            added += j2 - j1
        if tag != "insert":
            deleted += i2 - i1
    return added, deleted


def _diff_shape(a: str, b: str) -> tuple[int, int]:
    """(hunk_count, changed_lines) of the unified diff a→b."""
    hunks = changed = 0
    in_hunk = False
    for ln in difflib.unified_diff(
            a.splitlines(), b.splitlines(), lineterm="", n=0):
        if ln.startswith(("---", "+++", "@@", "--- ")):
            if ln.startswith("@@"):
                hunks += 1
                in_hunk = False
            continue
        if ln[:1] in ("+", "-"):
            changed += 1
            in_hunk = True
    return hunks, changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases-dir", default="extracted-testdata/realworld")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    rows = []
    for f in sorted(Path(args.cases_dir).glob("*.json")):
        try:
            case = json.load(open(f))
        except Exception:  # noqa: BLE001
            continue
        base, cur, rep = (case.get("base"), case.get("current"),
                          case.get("replayed"))
        oracle = case.get("expected_resolved")
        if not all(isinstance(x, str) and x for x in (base, cur, rep, oracle)):
            continue
        ctx = full_file_context(base, cur, rep)
        j_cur = _token_jaccard(oracle, cur)
        j_rep = _token_jaccard(oracle, rep)
        best_side = "current" if j_cur >= j_rep else "replayed"
        j_best = max(j_cur, j_rep)
        j_other = min(j_cur, j_rep)
        best_text = cur if best_side == "current" else rep
        # Residual shape oracle-vs-best-side (only meaningful near 1.0).
        hunks, changed = _diff_shape(best_text, oracle)
        # Deletion-purity of each side (whole-file granularity).
        cur_add, cur_del = _side_churn_split(base, cur)
        rep_add, rep_del = _side_churn_split(base, rep)
        rows.append({
            "id": case.get("id") or f.stem,
            "dataset": case.get("dataset"),
            "churn_ratio": round(ctx["churn_ratio"], 4),
            "j_best": round(j_best, 4),
            "j_other": round(j_other, 4),
            "best_side": best_side,
            "verbatim": oracle == best_text,
            "residual_hunks": hunks if not oracle == best_text else 0,
            "residual_changed_lines": (changed
                                       if not oracle == best_text else 0),
            "cur_pure_deletion": cur_add == 0 and cur_del > 0,
            "rep_pure_deletion": rep_add == 0 and rep_del > 0,
        })

    n = len(rows)
    if not n:
        print("no cases measured")
        return 1

    def pct(k: int) -> str:
        return f"{k}/{n} ({100.0 * k / n:.1f}%)"

    print(f"== near-verbatim band census ({n} cases) ==")
    for lo in (0.95, 0.99):
        band = [r for r in rows if r["j_best"] >= lo]
        verb = [r for r in band if r["verbatim"]]
        print(f"  j_best >= {lo}: {pct(len(band))}  "
              f"(of which verbatim: {len(verb)})")
    verb_all = [r for r in rows if r["verbatim"]]
    print(f"  verbatim (==): {pct(len(verb_all))}")

    print("\n== band x churn regime (does churn separate the band?) ==")
    for label, lo in ((">=0.95", 0.95), (">=0.99", 0.99)):
        band = [r for r in rows if r["j_best"] >= lo]
        wholesale = [r for r in band if r["churn_ratio"] >= 0.90]
        mid = [r for r in band if 0.55 <= r["churn_ratio"] < 0.90]
        sym = [r for r in band if r["churn_ratio"] < 0.55]
        print(f"  j_best {label}: wholesale {len(wholesale)}, "
              f"mid {len(mid)}, symmetric {len(sym)}")
    woven = [r for r in rows if r["j_best"] < 0.95 and r["j_other"] < 0.95]
    w_wholesale = [r for r in woven if r["churn_ratio"] >= 0.90]
    print(f"  woven class (both j < 0.95): {pct(len(woven))}; "
          f"of which wholesale-regime: {len(w_wholesale)}")

    near = [r for r in rows if r["j_best"] >= 0.99 and not r["verbatim"]]
    print(f"\n== residual concentration (j_best >= 0.99, not verbatim: "
          f"{len(near)}) ==")
    concentrated = [r for r in near
                    if r["residual_hunks"] <= 2
                    and r["residual_changed_lines"] <= 20]
    print(f"  concentrated (<=2 hunks, <=20 lines): {len(concentrated)}")
    for r in sorted(near, key=lambda r: -r["residual_changed_lines"])[:8]:
        print(f"    {r['id']}: j={r['j_best']:.3f} hunks={r['residual_hunks']} "
              f"changed={r['residual_changed_lines']} ratio={r['churn_ratio']}")

    print("\n== deletion-carveout band (P2 premise, whole-file proxy) ==")
    del_cases = [r for r in rows
                 if r["cur_pure_deletion"] or r["rep_pure_deletion"]]
    for r in del_cases:
        r["carveout_winner"] = ("replayed" if r["cur_pure_deletion"]
                                else "current")
        r["oracle_is_winner"] = (
            r["best_side"] == r["carveout_winner"] and r["j_best"] >= 0.99)
    won = [r for r in del_cases if r.get("oracle_is_winner")]
    lost = [r for r in del_cases if not r.get("oracle_is_winner")]
    print(f"  one side pure-deletion: {pct(len(del_cases))}")
    if del_cases:
        print(f"  oracle ~= OTHER side (>=0.99): {len(won)}/{len(del_cases)} "
              f"({100.0 * len(won) / len(del_cases):.1f}%)")
        print(f"  oracle NOT the other side: {len(lost)}")
        for r in lost[:8]:
            print(f"    {r['id']}: j_best={r['j_best']:.3f} "
                  f"best_side={r['best_side']} "
                  f"carveout_winner={r['carveout_winner']}")

    if args.json_out:
        json.dump(rows, open(args.json_out, "w"), indent=1)
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
