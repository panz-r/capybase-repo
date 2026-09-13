#!/usr/bin/env python3
"""Live evaluation of multi-commit rebase scenarios (sprint-27 corpus format v2).

The scenario corpus (extracted-testdata/rebase-scenarios/, mined by
mine_rebase_scenarios.py) carries: source commits, target tip, merge base,
per-step conflict tuples, and the human merge commit M (the oracle). This
script drives the REAL orchestrator through the scenario:

1. A worktree on the scenario's clone at target_tip; branch; rebase
   --onto target base source → stops at the first conflicted commit.
2. Orchestrator.run() resolves the conflict and continues the rebase
   (its own step loop drives subsequent stops).
3. Verdict per conflicted file: the final worktree content vs the human
   merge M's content (token jaccard), marker-free + language gate.
   Scenario PASS = every conflicted file passes.

Usage:
    .venv/bin/python scripts/live_eval_scenarios.py --provider NAME \
        --scenario polars-history-rebase-0003 [--dataset ds] [--list] \
        [--out results.json] [--preserve-flights DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

SCENARIO_DIR = Path(__file__).resolve().parent.parent / "extracted-testdata" / "rebase-scenarios"
CLONES_NEW = Path("/var/tmp/capybase-live/s27-scenarios/external-datasets")
CLONES_REPO = Path(__file__).resolve().parent.parent / "external-datasets"
_SUBDIR = {"tikv": "tikv", "polars": "polars", "cython": "cython",
           "scikit-learn": "scikit-learn", "php": "php-src", "libuv": "libuv",
           "duckdb": "duckdb", "prusaslicer": "prusaslicer"}

# Generator-output basename pattern (S27-57/60; module-scope since s27-71
# so smoke() asserts the PRODUCTION pattern instead of a diverging inline
# copy — the copy had already silently kept the pre-s27-67 dead
# `/generated_` alternative). Census: every corpus match (12 paths,
# duckdb/cython families incl. two hand-written generated-COLUMN feature
# tests) is oracle-equal-or-inert — 12/12 benign, s27-71.
_GEN_OUTPUT = re.compile(
    r"(compiled_grammar|inlined_grammar|\.pb\.cc|\.pb\.h|"
    r"\.generated\.|\.tab\.c|\.yy\.c|"
    r"transform_generated_|_generated\.|generated_)",
    re.IGNORECASE)


def _is_partial(clone: Path) -> bool:
    """True when the clone is blob-filtered (promisor remote configured).

    Scenario runs WALK HISTORY (git log --all, --find-object) — on a
    partial clone every missing blob is an on-demand network fetch, and
    the first orchestrator run hangs for many minutes mid-extraction.
    Full-fetch the clone once (git fetch origin --refetch) before running.
    """
    r = subprocess.run(["git", "-C", str(clone), "config", "--get",
                        "remote.origin.promisor"], capture_output=True, text=True)
    return r.stdout.strip() == "true"


def _clone_for(dataset: str) -> Path | None:
    stem = dataset.replace("-history", "")
    if stem in _SUBDIR:
        c = CLONES_NEW / _SUBDIR[stem]
        return c if c.exists() else None
    c = CLONES_REPO / stem
    return c if c.exists() else None


def _git(wt: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    # Binary-safe: touched-file diffs can carry non-UTF-8 bytes; decode
    # with replacement so the verdict path never dies on them.
    r = subprocess.run(["git", "-C", str(wt), *args], capture_output=True)
    r.stdout = r.stdout.decode("utf-8", "replace")
    r.stderr = r.stderr.decode("utf-8", "replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {args[:3]} failed: {r.stderr.strip()[:200]}")
    return r


def _token_jaccard(a: str, b: str) -> float:
    ta, tb = set(a.split()), set(b.split())
    return len(ta & tb) / len(ta | tb) if (ta | tb) else 1.0


def _py_compiles(content: str) -> bool:
    if not content.strip():
        return True
    import ast as _ast
    try:
        _ast.parse(content)
        return True
    except (SyntaxError, ValueError):
        return False


def classify_stop_cascade(results: list, *, escalated: bool) -> int:
    """Flag files still carrying markers on an ESCALATE (s27-61): they were
    never resolved — the replay stopped before their commits replayed, so
    their 0.0 sims measure the STOP, not the resolver. Mutates the detail
    dicts (sets ``stop_cascade``) and returns the count."""
    n = 0
    if escalated:
        for r_ in results:
            if r_.get("ok") is False and r_.get("markers"):
                r_["stop_cascade"] = True
                n += 1
    return n


def _journal_counters(journal_path) -> dict:
    """Cost + mechanism accounting from a session's journal (s27-61).

    One pass over the JSONL (the Journal appends per-event, flushed): the
    number of LLM generations (``candidate_generated``) and the acceptance
    count per mechanism (``candidate_accepted`` payload ``via``). Malformed
    or trailing-partial lines are skipped — counters are advisory.
    """
    out = {"llm_calls": 0, "mechanism_accepts": {}}
    try:
        with open(journal_path, encoding="utf-8") as fh:
            events = []
            for line in fh:
                try:
                    e = json.loads(line)
                except Exception:  # noqa: BLE001 — advisory
                    continue
                events.append(e)
    except OSError:
        return out
    # s27-72 (sixth pass): a whole-file takeover supersedes earlier per-unit
    # accepts — both journaled candidate_accepted events; subtract the
    # superseded candidate ids or every takeover double-counts.
    superseded: set[str] = set()
    for e in events:
        if e.get("event_type") == "outcomes_superseded":
            superseded.update(
                (e.get("payload") or {}).get("candidate_ids") or [])
    for e in events:
        t = e.get("event_type")
        if t == "candidate_generated":
            out["llm_calls"] += 1
        elif t == "candidate_accepted":
            payload = e.get("payload") or {}
            if payload.get("candidate_id") in superseded:
                continue
            via = payload.get("via") or "?"
            out["mechanism_accepts"][via] = (
                out["mechanism_accepts"].get(via, 0) + 1)
    return out


def _chain_verdict(escalated: bool, holes: bool, scored: list,
                   n_ok: int, absent: list, kept_in_replay: int) -> str:
    """The per-scenario verdict chain (s27-71: extracted for testability).

    PASS requires all SCORED rows ok (absent rows are excluded by design);
    a scenario whose every row is absent-at-oracle with the replay
    agreeing on every deletion has nothing to score and nothing divergent
    — ALL_ABSENT, not the old fall-through ORACLE_DIVERGENT (a false FAIL
    no resolver quality could avoid)."""
    if escalated:
        return "ESCALATE"
    if holes:
        return "ORACLE_HOLE"
    if scored and n_ok == len(scored):
        return "PASS"
    if n_ok > 0:
        return "PARTIAL"
    if not scored and absent and not kept_in_replay:
        return "ALL_ABSENT"
    return "ORACLE_DIVERGENT"


def run_scenario(sc: dict, client, *, flights_dir: Path | None = None) -> dict:
    """Drive one scenario; return the result row."""
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator
    from capybase.resolution_engine import ResolutionEngine
    from capybase.adapters.parsers import contains_markers
    import live_eval_realworld as L

    clone = _clone_for(sc["dataset"])
    if clone is not None and _is_partial(clone):
        raise RuntimeError(
            f"{clone} is blob-filtered; full-fetch it first: "
            f"git -C {clone} fetch origin --refetch "
            "(history-walking hangs on on-demand blob fetches)")
    t0 = time.time()
    row = {"id": sc["id"], "dataset": sc["dataset"],
           "steps": len(sc.get("conflict_steps", [])),
           "files": len({s["path"] for s in sc.get("conflict_steps", [])})}
    _flights_copied = False
    orch = None
    wt = Path(tempfile.mkdtemp(prefix="capy-scen-"))
    branch = f"capy-scen-{uuid.uuid4().hex[:8]}"
    # Observability line (s27-61): the live journal lives INSIDE this
    # worktree (<wt>/.rebase-agent/sessions/<id>/journal.jsonl) and is only
    # copied to the flights dir AFTER completion — watching the flights dir
    # mid-run shows nothing (the s27-47 forensics trap). The batch runner's
    # watchdog parses this line to bind to the scenario's journal; humans
    # debugging a live run get the path in the log.
    print(f"  worktree={wt}", flush=True)
    try:
        # 1) worktree at target_tip + branch; drive the --onto rebase
        _git(clone, "worktree", "add", "--detach", str(wt), sc["target_tip_oid"])
        _git(wt, "checkout", "-q", "-b", branch)
        r = _git(wt, "rebase", "--onto", sc["target_tip_oid"],
                 sc["merge_base_oid"], sc["source_tip_oid"], check=False)
        if r.returncode == 0:
            row["verdict"] = "SAFE_SKIP"
            row["reason"] = "rebase completed cleanly (no conflict)"
            return row
        # 2) orchestrator drives the stopped rebase
        L._PROVIDER = L.resolve_provider(provider=_PROVIDER_NAME) \
            if _PROVIDER_NAME else L._PROVIDER
        case_lang = {"rust": "rust", "python": "python", "c": "c",
                     "cpp": "cpp"}.get(
            sc.get("language")
            or _lang_of_dataset(sc["dataset"]), "rust")
        cfg = L._config_for(type("C", (), {
            "path": "scenario.rs", "language": case_lang,
            "marker_original": "", "id": sc["id"],
            "dataset": sc["dataset"],
        })(), has_crate=True)
        # _config_for's python branch materializes a per-CASE test gate
        # ("python3 -m py_compile {case.path}") — meaningful for the
        # single-file corpus where one case is one file, but in a replay
        # scenario the stub path ("scenario.rs") doesn't exist and the real
        # conflict paths vary per step. The broken command sat latent in
        # python-dataset scenarios until a unit reached the f1 takeover's
        # verify_file, where the FileNotFoundError read as a HARD build
        # failure and vetoed every takeover (cython-0020's 262s escalate:
        # "whole-file validation failed ... No such file or directory:
        # 'scenario.rs'"). The scenario harness judges outcomes itself
        # (token-jaccard vs the merge oracle); neutralize the gate. The C
        # datasets already fall through to "true" here (their command tables
        # are keyed by single-file case ids).
        cfg.tests.pre_continue = "true"
        cfg.tests.final = "true"
        engine = ResolutionEngine(cfg.model, client=client)
        # Per-scenario wall budget scales with step count: each replayed
        # stop carries its own resolution rounds (~60-120s LLM worst case);
        # a flat 900s cap starved 13-step scenarios (duckdb-0003's timeout
        # was budget, not resolver). Base 600s + 240s/step, capped at 2h.
        n_steps = max(1, len(sc.get("conflict_steps", [])))
        cfg.policy.max_wall_time_per_file_seconds = min(600 + 240 * n_steps, 7200)
        # Race seeds (S27-26): pre-compute the cross-file move evidence
        # the per-unit cascade cannot see — for each conflict path, is the
        # replayed block's content present in the SOURCE TIP's tree (the
        # moved def's new home)? Registered on the orchestrator; the
        # dormant def_site_race mechanism activates only on these paths.
        from capybase.def_site_race import resolve_def_site_race as _dsr
        race_paths: dict[str, dict] = {}
        for stepinfo in sc.get("conflict_steps", []):
            sp = stepinfo["path"]
            if sp in race_paths:
                continue
            try:
                rr = _dsr(stepinfo["marker_text"])
            except ValueError:
                continue
            if not rr.resolved:
                continue
            ps = subprocess.run(
                ["git", "-C", str(clone), "show",
                 f'{sc["source_tip_oid"]}:{sp}'],
                capture_output=True, timeout=30)
            if ps.returncode != 0:
                continue
            src_lines = {l.strip() for l in
                         ps.stdout.decode("utf-8", "replace").splitlines()
                         if l.strip()}
            cand_lines = {l.strip() for l in rr.text.splitlines()
                          if l.strip()}
            if cand_lines and cand_lines <= src_lines:
                race_paths[sp] = {"content": rr.content,
                                  "source_tip": sc["source_tip_oid"]}
        orch = Orchestrator(cfg, repo=str(wt), resolution_engine=engine,
                            out=lambda *a, **k: None)
        print(f"  session={orch.session_id} journal={orch.paths.journal}",
              flush=True)
        if race_paths:
            orch._race_step_paths = race_paths
            # activate the dormant mechanism — the evidence gate now does
            # the real filtering (per-unit mode stays protected).
            cfg.future.enable_def_site_race = True
        # Convergence seeds (S27-54): paths where the TARGET TIP and SOURCE
        # TIP carry the same blob — the replay's conflict on them is
        # transient churn over a decided final state. Census: 1080/1080
        # such conflicts have the oracle == the tips' content (the duckdb
        # family carries ~60 per scenario). Registered like the race seeds;
        # the mechanism writes the converged content as the whole file,
        # validated through the standard gates.
        conv_seeds: dict[str, str] = {}
        # Generator-output seeds (S27-57): for GENERATOR-OUTPUT
        # paths (the strict pattern — compiled/inlined grammar, pb.cc/h,
        # .generated, parser tables; NOT lockfiles, which package managers
        # merge substantively — the mixed 20/79 census included those), the
        # human policy is 30/30 take-the-source-side verbatim (the duckdb
        # grammar family; compiled_grammar.cpp blocked 0004/0005/0007/0010
        # as model refusals). The seed is the SOURCE TIP's blob — computed
        # from tips alone, census-validated, no oracle access.
        # (Pattern hoisted to module scope s27-71 — see _GEN_OUTPUT.)
        # Seed candidates = every path the replay can touch (the source
        # range's diff), not just the miner's conflict_steps — EMERGENT
        # conflicts (compiled_grammar.cpp blocked four duckdb runs yet
        # appears in no conflict_steps list; its conflict arises from
        # earlier resolutions, not a replayed commit's own diff) would
        # otherwise be invisible to the seeding.
        try:
            # The LOG-UNION of per-commit touches (not the net diff — a
            # file changed AND reverted inside the range nets to zero and
            # vanishes from `diff --name-only`, yet its replay still
            # conflicts; compiled_grammar.cpp is exactly this shape).
            _touched_r = subprocess.run(
                ["git", "-C", str(clone), "log", "--format=", "--name-only",
                 f'{sc["merge_base_oid"]}..{sc["source_tip_oid"]}'],
                capture_output=True, timeout=180)
            _seed_paths = sorted(
                {st["path"] for st in sc.get("conflict_steps", [])}
                | ({l.strip() for l
                    in _touched_r.stdout.decode("utf-8", "replace").splitlines()
                    if l.strip()}
                   if _touched_r.returncode == 0 else set()))
        except Exception:  # noqa: BLE001 — fall back to the listed conflicts
            _seed_paths = sorted(
                {st["path"] for st in sc.get("conflict_steps", [])})
        try:
            for sp in _seed_paths:
                if sp in conv_seeds:
                    continue
                r_t = subprocess.run(
                    ["git", "-C", str(clone), "rev-parse",
                     f'{sc["target_tip_oid"]}:{sp}'],
                    capture_output=True, timeout=30)
                r_s = subprocess.run(
                    ["git", "-C", str(clone), "rev-parse",
                     f'{sc["source_tip_oid"]}:{sp}'],
                    capture_output=True, timeout=30)
                if r_t.returncode != 0 and r_s.returncode != 0:
                    # TRANSIENT FILE: absent at both tips — born and deleted
                    # inside the replay window. The final state is deletion
                    # (census: 499/499 oracle agreement). Empty string is the
                    # delete-seed.
                    conv_seeds[sp] = ""
                    continue
                if (r_t.returncode != 0 or r_s.returncode != 0
                        or r_t.stdout.strip() != r_s.stdout.strip()):
                    # Generator-output exception: when the sides disagree,
                    # the regenerated (source) version wins — 30/30 census.
                    if (r_s.returncode == 0
                            and _GEN_OUTPUT.search(sp.rsplit("/", 1)[-1])):
                        r_c = subprocess.run(
                            ["git", "-C", str(clone), "show",
                             f'{sc["source_tip_oid"]}:{sp}'],
                            capture_output=True, timeout=30)
                        if r_c.returncode == 0:
                            conv_seeds[sp] = r_c.stdout.decode(
                                "utf-8", "replace")
                    continue
                try:
                    r_c = subprocess.run(
                        ["git", "-C", str(clone), "show",
                         f'{sc["target_tip_oid"]}:{sp}'],
                        capture_output=True, timeout=30)
                except subprocess.TimeoutExpired:
                    continue  # one slow read must not discard other seeds
                if r_c.returncode == 0:
                    conv_seeds[sp] = r_c.stdout.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 — seeds are best-effort
            pass
        if conv_seeds:
            orch._convergence_seeds = conv_seeds
            cfg.future.enable_convergence_seed = True
        step = orch.run()
        row["escalated"] = bool(getattr(step, "escalated", False))
        row["reason"] = (step.reason or "")[:200]
        row["session_id"] = getattr(orch, "session_id", "")
        # 3) per-file verdicts vs the human merge M — CONFLICTED files
        # plus the replayed commits' OTHER touched files (the tikv-0001
        # lesson: wrongness outside the conflict span is invisible to a
        # conflicted-only verdict; the resurrection scan protects the
        # resolver, this widens the MEASUREMENT to match).
        conflicted = {s["path"] for s in sc["conflict_steps"]}
        # s27-72 (sixth pass): score the SAME universe the seeds cover —
        # the LOG-UNION of per-commit touches (computed above for the seed
        # registration), not the net diff. A changed-and-reverted file nets
        # to zero in the diff yet can emerge as a conflict and be SEEDED;
        # scoring the net diff left it unscored — a wrong seed write was
        # invisible (a false-PASS class).
        scored = sorted(conflicted | set(_seed_paths or []))
        results = []
        # One lazy per-scenario refetch when an oracle read hits an
        # object-store hole (side-branch merge oids no refspec covers).
        rescued_this_scenario = False
        for path in scored:
            # Read the WORKTREE file, not `git show :path` (the INDEX).
            # After the orchestrator's rebase completes, index == HEAD, so
            # this is equivalent there — but when a late replay stop left
            # the index with conflict stages, `:path` returns the MERGED-
            # WITH-MARKERS stage-0 blob and marks clean files as
            # marker-laden (scikit-0016: 9 sim-1.0 'failures' whose
            # content matched M exactly). The worktree file is the truth
            # the user would see.
            # SYMLINKS compare like-for-like: `git show` on a symlink blob
            # (mode 120000) returns the link TARGET STRING, while read_text
            # FOLLOWS the link to the destination content — disjoint token
            # sets, sim 0.0 on perfectly correct merges (serde's LICENSE/
            # src symlinks into precomp/). When the worktree entry is a
            # symlink, compare link targets (the oracle side is already the
            # target string via git show).
            import os as _os
            _wp = wt / path
            try:
                if _os.path.islink(_wp):
                    final = _os.readlink(_wp)
                else:
                    final = _wp.read_text(encoding="utf-8",
                                          errors="replace")
            except OSError:
                final = ""
            oracle_r = subprocess.run(
                ["git", "-C", str(clone), "show", f'{sc["merge_oid"]}:{path}'],
                capture_output=True)
            oracle_r.stdout = oracle_r.stdout.decode("utf-8", "replace")
            oracle_r.stderr = oracle_r.stderr.decode("utf-8", "replace")
            oracle = oracle_r.stdout if oracle_r.returncode == 0 else ""
            if oracle_r.returncode != 0 and "bad object" in (
                    oracle_r.stderr or "") and not rescued_this_scenario:
                # Self-heal once per scenario: the merge oid may live on a
                # side branch no refspec covers, so even a "full" clone can
                # miss its snapshot blobs (tikv-0004). One targeted refetch
                # of the merge oid fills every hole in its tree.
                rescued_this_scenario = True
                try:
                    subprocess.run(
                        ["git", "-C", str(clone), "fetch", "--refetch",
                         "origin", sc["merge_oid"]],
                        capture_output=True, timeout=300)
                    oracle_r = subprocess.run(
                        ["git", "-C", str(clone), "show",
                         f'{sc["merge_oid"]}:{path}'], capture_output=True)
                    oracle_r.stdout = oracle_r.stdout.decode("utf-8", "replace")
                    oracle_r.stderr = oracle_r.stderr.decode("utf-8", "replace")
                    oracle = oracle_r.stdout if oracle_r.returncode == 0 else ""
                except Exception:  # noqa: BLE001 — rescue is best-effort
                    pass
            if oracle_r.returncode != 0 and "bad object" in (
                    oracle_r.stderr or ""):
                # OBJECT-STORE HOLE, not absence (s27-44: tikv-0004's three
                # "absent" files were blob-filter holes — the oracle existed
                # and scored 1.000/0.954/0.874 once refetched). "bad object"
                # means the clone lacks the blob; genuine absence reads
                # "path ... does not exist in <sha>". A hole is a
                # MEASUREMENT error: mark it, never score it as absent
                # (absent flatters the verdict by skipping the file).
                print(f"  [warn] oracle blob hole: {path} at "
                      f"{sc['merge_oid'][:10]} — run --prepare / refetch "
                      f"the merge oid", flush=True)
                results.append({"path": path, "sim": None,
                                "oracle_read_error": True,
                                "ok": None})
                continue
            if (oracle_r.returncode != 0 and not oracle) or (
                    oracle_r.returncode == 0 and not oracle.strip()):
                # ABSENT or EMPTY-BLOB at the oracle: the human merge
                # removed (or emptied — prusaslicer-0013's build.yml is
                # git's canonical empty blob) this file. Judging content
                # against an empty oracle is meaningless; treat as the
                # structural absent case. (The empty blob is a real merge
                # artifact class: resolve-by-emptying.)
                # ABSENT sub-cases:
                # - final ALSO absent: the replay agreed with M (both
                #   dropped it) — consistent, informational only.
                # - final PRESENT: the replay kept a file M removed —
                #   the tikv-0001 signature (structural, unattributable
                #   to the resolver — no conflict named this path — but
                #   the resurrection scan's catch territory).
                # Both are excluded from the resolver-quality count.
                results.append({"path": path, "sim": None,
                                "absent_at_oracle": True,
                                "present_in_replay": bool(final.strip()),
                                "ok": None})
                continue
            sim = _token_jaccard(final, oracle) if (final or oracle) else 0.0
            markers = contains_markers(final) if final else True
            lang = _lang_of_path(path)
            # Parse gate (python only): vacuous when the ORACLE itself
            # fails py3 (language-era file — scikit-0016's parse_path.py
            # is a py2 tutorial script the human merge kept verbatim).
            # Same no-worse-than-before doctrine as the unit gates.
            compiles = True
            if lang == "python":
                compiles = (_py_compiles(final)
                            or not _py_compiles(oracle))
            ok = bool(final.strip()) and not markers and sim >= 0.90 and compiles
            results.append({"path": path, "sim": round(sim, 3),
                            "markers": markers, "ok": ok})
        row["files_detail"] = results
        row["stop_cascade_misses"] = classify_stop_cascade(
            results, escalated=bool(row.get("escalated")))
        scored = [r_ for r_ in results if r_["ok"] is not None]
        n_ok = sum(1 for r_ in scored if r_["ok"])
        absent = [r_ for r_ in results if r_.get("absent_at_oracle")]
        row["absent_at_oracle"] = len(absent)
        row["absent_kept_in_replay"] = sum(
            1 for r_ in absent if r_.get("present_in_replay"))
        holes = [r_ for r_ in results if r_.get("oracle_read_error")]
        if holes:
            # Measurement holes must be visible in the row itself, not just
            # the log — a PASS with unscored files is not an honest PASS.
            row["oracle_read_errors"] = len(holes)
        row["verdict"] = _chain_verdict(
            bool(row.get("escalated")), bool(holes), scored, n_ok,
            absent, row.get("absent_kept_in_replay") or 0)
        if row["verdict"] == "ALL_ABSENT":
            row["reason"] = ("all touched paths absent at the oracle; "
                             "replay agreed on every deletion")
        # PASS's criterion is all-SCORED-ok; the denominator shown is all
        # files (scored + absent + holes) so the row reads consistently.
        row["files_ok"] = f"{n_ok}/{len(results)}"
        # Cost + mechanism accounting (s27-61): the seeds' economics story
        # (27 LLM calls for 86 conflicts) and per-arm acceptance counts
        # used to take a journal grep per sweep — read once here instead.
        counters = _journal_counters(orch.paths.journal)
        row["llm_calls"] = counters["llm_calls"]
        row["mechanism_accepts"] = counters["mechanism_accepts"]
        # Preserve the session artifacts (journal, prompts, responses)
        # BEFORE the finally-block removes the worktree.
        if flights_dir is not None and row.get("session_id"):
            src = getattr(orch.paths, "root", None)
            if src is not None and Path(src).exists():
                dest = Path(flights_dir) / "flights" / sc["id"] / row["session_id"]
                try:
                    shutil.copytree(src, dest, dirs_exist_ok=True)
                    _flights_copied = True
                except OSError as exc:
                    # s27-72 (sixth pass): the copy is forensic, the verdict
                    # is the product — a disk-full copy error must not
                    # discard a completed result row (hours of spend).
                    print(f"[flights] copy FAILED ({exc}); verdict kept, "
                          f"journal lost with the worktree", flush=True)
        return row
    finally:
        if os.environ.get("CAPYBASE_KEEP_SCENARIO_WORKTREE"):
            # Debug: leave the worktree and log HEAD-vs-worktree for every
            # failed scored file — separates "git dropped it from HEAD"
            # (replay wrong) from "worktree never materialized it"
            # (checkout/index inconsistency).
            print(f"[keep-worktree] {wt}", flush=True)
            for f in row.get("files_detail", []) or []:
                if f.get("ok") is False:
                    path = f["path"]
                    head_e = _git(wt, "cat-file", "-e", f"HEAD:{path}",
                                  check=False).returncode == 0
                    print(f"[keep-worktree] {path} HEAD={head_e} "
                          f"worktree={(wt / path).exists()}", flush=True)
        else:
            # s27-72 (sixth pass): a crash path (orch.run() raising) skips
            # the success-point copy — preserve the journal into flights
            # BEFORE the worktree goes, or the crash leaves zero forensics
            # (the runner's kill-preservation only covers watchdog kills).
            if (orch is not None and flights_dir is not None
                    and not _flights_copied
                    and getattr(orch, "session_id", None)):
                _src = getattr(getattr(orch, "paths", None), "root", None)
                if _src is not None and Path(_src).exists():
                    _dest = (Path(flights_dir) / "flights" / sc["id"] /
                             f"{orch.session_id}-crashed")
                    try:
                        shutil.copytree(_src, _dest, dirs_exist_ok=True)
                        print(f"[flights] crashed session preserved -> "
                              f"{_dest}", flush=True)
                    except OSError:
                        pass
            _git(clone, "worktree", "remove", "--force", str(wt), check=False)
        _git(clone, "worktree", "prune", check=False)
        _git(clone, "branch", "-D", branch, check=False)
        row["elapsed"] = round(time.time() - t0, 1)


def _lang_of_dataset(ds: str) -> str:
    return {"tikv": "rust", "polars": "rust", "cython": "python",
            "scikit-learn": "python", "php": "c", "libuv": "c",
            "duckdb": "cpp", "prusaslicer": "cpp",
            "axum": "rust", "clap": "rust", "sea-orm": "rust",
            "serde": "rust", "tokio": "rust", "pydantic": "python"}.get(
        ds.replace("-history", ""), "rust")


def _lang_of_path(path: str) -> str:
    sfx = Path(path).suffix
    # .pyx is Cython, not Python — py_compile fails on its syntax (the
    # scikit-0016 .pyx files at sim 1.0). Its own tier: no parse gate.
    return {".rs": "rust", ".py": "python", ".pyx": "cython",
            ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp"}.get(sfx, "unknown")


_PROVIDER_NAME: str | None = None


def select_scenarios(scenario_ids: list[str], dataset: str | None,
                     *, include_inner_merges: bool = False) -> list[dict]:
    """The shared selection: load the corpus, filter by ids/dataset, drop
    oracle-less and (by default) inner-merge scenarios. Used by main() and
    by run_scenario_batch.py (s27-61) so both select identically.
    """
    sel = []
    idset = set(scenario_ids)
    for f in sorted(SCENARIO_DIR.glob("*-rebase-*.json")):
        d = json.loads(f.read_text())
        if idset and d["id"] not in idset:
            continue
        if dataset and d["dataset"] != dataset:
            continue
        if not d.get("merge_oid"):
            continue  # no oracle — can't verdict
        if d.get("inner_merges_in_source", 0) > 0 and not include_inner_merges:
            continue  # linear replay drops inner-merge resolutions (tikv-0001)
        sel.append(d)
    return sel


def smoke() -> list[str]:
    """Config self-test (s27-61 item 7): pure assertions, no git, no
    network. Regression armor for the stub-path-leak class (s27-50:
    _config_for's python branch materialized "python3 -m py_compile
    scenario.rs" from the harness's stub case — latent until a unit hit
    the f1 takeover's verify_file). Returns the list of failures; empty
    means healthy.
    """
    failures = []
    import live_eval_realworld as L
    from types import SimpleNamespace
    # Provider resolution is a LOCAL config read (no network) but requires
    # a configured provider; without one the stub-path assertions are
    # SKIPPED loudly (hermetic test environments have none) and the pattern
    # assertions still run.
    try:
        L._PROVIDER = L.resolve_provider(provider=_PROVIDER_NAME)
        provider_ok = True
    except Exception:
        provider_ok = False
        print("smoke: no provider configured — stub-path assertions SKIPPED")
    if provider_ok:
        # s27-71: the old check overwrote tests.pre_continue/final and then
        # inspected the overwritten values — a pure tautology (the s27-50
        # armor was dead). The REAL contract: the raw per-case gate may
        # materialize the stub path in tests.pre_continue/final (run_scenario
        # overwrites exactly those two for replays — the s27-50
        # neutralization), but the leak must be CONFINED to them — any OTHER
        # surface carrying the stub path would execute unneutralized in
        # replays.
        for lang in ("python", "rust", "c", "cpp"):
            cfg = L._config_for(SimpleNamespace(
                path="scenario.rs", language=lang, marker_original="",
                id="smoke", dataset="smoke-dataset"), has_crate=True)
            for section in ("tests",):
                obj = getattr(cfg, section, None)
                if obj is None:
                    continue
                for fname in getattr(obj, "model_fields", vars(obj)):
                    if fname.startswith("_"):
                        continue
                    try:
                        val = str(getattr(obj, fname, "") or "")
                    except Exception:  # noqa: BLE001
                        continue
                    if "scenario.rs" in val and fname not in (
                            "pre_continue", "final"):
                        failures.append(
                            f"{lang}: stub path leaked into "
                            f"{section}.{fname}: {val[:80]!r}")
            # (The s27-71 rewrite's neutralization-mirror assert was
            # removed s27-72: assign-then-assert is a tautology. The
            # confinement loop above is the armor; run_scenario's own
            # neutralization is pinned by the harness's live behavior.)
    # Generator-output seed pattern (s27-57/60): asserts the PRODUCTION
    # pattern (module-scope _GEN_OUTPUT, hoisted s27-71 — the old inline
    # copy had already diverged back to the dead `/generated_` form).
    # Census note (s27-71): every corpus match is oracle-equal-or-inert,
    # including the hand-written generated-COLUMN feature tests — the
    # bare `generated_` alternative deliberately stays (12/12 benign).
    expect_match = [
        "src/parser/peg/compiled_grammar.cpp",
        "src/parser/peg/transformer/transform_generated_trampoline.cpp",
        "proto/messages.pb.cc",
        "src/parser/peg/inlined_grammar.hpp",
        "src/settings/autogenerated_settings.cpp",
    ]
    expect_no_match = [
        "Cargo.lock",
        "package-lock.json",
        "src/main.cpp",
        "AUTHORS",
    ]
    for p in expect_match:
        if not _GEN_OUTPUT.search(p.rsplit("/", 1)[-1]):
            failures.append(f"generator pattern should match but doesn't: {p}")
    for p in expect_no_match:
        if _GEN_OUTPUT.search(p.rsplit("/", 1)[-1]):
            failures.append(f"generator pattern should NOT match but does: {p}")
    return failures


def main() -> int:
    global _PROVIDER_NAME
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider")
    ap.add_argument("--scenario", help="exact scenario id")
    ap.add_argument("--dataset", help="run all scenarios of a dataset")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--smoke", action="store_true",
                    help="config self-test (no git, no network): asserts no "
                         "stub-path leaks and the seed patterns' shape")
    ap.add_argument("--out", default="/tmp/scenario-results.json")
    ap.add_argument("--preserve-flights")
    ap.add_argument("--include-inner-merges", action="store_true",
                    help="include scenarios whose source range contains merge "
                         "commits — linear replay DROPS their resolutions, so "
                         "the oracle may be unreproducible (tikv-0001 class)")
    ap.add_argument("--prepare", action="store_true",
                    help="one-time full-fetch of the selected datasets' clones")
    args = ap.parse_args()
    _PROVIDER_NAME = args.provider

    scenarios = sorted(SCENARIO_DIR.glob("*-rebase-*.json"))
    if args.list:
        for f in scenarios:
            d = json.loads(f.read_text())
            print(f"{d['id']:40s} steps={len(d['conflict_steps']):3d} "
                  f"files={len({s['path'] for s in d['conflict_steps']}):3d} "
                  f"merge_oid={'Y' if d.get('merge_oid') else 'MISSING'}")
        return 0
    if args.smoke:
        fails = smoke()
        if fails:
            for f_ in fails:
                print(f"SMOKE-FAIL: {f_}")
            return 1
        print("smoke: all config assertions passed")
        return 0
    sel = select_scenarios(
        [args.scenario] if args.scenario else [], args.dataset,
        include_inner_merges=args.include_inner_merges)
    if args.prepare:
        for ds in sorted({d["dataset"] for d in sel}):
            prepare_clone(ds)
        return 0
    if not sel:
        print("no scenarios selected")
        return 1

    from capybase.adapters.llm_openai import OpenAICompatibleClient
    import live_eval_realworld as L
    L._PROVIDER = L.resolve_provider(provider=args.provider)
    # The client's ModelConfig comes from _config_for (provider-resolved:
    # endpoint + calibration) — the same path the realworld harness uses.
    from types import SimpleNamespace
    _probe_case = SimpleNamespace(path="scenario.rs", language="rust",
                                   marker_original="", id="probe")
    cfg0 = L._config_for(_probe_case)
    client = OpenAICompatibleClient(cfg0.model)

    results = []
    for sc in sel:
        print(f"[scenario] {sc['id']} ...", flush=True)
        row = run_scenario(sc, client, flights_dir=Path(args.preserve_flights) if args.preserve_flights else None)
        results.append(row)
        print(f"  {row.get('verdict')} {row.get('elapsed', '?')}s "
              f"files_ok={row.get('files_ok', '-')} {row.get('reason', '')[:60]}",
              flush=True)
    Path(args.out).write_text(json.dumps(results, indent=2))
    print(f"results -> {args.out}")
    return 0


def prepare_clone(dataset: str) -> None:
    """One-time: full-fetch a blob-filtered clone and remove the promisor
    config (history-walking scenarios need all blobs locally)."""
    clone = _clone_for(dataset)
    if clone is None or not _is_partial(clone):
        print(f"[prepare] {dataset}: already full")
        return
    print(f"[prepare] {dataset}: full-fetching {clone} (one-time)...")
    subprocess.run(["git", "-C", str(clone), "fetch", "origin", "--refetch",
                    "--no-tags"], check=True, timeout=3600)
    subprocess.run(["git", "-C", str(clone), "config", "--unset",
                    "remote.origin.promisor"], check=False)
    subprocess.run(["git", "-C", str(clone), "config", "--unset",
                    "remote.origin.partialclonefilter"], check=False)
    print(f"[prepare] {dataset}: full clone ready")


if __name__ == "__main__":
    sys.exit(main())
