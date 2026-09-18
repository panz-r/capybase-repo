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
    "model_involved",
)

#: Buckets that mean "the LLM's text is the final resolution". NOT the
#: README's llm column — the column is whole-process involvement (see
#: _llm_involved); kept for backward reference in extracts only.
_LLM_BUCKETS = ("llm_one_shot", "llm_cegis")

#: Journal events that mean "a model call happened during this case's
#: resolution" — the whole-process llm predicate (validated 2026-09-18
#: against the full s28 corpus: rows-flag and journal scan both yield
#: 958/1,481). Covers candidate generation, comment reconciliation,
#: block-capture decisions, every LLM adjudication ballot (the generic
#: adjudication-verdict dict catches midband/whole-side/phase-1), and
#: the tier-2 ballot.
_LLM_JOURNAL_EVENTS = frozenset({
    "candidate_generated",
    "comment_plan_generated",
    "comment_model_call_failed",
    "f1_tier2_adjudication_declined",
})


def _flights_index(flights_root: Path | None) -> dict[str, list[str]]:
    """One walk of the flights tree: case_id -> journal paths (newest
    first, non-crashed preferred). Shared by the mechanism and llm
    fallbacks so neither pays a per-case recursive glob."""
    if flights_root is None:
        return {}
    import os
    idx: dict[str, list[str]] = {}
    for dirpath, dirnames, filenames in os.walk(flights_root):
        if "journal.jsonl" not in filenames:
            continue
        # .../<case_id>/<session>/journal.jsonl — the case dir is the
        # parent of the journal's session dir.
        parts = Path(dirpath).parts
        if len(parts) < 2:
            continue
        case_id = parts[-2]
        try:
            mtime = os.path.getmtime(Path(dirpath) / "journal.jsonl")
        except OSError:
            mtime = 0.0
        idx.setdefault(case_id, []).append(
            (mtime, "-crashed" in parts[-1], str(Path(dirpath) / "journal.jsonl")))
    for case_id in idx:
        # newest first, non-crashed preferred (same policy as
        # _journal_mechanism's pool ordering)
        idx[case_id] = [
            p for _, _, p in
            sorted(idx[case_id], key=lambda t: (t[1], -t[0]))]
    return idx


def _llm_involved(rec: dict, flights_idx: dict[str, list[str]]) -> bool | None:
    """The README llm column: did ANY model call happen during the case's
    whole resolution process? The complement (no call) is provably
    solvable without model access.

    Source of truth, in order: the row's ``model_involved`` flag (set by
    the harness at run time — exact); else the preserved flight journals
    (the predicate above); else None = unknown (older results files run
    without --flights). Never falls back to resolution_bucket — that
    counts only landed-LLM-text cases and is the undercount this
    function exists to fix (147 vs 958 on the s28 corpus).
    """
    if rec.get("model_involved") is not None:
        return bool(rec["model_involved"])
    for jpath in flights_idx.get(rec["id"], []):
        try:
            with open(jpath, encoding="utf-8") as f:
                for line in f:
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    t = ev.get("event_type", "")
                    if t in _LLM_JOURNAL_EVENTS:
                        return True
                    p = ev.get("payload") or {}
                    if t == "block_capture_decision" and p.get("reason"):
                        return True
                    adj = p.get("adjudication")
                    if isinstance(adj, dict) and adj.get("verdict"):
                        return True
        except OSError:
            continue
    return False if flights_idx else None


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
    import os
    journals = sorted(glob.glob(str(flights_root / "**" / case_id / "**" / "journal.jsonl"), recursive=True))
    if not journals:
        # some layouts nest one level deeper/shallower — try by suffix match
        journals = sorted(glob.glob(str(flights_root / "**" / "journal.jsonl"), recursive=True))
        journals = [j for j in journals if f"/{case_id}/" in j]
    # s27-73 (seventh pass): the old loop returned the FIRST match, and the
    # s27-72 crash-preservation dirs (<session>-crashed) MATCH this glob —
    # session ids are random hex, so a crashed attempt's journal could beat
    # the successful retry's by sort order. Prefer non-crashed dirs, then
    # the NEWEST journal (later sessions overwrite the story).
    crashed = [j for j in journals if "-crashed" in j]
    normal = [j for j in journals if "-crashed" not in j]
    pool = normal or crashed
    def _mtime_safe(pth):
        try:
            return os.path.getmtime(pth)
        except OSError:
            return 0.0
    pool = sorted(pool, key=_mtime_safe, reverse=True)
    for jpath in pool:
        accepts: dict[tuple, str] = {}
        superseded: set[tuple] = set()
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
                elif t == "outcomes_superseded":
                    # s27-73: a whole-file takeover supersedes earlier
                    # per-unit accepts — subtract or the stale mechanism
                    # wins max(mix) on the fallback path (the same
                    # double-count the live counters fixed in s27-72).
                    # Collect-then-count: the superseded event arrives
                    # AFTER the accepts it kills.
                    for cid in (ev.get("payload") or {}).get(
                            "candidate_ids") or []:
                        superseded.add((ev.get("step_index"), cid))
                elif t == "candidate_accepted":
                    payload = ev.get("payload") or {}
                    prov = payload.get("provenance") \
                        or payload.get("via") or "unknown"
                    accepts[(ev.get("step_index"),
                             payload.get("candidate_id"))] = prov
        except OSError:
            continue
        mix: dict[str, int] = {}
        for key, prov in accepts.items():
            if key not in superseded:
                mix[prov] = mix.get(prov, 0) + 1
        if fast_path:
            return "phase1_fast_path"
        if portfolio:
            return "true_side_portfolio"
        if mix:
            return max(mix.items(), key=lambda kv: kv[1])[0]
    return None


