#!/usr/bin/env python3
"""CLI harness: replay the recorded jury corpus through the enforcement router.

Reads the frozen flight artifacts (default
``/var/tmp/capybase-flights-python/``) and replays every jury activation through
:mod:`capybase.jury_enforce` + :mod:`capybase.jury_replay`, comparing the
reconstructed route distribution against the golden distribution:

    accept 12, comment_counterexample 6, human_review 4, code_reopen 0

Never re-runs the model — the recorded juror verdicts are the source of truth
and the chair + router are deterministic.

Usage::

    uv run python scripts/replay_jury.py [--flights DIR]
                                         [--enable-code-reopen]
                                         [--json]

Exit code 0 when the reconstructed routes match the golden distribution AND all
invariants hold; non-zero otherwise (a replay mismatch is a stop condition for
the canary).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running as a script (uv run python scripts/replay_jury.py) without an
# installed package: add src/ to the path.
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from capybase.jury_replay import (  # noqa: E402
    GOLDEN_ROUTES, replay_corpus, format_report,
)


DEFAULT_FLIGHTS = "/var/tmp/capybase-flights-python"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay the recorded jury corpus through the enforcement router.",
    )
    parser.add_argument(
        "--flights", default=DEFAULT_FLIGHTS,
        help=f"Flights root (default: {DEFAULT_FLIGHTS})",
    )
    parser.add_argument(
        "--enable-code-reopen", action="store_true",
        help="Enable autonomous code_reopen during replay (default: off, matching "
             "the canary). When off, a satisfied reopen becomes human_review.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit the report as JSON instead of markdown.",
    )
    args = parser.parse_args(argv)

    flights = Path(args.flights)
    if not flights.is_dir():
        print(f"error: flights root not found: {flights}", file=sys.stderr)
        return 2

    report = replay_corpus(flights, enable_code_reopen=args.enable_code_reopen)

    if args.json:
        out = {
            "sessions_replayed": report.sessions_replayed,
            "verdict_files_replayed": report.verdict_files_replayed,
            "claim_decisions_replayed": report.claim_decisions_replayed,
            "golden_route_counts": report.golden_route_counts,
            "reconstructed_route_counts": report.reconstructed_route_counts,
            "matches_golden": report.matches_golden,
            "verbatim_preserved": report.verbatim_preserved,
            "verbatim_byte_identical": report.verbatim_byte_identical,
            "fingerprint_violations": report.fingerprint_violations,
            "evidence_ref_violations": report.evidence_ref_violations,
            "idempotent": report.idempotent,
            "all_invariants_hold": report.all_invariants_hold,
            "per_claim_mismatches": [
                {"case_id": c.case_id, "claim_id": c.claim_id,
                 "recorded_route": c.recorded_route,
                 "reconstructed_route": c.reconstructed_route,
                 "reason": c.reason}
                for c in report.per_claim_mismatches
            ],
        }
        print(json.dumps(out, indent=2))
    else:
        print(format_report(report))

    # Exit code: 0 when golden matches AND all invariants hold.
    ok = report.matches_golden and report.all_invariants_hold
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
