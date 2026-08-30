"""Build a docs/results/<round>/ extract set from a harvest results JSON.

Reproduces the s22r2 extract format (per-language JSONL with the
recountable per-case fields) so the README row's numbers recompute from
committed artifacts, plus a meta.json skeleton the caller completes with
the round's mechanism/state summary.

Usage:
    python scripts/make_results_round.py \
        --results /var/tmp/capybase-live/s26/full-harvest.json \
        --out docs/results/s26 --round s26

The s22r2 rows carry: id, language, dataset, verdict, terminal_reason,
matches_oracle, escalated, elapsed, repeat_verdicts, reason — plus the
era/toolchain fields the flip audit reads (toolchain_dead).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

_FIELDS = (
    "id", "language", "dataset", "verdict", "terminal_reason",
    "matches_oracle", "escalated", "elapsed", "repeat_verdicts", "reason",
    "toolchain_dead",
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--round", required=True, help="round name, e.g. s26")
    args = ap.parse_args()

    records = json.loads(Path(args.results).read_text())
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    by_lang: dict[str, list] = {}
    for rec in records:
        row = {k: rec.get(k) for k in _FIELDS}
        by_lang.setdefault(rec.get("language") or "?", []).append(row)

    for lang, rows in sorted(by_lang.items()):
        path = out_dir / f"{args.round}-{lang}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        print(f"{path}: {len(rows)} rows")

    # README-table recount from the extracts themselves (single source).
    # SAFE_SKIP (git resolved cleanly on replay) leaves the denominator,
    # matching the README convention (676 loaded − 16 skips = 660).
    total = passes = working = era = 0
    for lang, rows in by_lang.items():
        for row in rows:
            if row["verdict"] == "SAFE_SKIP":
                continue
            total += 1
            if row["verdict"] == "PASS":
                passes += 1
            elif row["verdict"] == "WORKING":
                working += 1
            if row.get("toolchain_dead"):
                era += 1
    denom_adj = total - era
    meta = {
        "round": args.round,
        "source": args.results,
        "recount": {
            "total": total,
            "pass": passes,
            "working": working,
            "era_dead": era,
            "pass_pct": round(100 * passes / total, 1) if total else 0,
            "adj_pct": round(100 * passes / denom_adj, 1) if denom_adj else 0,
            "pw_adj_pct": round(
                100 * (passes + working) / denom_adj, 1) if denom_adj else 0,
        },
        # caller completes: mechanism_commit, state, command_template,
        # ran, verification, outcome_summary
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps(meta["recount"], indent=1))


if __name__ == "__main__":
    main()