def _is_skip(rec: dict) -> bool:
    """The measurement/infrastructure skip classes — not resolver outcomes,
    excluded from every denominator (s27-73). SAFE_SKIP existed; the live
    classifier emits SETUP_FAILED for harness crashes (s27-72); the
    scenario harness's verdicts carry no terminal_reason at all."""
    if rec.get("terminal_reason") in ("SAFE_SKIP", "SETUP_FAILED"):
        return True
    return rec.get("verdict") in ("SAFE_SKIP", "ALL_ABSENT", "ORACLE_HOLE")


def _row_mechanism(rec: dict, flights_root: Path | None) -> str | None:
    """One row's dominant mechanism: provenance_mix's max, else the journal
    fallback (phase-1 fast path / other whole-file paths bypass the
    per-unit candidate loop and leave the mix empty)."""
    if _is_skip(rec):
        return None  # s27-74: skip rows never touch the journal fallback
    mix = rec.get("provenance_mix") or {}
    if mix:
        return max(mix.items(), key=lambda kv: kv[1])[0]
    m = _journal_mechanism(flights_root, rec["id"])
    if m is None:
        return "(unresolved)" if rec.get("escalated") else "(unclassified)"
    return m


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
    ap.add_argument(
        "--prior", default=None, metavar="JSON",
        help="The prior round's results JSON. Emits delta_pw_adj per "
             "language + total: difference of UNROUNDED P+W adj values, "
             "rounded once (the README Δ convention).")
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

    # s27-73: derive each row's mechanism ONCE (provenance_mix, else the
    # journal fallback) and PERSIST it in the extract — the old extracts
    # left the fallback-derived mechanism implied, so the histogram was
    # unreproducible once the flights tree aged out.
    flights_root = Path(args.flights) if args.flights else None
    for rec in records:
        rec["mechanism"] = _row_mechanism(rec, flights_root)

    by_lang: dict[str, list] = {}
    for rec in records:
        row = {k: rec.get(k) for k in _FIELDS}
        row["mechanism"] = rec.get("mechanism")
        by_lang.setdefault(rec.get("language") or "?", []).append(row)

    for lang, rows in sorted(by_lang.items()):
        path = out_dir / f"{args.round}-{lang}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        print(f"{path}: {len(rows)} rows")
    # s27-73 (rerun hygiene): prune stale language extracts a rerun no
    # longer produces (the old code left them beside the rewritten meta,
    # and extracts and meta silently disagreed).
    for stale in out_dir.glob(f"{args.round}-*.jsonl"):
        lang = stale.stem[len(args.round) + 1:]
        if lang not in by_lang:
            stale.unlink()
            print(f"pruned stale extract: {stale.name}")

    # README-table recount from the extracts themselves (single source).
    # SAFE_SKIP (git resolved cleanly on replay; verdict=ESCALATE +
    # terminal_reason=SAFE_SKIP) leaves the denominator, matching the
    # README convention (676 loaded − 16 skips = 660).
    total = passes = working = era = 0
    for lang, rows in by_lang.items():
        for row in rows:
            if _is_skip(row):
                continue
            total += 1
            if row["verdict"] == "PASS":
                passes += 1
            elif row["verdict"] == "WORKING":
                working += 1
            if row.get("toolchain_dead"):
                era += 1
    denom_adj = total - era

    # Per-language table (README rows). llm = whole-process model
    # involvement (row flag, else the journal predicate) — the README's
    # definition; era-excluded denominators for adj/pw_adj, rounded once
    # from unrounded values (hand-rounding produced a wrong total row in
    # the 2026-09-18 README pass — these numbers are copy-paste, never
    # recomputed by hand).
    flights_idx = _flights_index(flights_root)
    llm_unknown = 0
    by_language = {}
    for lang, rows in sorted(by_lang.items()):
        lt = lp = lw = le = lllm = 0
        for row in rows:
            if _is_skip(row):
                continue
            lt += 1
            v = row["verdict"]
            if v == "PASS":
                lp += 1
            elif v == "WORKING":
                lw += 1
            if row.get("toolchain_dead"):
                le += 1
            involved = _llm_involved(row, flights_idx)
            if involved is None:
                llm_unknown += 1
            elif involved:
                lllm += 1
        denom = lt - le
        by_language[lang] = {
            "cases": lt, "pass": lp, "working": lw, "era_dead": le,
            "llm": lllm,
            "pass_pct": round(100 * lp / lt, 1) if lt else 0,
            "adj_pct": round(100 * lp / denom, 1) if denom else 0,
            "pw_adj_pct": round(100 * (lp + lw) / denom, 1) if denom else 0,
        }
    if llm_unknown:
        print(f"WARNING: llm unknown for {llm_unknown} rows (no "
              f"model_involved flag and no --flights) — reported as "
              f"not-involved. Rerun with --flights or a newer results "
              f"file for the true column.")

    # accepted = counted − escalated (the mechanism counting-rules line;
    # hand-derived as 1,386 during the 2026-09-18 README pass).
    accepted = sum(
        1 for lang_rows in by_lang.values() for row in lang_rows
        if not _is_skip(row)
        and row["verdict"] not in ("ESCALATE", "ESCALATE_TOOLCHAIN"))

    # Δ P+W adj vs the prior round (--prior): difference of unrounded
    # values, rounded once — the README Δ convention (documented so the
    # displayed-difference vs raw-difference ambiguity that required a
    # 2026-09-18 audit cannot recur).
    delta: dict[str, float] | None = None
    if args.prior:
        prior_recs = json.loads(Path(args.prior).read_text())
        prior_by_lang: dict[str, list] = {}
        for rec in prior_recs:
            prior_by_lang.setdefault(rec.get("language") or "?", []).append(rec)
        delta = {}
        for lang in set(by_language) | {
                r.get("language") or "?" for r in prior_recs}:
            def _pw_adj(recs):
                t = p = w = e = 0
                for rec in recs:
                    if _is_skip(rec):
                        continue
                    t += 1
                    v = rec.get("verdict")
                    if v == "PASS":
                        p += 1
                    elif v == "WORKING":
                        w += 1
                    if rec.get("toolchain_dead"):
                        e += 1
                return (100.0 * (p + w) / (t - e)) if t - e else 0.0
            cur = _pw_adj(by_lang.get(lang, []))
            pri = _pw_adj(prior_by_lang.get(lang, []))
            delta[lang] = round(cur - pri, 1)
        delta["total"] = round(
            (100.0 * (passes + working) / denom_adj if denom_adj else 0.0)
            - _pw_adj(prior_recs), 1)

    # readme_table: the README's per-language table in its exact column
    # order — copy-paste, never hand-built. Δ column only when --prior.
    _DISPLAY_LANGS = ("python", "c", "cpp", "rust", "?")
    ordered = [l for l in _DISPLAY_LANGS if l in by_language] + \
              [l for l in sorted(by_language) if l not in _DISPLAY_LANGS]
    header = ("| lang | cases | PASS | WORKING | era-dead | llm | "
              "PASS % | adj % | P+W adj % |")
    sep = "|------|-------|------|---------|----------|-----|" \
          "--------|-----------|-----------|"
    if delta is not None:
        header += " Δ P+W |"
        sep += "-------|"
    lines = [header, sep]
    for lang in ordered:
        v = by_language[lang]
        row = (f"| {lang} | {v['cases']} | {v['pass']} | {v['working']} "
               f"| {v['era_dead']} | {v['llm']} | {v['pass_pct']}% "
               f"| {v['adj_pct']}% | {v['pw_adj_pct']}% |")
        if delta is not None:
            row += f" {delta[lang]:+.1f}pp |"
        lines.append(row)
    total_row = (f"| **total** | **{total}** | **{passes}** "
                 f"| **{working}** | **{era}** "
                 f"| **{sum(v['llm'] for v in by_language.values())}** "
                 f"| **{round(100 * passes / total, 1) if total else 0}%** "
                 f"| **{round(100 * passes / denom_adj, 1) if denom_adj else 0}%** "
                 f"| **{round(100 * (passes + working) / denom_adj, 1) if denom_adj else 0}%** |")
    if delta is not None:
        total_row += f" **{delta['total']:+.1f}pp** |"
    lines.append(total_row)
    readme_table = "\n".join(lines)

    # Mechanism histogram (EXTEND-70: mechanism | cases | PASS | WORKING |
    # P+W %), from each row's PERSISTED mechanism (s27-73 — recomputable
    # from the extract alone).
    mech: dict[str, dict[str, int]] = {}
    for rec in records:
        if _is_skip(rec):
            continue
        mechanism = rec.get("mechanism") or "(unclassified)"
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
        if _is_skip(rec):
            continue
        if rec.get("verdict") in ("ESCALATE", "ESCALATE_TOOLCHAIN"):
            continue
        mechs = set()
        for prov in (rec.get("provenance_mix") or {}):
            mechs.update(m for m in prov.split("+") if m)
        if not mechs:
            j = rec.get("mechanism")
            if j and not j.startswith("("):
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
            "accepted": accepted,
            "pass_pct": round(100 * passes / total, 1) if total else 0,
            "adj_pct": round(100 * passes / denom_adj, 1) if denom_adj else 0,
            "pw_adj_pct": round(
                100 * (passes + working) / denom_adj, 1) if denom_adj else 0,
            "by_language": by_language,
            "mechanism_histogram": histogram,
            "mechanism_participation": participation,
            "readme_table": readme_table,
        },
        # caller completes: mechanism_commit, state, command_template,
        # ran, verification, outcome_summary
    }
    if delta is not None:
        meta["recount"]["delta_pw_adj_pct"] = delta
        meta["recount"]["delta_convention"] = (
            "difference of unrounded P+W adj values, rounded once")
    # s27-73 (rerun hygiene): merge with any existing meta — the caller
    # completes mechanism_commit/state/outcome_summary, and a recount
    # must not silently erase them.
    meta_path = out_dir / "meta.json"
    if meta_path.exists():
        try:
            prior = json.loads(meta_path.read_text())
            for k, v in prior.items():
                meta.setdefault(k, v)
        except json.JSONDecodeError:
            pass
    # Completeness warning BEFORE the README points readers here: s27's
    # meta was never completed and a published "full list is in
    # docs/results/s27/meta.json" pointer was false (found 2026-09-18).
    missing = [f for f in ("description", "mechanism_commit", "state",
                           "command_template", "ran", "verification",
                           "outcome_summary") if not meta.get(f)]
    if missing:
        print(f"meta.json INCOMPLETE — missing: {', '.join(missing)} "
              f"(complete before the README references this file)")
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps({k: v for k, v in meta["recount"].items()
                      if k != "readme_table"}, indent=1))
    print("\nREADME table (copy-paste):")
    print(readme_table)


if __name__ == "__main__":
    main()
