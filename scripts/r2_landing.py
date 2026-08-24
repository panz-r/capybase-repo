#!/usr/bin/env python3
"""Reround landing pipeline: extracts, flip audits, totals, safety cross-tabs.

Runs against whatever r2-<lang>.json files exist (partial-safe): completes
incrementally as shards land, and produces the final README-row-2 inputs
when all four exist. Read-only over results/flights; writes only the
docs/results/s22r2/ extracts. No model requests.

    .venv/bin/python scripts/r2_landing.py [--skip-extracts]
"""
from __future__ import annotations

import argparse
import glob
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
R2 = Path("/var/tmp/capybase-live/s22")
OUT = REPO / "docs/results/s22r2"
KEEP = ("id", "language", "dataset", "verdict", "terminal_reason",
        "matches_oracle", "escalated", "elapsed", "repeat_verdicts", "reason")
SHARDS = (("python", "shard1-python"), ("c", "shard2-c"),
          ("rust", "shard3-rust"), ("cpp", "shard4-cpp"))


def extract(lang: str) -> int:
    src = R2 / f"r2-{lang}.json"
    if not src.exists():
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    cases = json.loads(src.read_text())
    with open(OUT / f"r2-{lang}.jsonl", "w") as f:
        for c in sorted(cases, key=lambda c: c["id"]):
            f.write(json.dumps({k: c.get(k) for k in KEEP},
                               separators=(",", ":")) + "\n")
    return len(cases)


def flip_audit(lang: str) -> None:
    src = OUT / f"r2-{lang}.jsonl"
    base = REPO / f"docs/results/s22/shard{'1234'[['python','c','rust','cpp'].index(lang)]}-{'python' if lang=='python' else lang}.jsonl"
    if not src.exists() or not base.exists():
        return
    print(f"\n=== flip audit: r2-{lang} vs {base.name} ===")
    subprocess.run([str(REPO / ".venv/bin/python"),
                    str(REPO / "scripts/verdict_diff.py"),
                    "--new", str(src), "--old", str(base)])


def totals() -> None:
    rows = []
    for lang, _ in SHARDS:
        p = OUT / f"r2-{lang}.jsonl"
        if not p.exists():
            continue
        cs = [json.loads(l) for l in open(p) if l.strip()]
        import collections
        v = collections.Counter(c["verdict"] for c in cs)
        era = v["ESCALATE_TOOLCHAIN"]
        safe = sum(1 for c in cs if c.get("terminal_reason") == "SAFE_SKIP")
        p_ = v["PASS"]
        rows.append((lang, len(cs), p_, era, safe))
    if not rows:
        return
    print("\n=== uniform-formula totals (PASS/(cases-era-SAFE_SKIP)) ===")
    T = [0, 0, 0, 0]
    for lang, n, p_, era, safe in rows:
        for i, x in enumerate((n, p_, era, safe)):
            T[i] += x
        print(f"{lang:8s} {n:4d} PASS={p_:4d} era={era:3d} skip={safe:2d} "
              f"raw={p_/n:6.1%} adj={p_/max(1, n-era-safe):6.1%}")
    n, p_, era, safe = T
    if rows:
        print(f"{'TOTAL':8s} {n:4d} PASS={p_:4d} era={era:3d} skip={safe:2d} "
              f"raw={p_/n:6.1%} adj={p_/max(1, n-era-safe):6.1%}")


def cross_tabs() -> None:
    dg = si = 0
    for f in glob.glob(str(R2 / "flights-r2-*/flights/*/*/journal.jsonl")):
        for line in open(f, errors="replace"):
            if '"resurrection_downgrade"' in line:
                dg += 1
            elif '"symbol_inject"' in line and '"micro_cegis_patch"' in line:
                si += 1
    print(f"\n=== safety cross-tabs (all r2 flights) ===\n"
          f"resurrection_downgrade events: {dg}\nsymbol_inject patches: {si}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-extracts", action="store_true")
    args = ap.parse_args()
    if not args.skip_extracts:
        for lang, _ in SHARDS:
            n = extract(lang)
            if n:
                print(f"extracted r2-{lang}: {n} cases", flush=True)
    for lang, _ in SHARDS:
        flip_audit(lang)
    totals()
    cross_tabs()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
