#!/usr/bin/env python3
"""Golden-path few-shot extractor — S20.E11 SKELETON (prepare, don't wire).

Mines flight journals for the 4B model's own clean successes — verdict
PASS, LLM provenance (not deterministic), sim > 0.95 — and extracts the
(prompt, response) pair keyed by skeleton signature, for a sprint-21
DECISION on injecting them as in-context examples (the E7 gate: >= 30
clean pairs before integration is even considered). Nothing here feeds
the production prompt builder this sprint.

    python scripts/extract_golden_path.py --results <results.json> \
        --flights <flights-dir> [--min-sim 0.95] [--out <jsonl>]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--flights", required=True)
    ap.add_argument("--min-sim", type=float, default=0.95)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    try:
        results = json.loads(Path(args.results).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"unreadable results: {exc}")
        return 1
    wins = {r["id"] for r in results
            if r.get("verdict") == "PASS"
            and (r.get("matches_oracle") or 0.0) >= args.min_sim}

    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        "lvr_gp", Path(__file__).resolve().parent / "live_eval_realworld.py")
    _m = _ilu.module_from_spec(_spec)
    sys.modules["lvr_gp"] = _m  # dataclasses resolves types via sys.modules
    _spec.loader.exec_module(_m)  # type: ignore[arg-type]

    pairs = []
    for j in sorted(Path(args.flights).rglob("journal.jsonl")):
        case_id = j.parent.parent.name
        if case_id not in wins:
            continue
        prompt = response = None
        for ln in j.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(ln)
            except json.JSONDecodeError:
                continue
            p = d.get("payload") or {}
            if d.get("event_type") == "context_built" and prompt is None:
                prompt = str(p.get("prompt") or "") or None
            if d.get("event_type") == "candidate_accepted" and response is None:
                response = str(p.get("resolved_text") or "") or None
        if prompt and response:
            pairs.append({
                "case_id": case_id,
                "skeleton": " ".join(_m._skeleton_signature(response)),
                "prompt": prompt, "response": response,
            })

    print(f"golden-path pairs: {len(pairs)} "
          f"(PASS + sim>={args.min_sim}; LLM provenance filter TBD at "
          f"integration — deterministic provenance tagging lands with "
          f"the journal schema extension)")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            for p in pairs:
                f.write(json.dumps(p) + "\n")
        print(f"jsonl: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
