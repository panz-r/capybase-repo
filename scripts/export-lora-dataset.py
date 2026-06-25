#!/usr/bin/env python3
"""Export accepted resolutions as a LoRA fine-tuning dataset.

The journal stores every (prompt, response, candidate) triple on disk; the
experience store labels which were accepted. This script joins them: for each
accepted experience, it finds the matching prompt/response artifacts and emits
a JSONL record in the format axolotm/llama.cpp fine-tuning expects:

    {"instruction": "<prompt>", "output": "<json response>"}

Optionally filters to compiler-verified-only (syntax_passed=True) for the
highest-quality training signal. The dataset grows with every successful
capybase run — no manual labeling needed.

Usage:
    python scripts/export-lora-dataset.py [--repo REPO] [--out OUT.jsonl]
        [--verified-only] [--min-confidence 0.5]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=".", help="repo root (default: CWD)")
    ap.add_argument(
        "--store",
        default=".rebase-agent/memory/experiences.jsonl",
        help="experience store path (relative to --repo)",
    )
    ap.add_argument("--out", required=True, help="output JSONL path")
    ap.add_argument(
        "--verified-only",
        action="store_true",
        help="only include resolutions where syntax/AST validation passed",
    )
    ap.add_argument(
        "--min-confidence",
        type=float,
        default=0.0,
        help="minimum self_reported_confidence to include",
    )
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    store_path = Path(args.store)
    if not store_path.is_absolute():
        store_path = repo / store_path
    if not store_path.is_file():
        print(f"ERROR: experience store not found at {store_path}", file=sys.stderr)
        print("Run capybase with [memory].enabled=true to accumulate data first.", file=sys.stderr)
        return 1

    # Load experiences.
    experiences = []
    with open(store_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                experiences.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # Build (prompt, response) pairs by scanning session artifacts. The
    # experience record has session_id + unit_id; the journal's prompt/response
    # artifacts live under .rebase-agent/sessions/<id>/prompts|responses/.
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sessions_root = repo / ".rebase-agent" / "sessions"
    written = 0
    skipped = 0
    with open(out_path, "w", encoding="utf-8") as out_fh:
        for exp in experiences:
            if exp.get("outcome") != "accepted":
                skipped += 1
                continue
            feats = exp.get("validator_features", {})
            if args.verified_only and not feats.get("syntax_passed", False):
                skipped += 1
                continue
            conf = exp.get("risk_score")
            # risk_score is 0..1 where lower is safer; invert for "confidence".
            confidence = 1.0 - (conf or 0.0)
            if confidence < args.min_confidence:
                skipped += 1
                continue
            session_id = exp.get("session_id", "")
            unit_id = exp.get("unit_id", "")
            # Find prompt/response artifacts for this unit.
            safe_unit = unit_id.replace("/", "__").replace(":", "-")
            sess_dir = sessions_root / session_id
            prompt_text = _find_prompt(sess_dir, safe_unit)
            if prompt_text is None:
                skipped += 1
                continue
            # The response is the accepted resolution JSON. We reconstruct it
            # from the example if the raw artifact isn't found.
            response = _find_response(sess_dir, safe_unit)
            if response is None:
                ex = exp.get("example", {})
                response = json.dumps(
                    {"resolved_text": ex.get("resolved", ""), "explanation": "merged"}
                )
            record = {"instruction": prompt_text, "output": response}
            out_fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            written += 1

    print(f"Exported {written} training examples to {out_path} ({skipped} skipped)")
    return 0


def _find_prompt(sess_dir: Path, safe_unit: str) -> str | None:
    prompts_dir = sess_dir / "prompts"
    if not prompts_dir.is_dir():
        return None
    # Prompts are named <safe_unit>.attempt<N>.txt; take attempt 0.
    for p in sorted(prompts_dir.glob(f"{safe_unit}.attempt*.txt")):
        return p.read_text(encoding="utf-8")
    return None


def _find_response(sess_dir: Path, safe_unit: str) -> str | None:
    resp_dir = sess_dir / "responses"
    if not resp_dir.is_dir():
        return None
    for p in sorted(resp_dir.glob(f"{safe_unit}.attempt*.txt")):
        return p.read_text(encoding="utf-8")
    return None


if __name__ == "__main__":
    raise SystemExit(main())
