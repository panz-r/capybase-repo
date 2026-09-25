"""Audit model-request ordering against deterministic-first evidence.

S28-249 (audits #3-#9' successor, re-run against the six-arm fleet):
for every model draw (candidate_generated) in the given flight
journals, check that at least one deterministic-resolution or gate
event precedes it for the same path. Violations are requests the
deterministic layer never got a chance to answer. Also reports the
reuse split (deterministic exact_reuse_skipped saves vs
candidate_validation_reused draw waste, the S28-247.2 census) and
per-case draw concentration (the soft-premature populations: draws
legal per-unit but spent where a deterministic pre-screen/closer
should have preempted them).

Usage:
    python3 scripts/audit_request_ordering.py --flights <flight-dir>
    python3 scripts/audit_request_ordering.py --flights <dir> --json-out out.json

Read-only over journals; makes no requests.
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

# Events that count as deterministic evidence for a path: a
# deterministic resolution attempt, an accepted structural/portfolio
# candidate, a gate probe or verdict, or a recorded attempt outcome.
DET_EVENTS = frozenset({
    "structurally_resolved",
    "candidate_accepted",
    "exact_reuse_skipped",
    "source_portfolio_accepted",
    "structural_draft_seeded",
    "build_probe",
    "file_validated",
    "whole_side_probe",
    "whole_file_portfolio_gate",
    "midband_subsumption_gate",
    "asymmetry_takeover_gate",
    "combination_declined",
    "f1_side_verify_failed",
    "resolution_attempt",
})


def iter_sessions(flights_dir):
    for case in sorted(os.listdir(flights_dir)):
        case_dir = os.path.join(flights_dir, case)
        if not os.path.isdir(case_dir):
            continue
        for sid in sorted(os.listdir(case_dir)):
            journal = os.path.join(case_dir, sid, "journal.jsonl")
            if os.path.exists(journal):
                yield case, sid, journal


def audit_session(journal):
    """Return (violations, draws, reuse) for one journal."""
    events = []
    with open(journal) as fh:
        for line in fh:
            line = line.strip()
            if line:
                events.append(json.loads(line))

    det_paths = set()
    violations = []
    draws = 0
    reuse = Counter()
    per_case_draws = 0
    for e in events:
        et = e.get("event_type", "")
        payload = e.get("payload") or {}
        if "reuse" in et:
            reuse[et] += 1
        if et in DET_EVENTS:
            det_paths.add(e.get("path") or payload.get("path") or "__global__")
        elif et == "candidate_generated":
            draws += 1
            per_case_draws += 1
            path = e.get("path") or payload.get("path")
            if (path or "__global__") not in det_paths:
                violations.append({
                    "seq": e.get("seq"),
                    "path": path,
                    "unit_id": e.get("unit_id") or payload.get("unit_id"),
                })
    return violations, draws, reuse


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--flights", required=True,
                    help="flight root (contains <case>/<session>/journal.jsonl)")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    total_violations = []
    draws_by_case = Counter()
    reuse_total = Counter()
    n_sessions = 0
    for case, sid, journal in iter_sessions(args.flights):
        n_sessions += 1
        violations, draws, reuse = audit_session(journal)
        draws_by_case[case] += draws
        reuse_total.update(reuse)
        for v in violations:
            total_violations.append({"case": case, "session": sid, **v})

    report = {
        "flights": args.flights,
        "sessions": n_sessions,
        "draws_total": sum(draws_by_case.values()),
        "ordering_violations": total_violations,
        "reuse_saves_exact_reuse_skipped": reuse_total.get(
            "exact_reuse_skipped", 0),
        "reuse_waste_candidate_validation_reused": reuse_total.get(
            "candidate_validation_reused", 0),
        "draws_by_case": dict(sorted(draws_by_case.items(),
                                     key=lambda kv: -kv[1])),
    }

    print(f"== request-ordering audit: {n_sessions} sessions, "
          f"{report['draws_total']} draws ==")
    print(f"  ordering violations: {len(total_violations)}")
    for v in total_violations[:10]:
        print(f"    {v['case']}/{v['session']} seq={v['seq']} path={v['path']}")
    print(f"  deterministic reuse saves: "
          f"{report['reuse_saves_exact_reuse_skipped']}")
    print(f"  draw-waste reused validations: "
          f"{report['reuse_waste_candidate_validation_reused']}")
    print("  draws by case:")
    for case, n in report["draws_by_case"].items():
        print(f"    {case:28s} {n}")

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(report, fh, indent=1)
        print(f"  json -> {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
