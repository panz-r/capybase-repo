#!/usr/bin/env python3
"""Harvest census — journal mining for the sprint-20 S20.12 harvest.

Reads the eval results JSON + the preserved flight journals and produces
every census table the sprint-21 decision memo needs:

1. Verdict distribution + real-conflict PASS rate (per dataset).
2. Era census: ESCALATE_TOOLCHAIN cases + declined toolchain probes
   (the audit trail carried on every result).
3. Oversized-site census: every llm_skipped_oversized[_prompt] firing —
   the true S20.10 (combined splitting) cohort.
4. P2 keep-or-verify: deletion_superseded / preservation_flagged /
   best-of-N recovery events corpus-wide.
5. Move-edit distribution (journal-only S20.8 stage).
6. Mechanism firings: lockfile takeover, micro-CEGIS, recovery retry,
   sibling-brace repair trims, member-split candidates.
7. Skeleton x jaccard cross-tab (S20.11 metric, eval-only).
8. Duplicate-conflict awareness (S20.3): divergent-oracle twins flagged
   so the PASS rate reads honestly.

Standalone + offline: run against any results/flights pair, including
partial (in-flight) harvests.

    python scripts/harvest_census.py --results <results.json> \
        [--flights <flights-dir>] [--json-out <path>]
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import sys
from pathlib import Path


def _load_results(path: str) -> list[dict]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def _iter_journals(flights_dir: str):
    if not flights_dir:
        return
    for j in sorted(Path(flights_dir).glob("flights/*/")):
        pass  # (structure probe below; kept for clarity)
    for j in sorted(Path(flights_dir).rglob("journal.jsonl")):
        # flights/<flights-root>/flights/<case_id>/<session>/journal.jsonl
        # (older layouts: <root>/flights/<case_id>/<session>/journal.jsonl)
        # — the case id is the directory two levels above the journal,
        # i.e. the FIRST component of the path relative to the deepest
        # 'flights' ancestor.
        rel = None
        for anc in j.parents:
            if anc.name == "flights":
                rel = j.relative_to(anc)
                break
        case_id = rel.parts[0] if rel is not None and rel.parts else j.parent.name
        try:
            with open(j, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        d = json.loads(ln)
                    except json.JSONDecodeError:
                        continue
                    yield case_id, d
        except OSError:
            continue


def _verdict_census(results: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.get("verdict") or "?"] = counts.get(
            r.get("verdict") or "?", 0) + 1
    real = [r for r in results if r.get("terminal_reason") != "SAFE_SKIP"]
    passed = sum(1 for r in real if r.get("verdict") == "PASS")
    by_ds: dict[str, dict[str, int]] = {}
    for r in results:
        ds = r.get("dataset") or "?"
        by_ds.setdefault(ds, {})
        v = r.get("verdict") or "?"
        by_ds[ds][v] = by_ds[ds].get(v, 0) + 1
    return {
        "verdicts": counts,
        "real_conflicts": len(real),
        "real_pass": passed,
        "real_pass_rate": round(passed / len(real), 4) if real else None,
        "by_dataset": by_ds,
    }


def _era_census(results: list[dict]) -> dict:
    era = [r["id"] for r in results if r.get("toolchain_dead")]
    declined_probed = [
        r["id"] for r in results
        if (r.get("toolchain_probe") or {}).get("toolchain_dead") is False]
    unprobed = len(results) - len(era) - len(declined_probed)
    return {
        "escale_toolchain_cases": era,
        "probed_declined": len(declined_probed),
        "not_probed": unprobed,
    }


def _duplicate_twins() -> list[list[str]]:
    cases_dir = Path(__file__).resolve().parent.parent / \
        "extracted-testdata" / "realworld"
    groups: dict[str, list[str]] = {}
    for f in sorted(cases_dir.glob("*.json")):
        try:
            c = json.loads(f.read_text(encoding="utf-8"))
            key = hashlib.sha256(
                (c["base"] + "\x00" + c["current"] + "\x00"
                 + c["replayed"]).encode()).hexdigest()
            groups.setdefault(key, []).append(c.get("id") or f.stem)
        except Exception:  # noqa: BLE001
            continue
    out = []
    for ids in groups.values():
        if len(ids) > 1:
            out.append(sorted(ids))
    return out


def _skeleton_cross_tab(results: list[dict]) -> dict:
    nc = [r for r in results
          if r.get("verdict") in
          ("ORACLE_DIVERGENT", "NEAR_MATCH", "WORKING")]
    idiomatic = [r["id"] for r in nc
                 if r.get("matches_oracle", 1.0) < 0.80
                 and r.get("skeleton_similarity", 0.0) >= 0.85]
    return {
        "non_clean_with_content": len(nc),
        "idiomatic_candidates": idiomatic,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--flights", default=None)
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    results = _load_results(args.results)
    if not results:
        print(f"no results at {args.results}")
        return 1

    report: dict = {
        "n_results": len(results),
        "verdict_census": _verdict_census(results),
        "era_census": _era_census(results),
        "duplicate_groups": _duplicate_twins(),
        "skeleton_cross_tab": _skeleton_cross_tab(results),
    }

    # Journal-mined censuses
    journal_counts: dict[str, int] = {}
    oversized_cases: set[str] = set()
    member_split: list[dict] = []
    move_edit: list[dict] = []
    preservation_events: dict[str, int] = {}
    # S20.E8: per-case mechanism waterfall + build economy.
    mech_mix: dict[str, dict[str, int]] = {}
    build_secs: dict[str, float] = {}
    prompt_mon: dict[str, dict] = {}
    golden_path: dict[str, dict] = {}
    shattered: dict[str, int] = {}
    for case_id, d in _iter_journals(args.flights or ""):
        et = d.get("event_type") or ""
        journal_counts[et] = journal_counts.get(et, 0) + 1
        p = d.get("payload") or {}
        mm = mech_mix.setdefault(case_id, {"structural": 0, "portfolio": 0,
                                           "llm": 0})
        if et == "structurally_resolved":
            mm["structural"] += 1
        elif et == "true_side_portfolio":
            mm["portfolio"] += 1
        elif et == "candidate_accepted":
            via = str(p.get("via") or "")
            if via.startswith("structural") or via.startswith("deterministic"):
                mm["structural"] += 1
            else:
                mm["llm"] += 1
        if et == "build_probe":
            try:
                build_secs[case_id] = build_secs.get(case_id, 0.0) + float(
                    p.get("duration_s") or p.get("elapsed") or 0.0)
            except (TypeError, ValueError):
                pass
        # Sprint-25 prompt monitoring: prompt_composition cross-tabs —
        # context size vs outcome, the R5 retry-variant tags, and the
        # golden-path retrieval hit-rate (repair few-shot).
        if et == "prompt_composition":
            _pc = prompt_mon.setdefault(
                case_id, {"n_prompts": 0, "max_ctx_tokens": 0,
                          "variants": {}})
            _pc["n_prompts"] += 1
            try:
                _pc["max_ctx_tokens"] = max(
                    _pc["max_ctx_tokens"],
                    int(p.get("context_token_estimate") or 0))
            except (TypeError, ValueError):
                pass
        elif et == "retrieval_explained" or et == "context_built":
            if p.get("retrieval_scores"):
                _rg = golden_path.setdefault(
                    case_id, {"hits": 0, "best": 0.0})
                _rg["hits"] += 1
                try:
                    _rg["best"] = max(
                        _rg["best"], max(p["retrieval_scores"]))
                except (TypeError, ValueError):
                    pass
        elif et == "shattered_repair_accept":
            shattered[case_id] = shattered.get(case_id, 0) + 1
        if et in ("llm_skipped_oversized", "llm_skipped_oversized_prompt"):
            oversized_cases.add(case_id)
        elif et == "class_member_split_candidate":
            member_split.append({"case": case_id, **{
                k: p.get(k) for k in
                ("region_lines", "current_member_points",
                 "replayed_member_points", "decline_reason")}})
        elif et == "move_edit_candidate":
            for c in (p.get("candidates") or [])[:1]:
                move_edit.append({"case": case_id, **c})
        elif "preservation" in et:
            preservation_events[et] = preservation_events.get(et, 0) + 1
        elif et == "candidate_accepted":
            # feature-level carveout marker rides accepted-candidate
            # features — scan only these payloads (defect review pass 3:
            # the prior json.dumps-per-event scan serialized EVERY event).
            if "deletion_superseded" in json.dumps(p):
                preservation_events["deletion_superseded(feature)"] = \
                    preservation_events.get(
                        "deletion_superseded(feature)", 0) + 1

    report["prompt_monitoring"] = {
        "cases_with_prompts": len(prompt_mon),
        "avg_prompts_per_case": (
            round(sum(v["n_prompts"] for v in prompt_mon.values())
                  / max(1, len(prompt_mon)), 1)),
        "max_context_tokens_seen": max(
            (v["max_ctx_tokens"] for v in prompt_mon.values()), default=0),
        # Hit COUNTS only — the retrieval_scores field carries raw
        # retriever distances (thousands-scale), not 0-1 similarities;
        # averaging them as scores was meaningless.
        "golden_path_cases": len(golden_path),
        "golden_path_total_hit_prompts": sum(
            v["hits"] for v in golden_path.values()),
        "shattered_repair_accepts": dict(sorted(shattered.items())),
    }
    report["journal_events"] = journal_counts
    report["oversized_cohort"] = sorted(oversized_cases)
    report["member_split_distribution"] = member_split[:50]
    report["move_edit_distribution"] = move_edit[:50]
    report["preservation_events"] = preservation_events
    # S20.E8 tables: mechanism mix + build economy.
    _tot = {"structural": 0, "portfolio": 0, "llm": 0}
    for mm in mech_mix.values():
        for k in _tot:
            _tot[k] += mm[k]
    report["mechanism_waterfall"] = {
        "cases_with_journals": len(mech_mix),
        "totals": _tot,
        "llm_only_cases": sorted(c for c, m in mech_mix.items()
                                 if m["llm"] and not (
                                     m["structural"] or m["portfolio"])),
    }
    report["build_economy_top10"] = sorted(
        build_secs.items(), key=lambda kv: -kv[1])[:10]

    # ---- formatted report ----
    v = report["verdict_census"]
    print(f"== harvest census: {report['n_results']} results ==")
    print(f"  verdicts: {v['verdicts']}")
    print(f"  real-conflict PASS rate: {v['real_pass']}/{v['real_conflicts']}"
          f" = {v['real_pass_rate']}")
    e = report["era_census"]
    print(f"== era census: {len(e['escale_toolchain_cases'])} toolchain-dead, "
          f"{e['probed_declined']} probed-declined, "
          f"{e['not_probed']} unprobed ==")
    print("== journal-mechanism firings ==")
    for k in sorted(journal_counts):
        if any(s in k for s in (
                "oversized", "lockfile", "micro_cegis", "recovery_retry",
                "move_edit", "class_member", "preservation", "brace",
                "true_side", "resurrections")):
            print(f"  {k}: {journal_counts[k]}")
    print(f"== oversized cohort (S20.10 decision): "
          f"{report['oversized_cohort']}")
    print(f"== move-edit distribution: {len(report['move_edit_distribution'])} "
          f"candidates")
    w = report["mechanism_waterfall"]
    print(f"== mechanism waterfall: {w['totals']} across "
          f"{w['cases_with_journals']} cases "
          f"(llm-only: {len(w['llm_only_cases'])})")
    print(f"== build economy (top spenders, s): "
          f"{[(c, round(s)) for c, s in report['build_economy_top10'][:5]]}")
    print(f"== preservation events: {report['preservation_events'] or 'none'}")
    s = report["skeleton_cross_tab"]
    print(f"== skeleton x jaccard: {s['non_clean_with_content']} non-clean "
          f"with content, {len(s['idiomatic_candidates'])} idiomatic "
          f"candidates {s['idiomatic_candidates'][:8]}")
    print(f"== duplicate groups: {len(report['duplicate_groups'])} "
          f"(twins to dedupe/pair-treat in metrics)")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report, indent=1, default=str), encoding="utf-8")
        print(f"json: {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
