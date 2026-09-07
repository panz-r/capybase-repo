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
    wt = Path(tempfile.mkdtemp(prefix="capy-scen-"))
    branch = f"capy-scen-{uuid.uuid4().hex[:8]}"
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
        engine = ResolutionEngine(cfg.model, client=client)
        # Per-scenario wall budget scales with step count: each replayed
        # stop carries its own resolution rounds (~60-120s LLM worst case);
        # a flat 900s cap starved 13-step scenarios (duckdb-0003's timeout
        # was budget, not resolver). Base 600s + 240s/step, capped at 2h.
        n_steps = max(1, len(sc.get("conflict_steps", [])))
        cfg.policy.max_wall_time_per_file_seconds = min(600 + 240 * n_steps, 7200)
        orch = Orchestrator(cfg, repo=str(wt), resolution_engine=engine,
                            out=lambda *a, **k: None)
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
        touched_r = subprocess.run(
            ["git", "-C", str(clone), "diff", "--name-only",
             sc["merge_base_oid"], sc["source_tip_oid"]],
            capture_output=True, timeout=120)
        touched_r.stdout = touched_r.stdout.decode("utf-8", "replace")
        scored = sorted(conflicted | (
            set(touched_r.stdout.splitlines()) if touched_r.returncode == 0
            else set()))
        results = []
        for path in scored:
            final_p = _git(wt, "show", f":{path}", check=False)
            if final_p.returncode != 0:
                final = ""
                try:
                    final = (wt / path).read_text(errors="replace")
                except OSError:
                    pass
            else:
                final = final_p.stdout
            oracle_r = subprocess.run(
                ["git", "-C", str(clone), "show", f'{sc["merge_oid"]}:{path}'],
                capture_output=True)
            oracle_r.stdout = oracle_r.stdout.decode("utf-8", "replace")
            oracle_r.stderr = oracle_r.stderr.decode("utf-8", "replace")
            oracle = oracle_r.stdout if oracle_r.returncode == 0 else ""
            if oracle_r.returncode != 0 and not oracle:
                # ABSENT at the oracle. Two sub-cases:
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
            compiles = (_py_compiles(final) if lang == "python"
                        else True)  # other languages: brace gate deferred
            ok = bool(final.strip()) and not markers and sim >= 0.90 and compiles
            results.append({"path": path, "sim": round(sim, 3),
                            "markers": markers, "ok": ok})
        row["files_detail"] = results
        scored = [r_ for r_ in results if r_["ok"] is not None]
        n_ok = sum(1 for r_ in scored if r_["ok"])
        absent = [r_ for r_ in results if r_.get("absent_at_oracle")]
        row["absent_at_oracle"] = len(absent)
        row["absent_kept_in_replay"] = sum(
            1 for r_ in absent if r_.get("present_in_replay"))
        if row["escalated"]:
            row["verdict"] = "ESCALATE"
        elif scored and n_ok == len(scored):
            row["verdict"] = "PASS"
        elif n_ok > 0:
            row["verdict"] = "PARTIAL"
        else:
            row["verdict"] = "ORACLE_DIVERGENT"
        row["files_ok"] = f"{n_ok}/{len(scored)}"
        row["files_ok"] = f"{n_ok}/{len(results)}"
        # Preserve the session artifacts (journal, prompts, responses)
        # BEFORE the finally-block removes the worktree.
        if flights_dir is not None and row.get("session_id"):
            src = getattr(orch.paths, "root", None)
            if src is not None and Path(src).exists():
                dest = Path(flights_dir) / "flights" / sc["id"] / row["session_id"]
                shutil.copytree(src, dest, dirs_exist_ok=True)
        return row
    finally:
        _git(clone, "worktree", "remove", "--force", str(wt), check=False)
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
    return {".rs": "rust", ".py": "python", ".pyx": "python",
            ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp"}.get(sfx, "unknown")


_PROVIDER_NAME: str | None = None


def main() -> int:
    global _PROVIDER_NAME
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider")
    ap.add_argument("--scenario", help="exact scenario id")
    ap.add_argument("--dataset", help="run all scenarios of a dataset")
    ap.add_argument("--list", action="store_true")
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
    sel = []
    for f in scenarios:
        d = json.loads(f.read_text())
        if args.scenario and d["id"] != args.scenario:
            continue
        if args.dataset and d["dataset"] != args.dataset:
            continue
        if not d.get("merge_oid"):
            continue  # no oracle — can't verdict
        if d.get("inner_merges_in_source", 0) > 0 and not args.include_inner_merges:
            continue  # linear replay drops inner-merge resolutions (tikv-0001)
        sel.append(d)
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
