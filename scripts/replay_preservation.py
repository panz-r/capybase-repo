#!/usr/bin/env python3
"""Counterfactual preservation replay: evaluate the new change-accounting logic
over stored convergence cases WITHOUT model calls or compilation.

Loads each case's stored base/current/replayed + the candidate the model
produced (from the flight artifacts), runs the new
:mod:`capybase.change_accounting` classifier, and predicts whether the new
preservation heuristic would change the outcome.

Produces the aggregate table the external analysis recommended:

    case_id | old_outcome | old_risk_reason | candidate_equals |
    missing_additive | exclusive_choice | deferred_comment |
    new_preservation_result | predicted_outcome

This is the cheap way to measure the aggregate impact of the exclusive-PASS
fix before spending compute on a full re-run.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from capybase.change_accounting import (
    derive_missing_obligations, derive_deferred_comments,
)


FLIGHTS = Path("/var/tmp/capybase-flights-rust-shadow/flights")
RESULTS = Path("/tmp/capybase-live/rust-shadow-results.json")


def _load_json(p):
    with open(p) as f:
        return json.load(f)


def _glob_one(pattern, base):
    matches = sorted(base.glob(pattern))
    return matches[0] if matches else None


def main():
    results = _load_json(RESULTS)
    # Find all convergence cases that had preservation_heuristic in their risk reasons.
    rows = []
    for entry in results:
        if not (entry.get("escalated") and "convergence" in (entry.get("reason") or "")):
            continue
        case_id = entry["id"]
        session_dir = None
        for sd in (FLIGHTS / case_id).iterdir():
            if (sd / "journal.jsonl").exists():
                session_dir = sd
                break
        if not session_dir:
            continue
        # Check if preservation_heuristic fired.
        has_pres = False
        for line in (session_dir / "journal.jsonl").read_text().splitlines():
            ev = json.loads(line)
            if ev.get("event_type") == "risk_decision":
                if any("preservation_heuristic" in r for r in ev.get("payload", {}).get("reasons", [])):
                    has_pres = True
                    break
        if not has_pres:
            continue

        # Load the case data + the model's candidate.
        case_path = Path(f"extracted-testdata/realworld/{case_id}.json")
        if not case_path.exists():
            continue
        case = _load_json(case_path)

        # Find the model's first response (the candidate that triggered preservation).
        resp_dir = session_dir / "responses"
        if not resp_dir.is_dir():
            continue
        resps = sorted(resp_dir.glob("*.txt"))
        resolved_text = ""
        for rp in resps:
            try:
                d = _load_json(rp)
                rt = (d.get("resolved_text") or "").strip()
                if rt:
                    resolved_text = rt
                    break
            except Exception:
                continue
        if not resolved_text:
            continue

        # Determine which side was copied.
        cur = (case.get("current") or "").strip()
        rep = (case.get("replayed") or "").strip()
        copied_side = ""
        copied_text = ""
        if resolved_text == cur:
            copied_side = "current"
            copied_text = case.get("current") or ""
        elif resolved_text == rep:
            copied_side = "replayed"
            copied_text = case.get("replayed") or ""
        else:
            # The resolved_text is the HUNK replacement, not the whole file.
            # Try matching against the hunk interiors from marker_original.
            m = case.get("marker_original") or ""
            s = m.find("<<<<<<<")
            if s >= 0:
                mid = m.find("=======", s)
                e = m.find(">>>>>>>", mid) if mid >= 0 else -1
                if mid >= 0 and e >= 0:
                    cur_hunk = m[s + m[s:].find("\n") + 1:mid].strip()
                    rep_hunk = m[mid + m[mid:].find("\n") + 1:e].strip()
                    if resolved_text == cur_hunk:
                        copied_side = "current"
                        copied_text = cur_hunk
                    elif resolved_text == rep_hunk:
                        copied_side = "replayed"
                        copied_text = rep_hunk

        if not copied_side:
            rows.append({
                "case_id": case_id, "candidate_equals": "neither",
                "classification": "UNCLASSIFIED",
            })
            continue

        # Run the new change-accounting.
        base_text = case.get("base") or ""
        if copied_side == "current":
            other_cur, other_rep = case.get("current") or "", case.get("replayed") or ""
        else:
            other_cur, other_rep = case.get("replayed") or "", case.get("current") or ""

        missing = derive_missing_obligations(base_text, case.get("current") or "",
                                             case.get("replayed") or "", copied_text)
        deferred = derive_deferred_comments(base_text, case.get("current") or "",
                                            case.get("replayed") or "", copied_text)

        additive = [o for o in missing if not o.exclusive]
        exclusive = [o for o in missing if o.exclusive]

        if not additive and not exclusive:
            pres_result = "CLEAR"
            predicted = "PASS (no obligations)"
        elif not additive and exclusive:
            pres_result = "CHOICE_REQUIRED"
            predicted = "PASS (exclusive choice — auditable)"
        elif additive:
            pres_result = "REPAIR_REQUIRED"
            predicted = "RETRY (additive missing)"
        else:
            pres_result = "UNKNOWN"
            predicted = "ESCALATE"

        rows.append({
            "case_id": case_id,
            "candidate_equals": copied_side,
            "missing_additive": len(additive),
            "exclusive_choice": len(exclusive),
            "deferred_comment": len(deferred),
            "new_preservation_result": pres_result,
            "predicted_outcome": predicted,
        })

    # Print the table.
    print(f"{'case_id':28s} {'copied':8s} {'add':>4s} {'excl':>5s} {'defer':>6s} {'result':18s} {'predicted':35s}")
    print("-" * 115)
    by_result = Counter()
    by_predicted = Counter()
    for r in rows:
        cid = r["case_id"]
        cp = r.get("candidate_equals", "?")
        add = r.get("missing_additive", "?")
        excl = r.get("exclusive_choice", "?")
        defer = r.get("deferred_comment", "?")
        pres = r.get("new_preservation_result", r.get("classification", "?"))
        pred = r.get("predicted_outcome", "?")
        print(f"{cid:28s} {cp:8s} {str(add):>4s} {str(excl):>5s} {str(defer):>6s} {pres:18s} {pred:35s}")
        by_result[pres] += 1
        by_predicted[pred.split(" (")[0]] += 1

    print(f"\n=== Aggregate ({len(rows)} convergence cases) ===")
    print("By preservation result:")
    for k, v in by_result.most_common():
        print(f"  {k:20s} {v}")
    print("By predicted outcome:")
    for k, v in by_predicted.most_common():
        print(f"  {k:20s} {v}")


if __name__ == "__main__":
    main()
