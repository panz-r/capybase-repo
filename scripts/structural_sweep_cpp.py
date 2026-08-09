#!/usr/bin/env python3
"""Deterministic-only structural sweep over the C++ corpus.

Runs the full structural resolver pipeline (marker parse → build units →
entity-split → diff3-refine → resolve) over all 88 C++ cases WITHOUT any
model calls, git repo, or compiler. Reports a routing census: which rule
fired for each sub-unit. Run before and after any resolver change to see
the blast radius instantly.

Usage:
    .venv/bin/python scripts/structural_sweep_cpp.py [--no-diff3] [--out FILE]

With --no-diff3: skip git merge-file --diff3 (truly git-free; coverage drops
from ~100/335 to ~16/335 since most rules decline without the tight hunk base).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from collections import Counter

# Ensure src/ is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from capybase.adapters.parsers import parse_marker_blocks
from capybase.conflict_model import ConflictUnit, ConflictSide
from capybase.structural_resolver import resolve_structurally

TESTDATA = Path(__file__).resolve().parent.parent / "extracted-testdata" / "realworld"


def _side(label, text):
    return ConflictSide(label=label, text=text)


def _load_cpp_cases():
    """Load all C++ case JSONs from the corpus."""
    cases = []
    for f in sorted(TESTDATA.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if d.get("language") != "cpp":
            continue
        cases.append(d)
    return cases


def _diff3_refine(base_text, cur_text, rep_text):
    """Run git merge-file --diff3 to get refined conflict hunks."""
    import subprocess
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as bf:
        bf.write(base_text); base_path = bf.name
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as cf:
        cf.write(cur_text); cur_path = cf.name
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as rf:
        rf.write(rep_text); rep_path = rf.name
    try:
        proc = subprocess.run(
            ["git", "merge-file", "-p", "--diff3", cur_path, base_path, rep_path],
            capture_output=True, text=True, timeout=5,
        )
    finally:
        for p in (base_path, cur_path, rep_path):
            os.unlink(p)

    if proc.returncode not in (0, 1):
        return None  # error

    # Parse diff3 output: <<<<<<< cur ======= ||||| base >>>>>>> rep
    lines = proc.stdout.split("\n")
    hunks = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("<<<<<<<"):
            i += 1
            cur_h = []
            while i < len(lines) and not lines[i].startswith("======="):
                cur_h.append(lines[i])
                i += 1
            i += 1  # skip =======
            # Check for ||||| (diff3 base)
            if i < len(lines) and lines[i].startswith("|||||||"):
                i += 1  # skip |||||
                base_h = []
                while i < len(lines) and not lines[i].startswith(">>>>>>>"):
                    base_h.append(lines[i])
                    i += 1
                i += 1  # skip >>>>>>>
                rep_h = []
                while i < len(lines) and not lines[i].startswith("<<<<<<<"):
                    rep_h.append(lines[i])
                    i += 1
                hunks.append({
                    "current": "\n".join(cur_h),
                    "base": "\n".join(base_h),
                    "replayed": "\n".join(rep_h),
                })
            else:
                # No diff3 base — just cur then rep
                rep_h = []
                while i < len(lines) and not lines[i].startswith(">>>>>>>"):
                    rep_h.append(lines[i])
                    i += 1
                i += 1  # skip >>>>>>>
                hunks.append({
                    "current": "\n".join(cur_h),
                    "base": "",
                    "replayed": "\n".join(rep_h),
                })
        else:
            i += 1
    return hunks


def _build_units(case, use_diff3=True):
    """Build ConflictUnits from a case JSON, mirroring the conflict extractor.

    Case data structure:
    - base: whole-file base text (the common ancestor)
    - current: the CURRENT_UPSTREAM_SIDE text (conflict block interior)
    - replayed: the REPLAYED_COMMIT_SIDE text (conflict block interior)
    - marker_original: the whole file WITH conflict markers embedded

    The case JSON's current/replayed are the conflict block interiors
    (one per marker region), NOT whole files. base is the whole file.
    """
    marker = case.get("marker_original", "")
    path = case.get("conflict_path") or case.get("path", "unknown.cpp")
    base_full = case.get("base", "")
    cur_text = case.get("current", "")
    rep_text = case.get("replayed", "")

    # Parse markers to find conflict regions
    blocks = parse_marker_blocks(marker)
    if not blocks:
        return []

    # Optionally compute diff3 refinement: run git merge-file --diff3 on
    # (cur_text, base_full, rep_text) to find the tight conflict base.
    # Skip on large files (>50K chars) — git merge-file hangs on the 180K+
    # ClickHouse files and the 879KB amalgamated header. Those cases hit the
    # oversized-prompt guard at runtime anyway (LLM never resolves them).
    # Without diff3, the resolver gets the whole-file base and the histogram
    # diff is O(n²) — hangs on 100K+ char inputs.
    diff3_hunks = None
    if use_diff3 and len(base_full) < 50_000:
        try:
            diff3_hunks = _diff3_refine(base_full, cur_text, rep_text)
        except Exception:
            diff3_hunks = None

    units = []
    for idx, block in enumerate(blocks):
        # If diff3 didn't produce a refined base, skip — the resolver would hang
        # on the whole-file base (O(n²) histogram diff on 100K+ char inputs).
        if use_diff3 and not diff3_hunks:
            continue

        base_text = base_full  # whole-file base (the unit.base.text convention)

        # If diff3 is available, use the refined hunk
        refined = None
        if diff3_hunks and idx < len(diff3_hunks):
            h = diff3_hunks[idx]
            if h["base"]:
                refined = (h["current"], h["base"], h["replayed"])

        meta = {}
        if refined:
            meta["diff3_refined"] = {
                "current": refined[0],
                "base": refined[1],
                "replayed": refined[2],
            }

        unit = ConflictUnit(
            session_id="sweep", step_index=0, path=path,
            unit_id=f"{path}:{idx}",
            language="cpp",
            base=_side("BASE", base_text),
            current=_side("CURRENT_UPSTREAM_SIDE", cur_text),
            replayed=_side("REPLAYED_COMMIT_SIDE", rep_text),
            original_worktree_text=marker,
            marker_span=block.span,
            structural_metadata=meta,
        )
        units.append(unit)

    return units


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Deterministic structural sweep over C++ corpus")
    ap.add_argument("--no-diff3", action="store_true", help="Skip diff3 refinement (git-free)")
    ap.add_argument("--out", default=None, help="Write JSON census to this file")
    args = ap.parse_args()

    use_diff3 = not args.no_diff3
    cases = _load_cpp_cases()
    print(f"Loaded {len(cases)} C++ cases (diff3={'on' if use_diff3 else 'off'})")

    census = Counter()
    per_unit = []
    for case in cases:
        case_id = case.get("id", "?")
        units = _build_units(case, use_diff3=use_diff3)
        for unit in units:
            result = resolve_structurally(unit)
            rule = result.rule if result.resolved else "unresolved"
            census[rule] += 1
            refined = unit.structural_metadata.get("diff3_refined") is not None
            per_unit.append({
                "case_id": case_id,
                "unit_id": unit.unit_id,
                "path": unit.path,
                "rule": rule,
                "diff3_refined": refined,
                "resolved_text": (result.text or "")[:200],
            })

    total = sum(census.values())
    resolved = total - census.get("unresolved", 0)

    print(f"\n{'='*60}")
    print(f"STRUCTURAL SWEEP CENSUS ({total} units, {resolved} resolved)")
    print(f"{'='*60}")
    for rule, count in census.most_common():
        pct = count / total * 100
        print(f"  {rule:30s} {count:4d}  ({pct:.1f}%)")
    print(f"\n  resolved: {resolved}/{total} = {resolved/total*100:.1f}%")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "total": total,
            "resolved": resolved,
            "census": dict(census),
            "units": per_unit,
        }, indent=2))
        print(f"\nfull census: {out}")


if __name__ == "__main__":
    main()
