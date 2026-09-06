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
    "toolchain_dead", "resolution_bucket", "provenance_mix",
)

#: Buckets that mean "the LLM was involved" (EXTEND-70's llm column: broad
#: breakdown — the LLM participated, not that it solved the case alone).
_LLM_BUCKETS = ("llm_one_shot", "llm_cegis")


def _journal_mechanism(flights_root: Path | None, case_id: str) -> str | None:
    """Derive a case's mechanism from its preserved flight journal.

    The fallback for rows whose provenance_mix is empty: the phase-1 fast
    path and other whole-file paths bypass the per-unit candidate loop, so
    classify_resolution_bucket saw nothing — but the journal records what
    actually ran (phase1_fast_path_adjudication, true_side_portfolio,
    candidate_accepted with provenance/via).
    """
    if flights_root is None:
        return None
    import glob
    journals = sorted(glob.glob(str(flights_root / "**" / case_id / "**" / "journal.jsonl"), recursive=True))
    if not journals:
        # some layouts nest one level deeper/shallower — try by suffix match
        journals = sorted(glob.glob(str(flights_root / "**" / "journal.jsonl"), recursive=True))
        journals = [j for j in journals if f"/{case_id}/" in j]
    for jpath in journals:  # newest last; later sessions overwrite the story
        mix: dict[str, int] = {}
        fast_path = portfolio = False
        try:
            for line in open(jpath, encoding="utf-8"):
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = ev.get("event_type", "")
                if t == "phase1_fast_path_adjudication":
                    fast_path = True
                elif t == "true_side_portfolio":
                    portfolio = True
                elif t == "candidate_accepted":
                    prov = (ev.get("payload") or {}).get("provenance") \
                        or (ev.get("payload") or {}).get("via") or "unknown"
                    mix[prov] = mix.get(prov, 0) + 1
        except OSError:
            continue
        if fast_path:
            return "phase1_fast_path"
        if portfolio:
            return "true_side_portfolio"
        if mix:
            return max(mix.items(), key=lambda kv: kv[1])[0]
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--round", required=True, help="round name, e.g. s26")
    ap.add_argument(
        "--flights", default=None, metavar="DIR",
        help="The harvest's --preserve-flights root. Enables the mechanism "
             "histogram's journal fallback: rows whose provenance_mix is "
             "empty (the phase-1 fast path and other whole-file paths "
             "bypass the per-unit candidate loop) get their mechanism "
             "derived from the preserved journal's event trace.")
    ap.add_argument(
        "--override", action="append", default=None, metavar="JSON",
        help="A later rerun's results JSON whose verdicts REPLACE the "
             "harvest's for matching case ids (later files win). Used when "
             "the harvest ran pre-fix code for a known case set: the "
             "fix-validation rerun's verdicts are the honest numbers. The "
             "replaced row's repeat_verdicts carry a marker of the swap.")
    args = ap.parse_args()

    records = json.loads(Path(args.results).read_text())
    swapped = 0
    if args.override:
        index = {r["id"]: r for r in records}
        for ovr_path in args.override:
            for ovr in json.loads(Path(ovr_path).read_text()):
                old = index.get(ovr["id"])
                if old is None:
                    continue
                ovr = dict(ovr)
                ovr["repeat_verdicts"] = list(
                    ovr.get("repeat_verdicts") or []) + [
                        f"override-from:{old.get('verdict')}"]
                index[ovr["id"]] = ovr
                swapped += 1
        records = list(index.values())
        print(f"overridden verdicts: {swapped}")
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
    # SAFE_SKIP (git resolved cleanly on replay; verdict=ESCALATE +
    # terminal_reason=SAFE_SKIP) leaves the denominator, matching the
    # README convention (676 loaded − 16 skips = 660).
    total = passes = working = era = 0
    for lang, rows in by_lang.items():
        for row in rows:
            if row.get("terminal_reason") == "SAFE_SKIP":
                continue
            total += 1
            if row["verdict"] == "PASS":
                passes += 1
            elif row["verdict"] == "WORKING":
                working += 1
            if row.get("toolchain_dead"):
                era += 1
    denom_adj = total - era

    # Per-language table (README rows) incl. the llm column (EXTEND-70:
    # cases whose resolution_bucket is llm_one_shot/llm_cegis — the LLM
    # was involved).
    by_language = {}
    for lang, rows in sorted(by_lang.items()):
        lt = lp = lw = le = lllm = 0
        for row in rows:
            if row.get("terminal_reason") == "SAFE_SKIP":
                continue
            lt += 1
            v = row["verdict"]
            if v == "PASS":
                lp += 1
            elif v == "WORKING":
                lw += 1
            if row.get("toolchain_dead"):
                le += 1
            if row.get("resolution_bucket") in _LLM_BUCKETS:
                lllm += 1
        by_language[lang] = {
            "cases": lt, "pass": lp, "working": lw, "era_dead": le,
            "llm": lllm,
        }

    # Mechanism histogram (EXTEND-70: mechanism | cases | PASS | WORKING |
    # P+W %), from each case's dominant provenance; journal fallback for
    # empty-mix rows when --flights is given.
    flights_root = Path(args.flights) if args.flights else None
    mech: dict[str, dict[str, int]] = {}
    for rec in records:
        if rec.get("terminal_reason") == "SAFE_SKIP":
            continue
        mix = rec.get("provenance_mix") or {}
        if mix:
            mechanism = max(mix.items(), key=lambda kv: kv[1])[0]
        else:
            mechanism = _journal_mechanism(flights_root, rec["id"])
            if mechanism is None:
                mechanism = ("(unresolved)" if rec.get("escalated")
                             else "(unclassified)")
        d = mech.setdefault(mechanism, {"cases": 0, "pass": 0, "working": 0})
        d["cases"] += 1
        v = rec.get("verdict")
        if v == "PASS":
            d["pass"] += 1
        elif v == "WORKING":
            d["working"] += 1
    histogram = []
    for m, d in sorted(mech.items(), key=lambda kv: -kv[1]["cases"]):
        pw = round(100 * (d["pass"] + d["working"]) / d["cases"], 1) if d["cases"] else 0
        histogram.append({"mechanism": m, **d, "pw_pct": pw})

    # Participation view: a case counts for EVERY mechanism that
    # participated in building its final ACCEPTED candidates — walking
    # backwards from the acceptance: each accepted provenance string is
    # a "+"-joined lineage of its composers (e.g.
    # plain_llm+keyed_item_union = the model's candidate, then the union
    # layer completed it), so split on "+" and union across units.
    # Mechanisms that ran but were not part of the winning path are
    # deliberately absent. Escalated cases have no winning path and are
    # excluded; having participated in an accepted candidate IS this
    # table's success criterion, so no PASS/WORKING columns — the
    # breakdown treats accepted as passed. Empty-mix rows (whole-file
    # paths) participate via their journal-derived mechanism.
    part: dict[str, int] = {}
    for rec in records:
        if rec.get("terminal_reason") == "SAFE_SKIP":
            continue
        if rec.get("verdict") in ("ESCALATE", "ESCALATE_TOOLCHAIN"):
            continue
        mechs = set()
        for prov in (rec.get("provenance_mix") or {}):
            mechs.update(m for m in prov.split("+") if m)
        if not mechs:
            j = _journal_mechanism(flights_root, rec["id"])
            if j:
                mechs.add(j)
        for m in mechs:
            part[m] = part.get(m, 0) + 1
    participation = [
        {"mechanism": m, "cases": n,
         "pct_of_corpus": round(100 * n / total, 1) if total else 0}
        for m, n in sorted(part.items(), key=lambda kv: -kv[1])
    ]

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
            "by_language": by_language,
            "mechanism_histogram": histogram,
            "mechanism_participation": participation,
        },
        # caller completes: mechanism_commit, state, command_template,
        # ran, verification, outcome_summary
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps(meta["recount"], indent=1))


if __name__ == "__main__":
    main()
