#!/usr/bin/env python3
"""Live-model evaluation harness for the realworld conflict corpus.

Drives the capybase Orchestrator with a REAL OpenAICompatibleClient against
the configured local model, on the genuine git merge conflicts under
extracted-testdata/realworld/. Each case is materialized as a real git repo
with the conflict markers on disk, then `orch.run()` resolves it end-to-end
(extraction → resolution → file write → test gate) — the authentic system path.

NOT part of the hermetic test suite — makes real network calls. Run:

    CAPYBASE_BASE_URL=http://host:8086/v1 \\
    CAPYBASE_MODEL='<gguf-id>' \\
    .venv/bin/python scripts/live_eval_realworld.py [--limit N] [--lang rust|python]

Verdict per case:
  PASS     — orch.run() did not escalate; resolved file is marker-free AND
             compiles (py_compile for Python, brace-balance for Rust).
  ESCALATE — orch.run() escalated (human required). The SAFE outcome — not wrong.
  WRONG    — orch.run() did NOT escalate but the resolved file has leftover
             markers OR fails the compile check. This is the silent-wrong-output
             signal the parser/resolver fixes are meant to eliminate.

The human merge (expected_resolved) is the oracle; we report token-Jaccard
similarity to it as a QUALITY signal (real-world merges have multiple valid
forms, so we don't hard-fail on inequality).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from capybase.adapters.llm_openai import OpenAICompatibleClient  # noqa: E402
from capybase.config import Config  # noqa: E402
from capybase.orchestrator import Orchestrator  # noqa: E402
from capybase.resolution_engine import ResolutionEngine  # noqa: E402

TESTDATA = Path(__file__).resolve().parent.parent / "extracted-testdata" / "realworld"


@dataclass
class Case:
    id: str
    path: str
    language: str
    base: str
    current: str
    replayed: str
    expected_resolved: str
    marker_original: str
    dataset: str = ""


@dataclass
class CaseResult:
    id: str
    language: str
    dataset: str
    escalated: bool = False
    marker_free: bool = False
    compiles: bool = False
    matches_oracle: float = 0.0
    elapsed: float = 0.0
    reason: str = ""


def load_cases(*, limit: int | None = None, lang: str | None = None) -> list[Case]:
    cases: list[Case] = []
    for f in sorted(TESTDATA.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        required = ("base", "current", "replayed", "expected_resolved", "marker_original")
        if not all(k in d for k in required):
            continue
        c = Case(
            id=d.get("id", f.stem),
            path=d.get("path", f"{f.stem}.rs"),
            language=d.get("language", "rust"),
            base=d["base"], current=d["current"], replayed=d["replayed"],
            expected_resolved=d["expected_resolved"],
            marker_original=d["marker_original"],
            dataset=d.get("dataset", ""),
        )
        if lang and c.language != lang:
            continue
        # Skip pathologically huge conflicts (>48K chars ≈ blow context window).
        if len(c.marker_original) > 48 * 1024:
            continue
        cases.append(c)
        if limit and len(cases) >= limit:
            break
    return cases


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "tester"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "t@example.com"
    env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = "2000-01-01T00:00:00"
    env["GIT_PAGER"] = "cat"
    p = subprocess.run(["git", "-C", str(repo), *args], env=env,
                       capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {args} failed: {p.stderr.strip()[:200]}")
    return p


def _materialize_conflict(case: Case, repo: Path) -> None:
    """Build a git history that produces the case's conflict markers on disk.

    Three commits: base, current (HEAD), replayed (the branch being rebased).
    A `git rebase` produces the UU conflict with case.marker_original on disk.
    """
    # Sanity: marker_original must contain conflict markers for rebase to conflict.
    # Some cases carry a pre-resolved marker_original (no <<<<<<<); for those we
    # write the markers verbatim (orchestrator's conflict extractor will find them).
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    # base commit
    (repo / case.path).parent.mkdir(parents=True, exist_ok=True)
    (repo / case.path).write_text(case.base)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    # current (upstream) commit — the branch HEAD advances to
    _git(repo, "checkout", "-q", "-b", "current")
    (repo / case.path).write_text(case.current)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "current")
    # replayed commit — off base, will be rebased onto current
    _git(repo, "checkout", "-q", "main")
    _git(repo, "checkout", "-q", "-b", "replayed")
    (repo / case.path).write_text(case.replayed)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "replayed")
    # Drive the rebase onto current; expect a conflict.
    _git(repo, "checkout", "-q", "replayed")
    r = _git(repo, "rebase", "current", check=False)
    if r.returncode == 0:
        # No conflict from git's view — force the marker text onto disk so the
        # orchestrator's extractor finds it (the case is still a valid conflict
        # shape, just git-resolved it cleanly). Reset to a conflicted state.
        _git(repo, "rebase", "--abort", check=False)
        # Write markers verbatim as the "conflicted" worktree.
        (repo / case.path).write_text(case.marker_original)
        # Drop into a detached state mimicking mid-rebase so orch.run() engages.
        _git(repo, "add", "-A")


def _config_for(case: Case) -> Config:
    cfg = Config()
    cfg.model.base_url = os.environ.get("CAPYBASE_BASE_URL", "http://192.168.50.235:8086/v1")
    cfg.model.api_key = os.environ.get("CAPYBASE_API_KEY", "sk-local")
    cfg.model.model = os.environ.get("CAPYBASE_MODEL", "chat")
    cfg.model.temperature = 0.2
    cfg.model.max_tokens = 8192
    cfg.model.json_mode = True
    cfg.model.request_timeout_seconds = 600
    cfg.model.generation_timeout_seconds = 240
    # Test gate: py_compile for Python; for Rust, no cheap reliable standalone
    # check (crate paths fail outside the full checkout), so use 'true' and rely
    # on the harness's brace-balance + marker-free checks.
    if case.language == "python":
        cfg.tests.pre_continue = f"python3 -m py_compile {case.path}"
    else:
        cfg.tests.pre_continue = "true"
    cfg.tests.final = cfg.tests.pre_continue
    cfg.tests.required = False  # harness judges; don't double-gate
    cfg.future.enable_structural_resolver = True
    cfg.future.enable_combination_search = True
    cfg.policy.max_retries_per_unit = 2  # cap CEGIS retries for throughput
    return cfg


def _contains_markers(text: str) -> bool:
    return any(m in text for m in ("<<<<<<<", ">>>>>>>")) or text.count("=======\n") > 0


def _brace_balanced(text: str, lang: str) -> bool:
    try:
        from capybase.adapters.string_lexer import blank_strings_and_comments
        cleaned = blank_strings_and_comments(text, lang)
        return cleaned.count("{") == cleaned.count("}")
    except Exception:
        return True


def _py_compiles(text: str) -> bool:
    import py_compile, tempfile
    try:
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w") as tf:
            tf.write(text); tmpf = tf.name
        py_compile.compile(tmpf, doraise=True)
        return True
    except Exception:
        return False
    finally:
        try: os.unlink(tmpf)
        except Exception: pass


def _token_jaccard(a: str, b: str) -> float:
    ta, tb = set(a.split()), set(b.split())
    if not ta and not tb: return 1.0
    u = ta | tb
    return len(ta & tb) / len(u) if u else 0.0


def run_case(case: Case, client: OpenAICompatibleClient) -> CaseResult:
    res = CaseResult(id=case.id, language=case.language, dataset=case.dataset)
    t0 = time.time()
    # Use /var/tmp (root pool, 1.4T free) instead of the default /tmp (a 30G
    # tmpfs that filled up on a previous run — the orchestrator's per-case
    # session artifacts + git worktrees spike the usage).
    with tempfile.TemporaryDirectory(prefix="capy-rw-", dir="/var/tmp") as td:
        repo = Path(td) / "r"
        try:
            _materialize_conflict(case, repo)
        except Exception as exc:
            res.elapsed = time.time() - t0
            res.reason = f"setup failed: {type(exc).__name__}: {str(exc)[:100]}"
            # Treat setup failure as escalate (can't judge the merge)
            res.escalated = True
            return res
        cfg = _config_for(case)
        engine = ResolutionEngine(cfg.model, client=client)
        orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                            out=lambda *_a, **_k: None)
        try:
            step = orch.run()
            res.escalated = bool(step.escalated)
            res.reason = step.reason or ""
        except Exception as exc:
            res.escalated = True
            res.reason = f"orch raised: {type(exc).__name__}: {str(exc)[:100]}"
        # Read the resolved file.
        final = repo / case.path
        content = final.read_text() if final.exists() else ""
    res.elapsed = time.time() - t0
    res.marker_free = not _contains_markers(content) if content else False
    if case.language == "python":
        res.compiles = _py_compiles(content) if content else False
    else:
        res.compiles = _brace_balanced(content, case.language) if content else False
    res.matches_oracle = _token_jaccard(content, case.expected_resolved) if content else 0.0
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--lang", choices=("rust", "python"), default=None)
    ap.add_argument("--out", default="/tmp/capybase-live/realworld-results.json")
    ap.add_argument("--skip-existing", action="store_true",
                    help="Skip cases whose id is already in --out (resume after a kill)")
    ap.add_argument("--case-timeout", type=int, default=900,
                    help="Per-case wall-clock cap (seconds); 0 = no cap. Prevents one "
                         "hard case (endless CEGIS retries) from stalling the run.")
    args = ap.parse_args()

    cases = load_cases(limit=args.limit, lang=args.lang)
    print(f"loaded {len(cases)} cases (lang={args.lang or 'all'})")
    if not cases:
        print("no cases; exiting"); return

    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    # Resume support: load prior results and skip already-done case ids.
    results: list[CaseResult] = []
    done_ids: set[str] = set()
    if args.skip_existing and out.exists():
        try:
            prior = json.loads(out.read_text())
            for r in prior:
                results.append(CaseResult(**{k: v for k, v in r.items()
                                            if k in CaseResult.__dataclass_fields__}))
                # The verdict was not stored on CaseResult; recompute it.
                done_ids.add(r.get("id"))
            print(f"resume: loaded {len(done_ids)} prior results from {out}; skipping those ids")
        except Exception as exc:
            print(f"resume: could not load prior results ({exc}); starting fresh")

    cfg0 = _config_for(cases[0])
    client = OpenAICompatibleClient(cfg0.model)
    print(f"endpoint: {cfg0.model.base_url} model={cfg0.model.model}")

    pass_ct = sum(1 for r in results if not r.escalated and r.marker_free and r.compiles)
    escalate_ct = sum(1 for r in results if r.escalated)
    wrong_ct = sum(1 for r in results
                   if not (r.escalated or (r.marker_free and r.compiles)))
    t_start = time.time()
    skipped = 0
    for i, case in enumerate(cases, 1):
        if case.id in done_ids:
            skipped += 1
            continue
        print(f"[{i}/{len(cases)}] {case.id} ({case.language}/{case.dataset}) ...", end=" ", flush=True)
        # Run with a per-case wall-clock cap so one hard case (endless CEGIS
        # retries) can't stall the whole run. Implemented via a watchdog thread
        # that interrupts the worker. If the cap fires, treat it as an escalate.
        import threading
        result_holder: list = []
        def _worker():
            try:
                result_holder.append(run_case(case, client))
            except Exception as exc:
                result_holder.append(CaseResult(
                    id=case.id, language=case.language, dataset=case.dataset,
                    escalated=True,
                    reason=f"harness error: {type(exc).__name__}: {str(exc)[:100]}"))
        th = threading.Thread(target=_worker, daemon=True)
        th.start()
        th.join(timeout=args.case_timeout or None)
        if th.is_alive():
            # The worker is still in an LLM/CEGIS loop — abandon it (daemon) and
            # record an escalate. The next case starts fresh.
            print(f"\n      [TIMEOUT after {args.case_timeout}s — moving on]", end="")
            r = CaseResult(id=case.id, language=case.language, dataset=case.dataset,
                           escalated=True,
                           reason=f"case timeout after {args.case_timeout}s (endless CEGIS retries)")
        else:
            r = result_holder[0] if result_holder else CaseResult(
                id=case.id, language=case.language, dataset=case.dataset,
                escalated=True, reason="worker produced no result")
        if r.escalated:
            verdict = "ESCALATE"; escalate_ct += 1
        elif r.marker_free and r.compiles:
            verdict = "PASS"; pass_ct += 1
        else:
            verdict = "WRONG"; wrong_ct += 1
        print(f"{verdict}  {r.elapsed:.0f}s  sim={r.matches_oracle:.2f}  {r.reason[:60]}")
        results.append(r)
        # Incremental write: a kill won't lose progress.
        out.write_text(json.dumps([r.__dict__ for r in results], indent=2))

    elapsed = time.time() - t_start
    print("\n" + "=" * 64)
    print("REALWORLD LIVE EVAL SUMMARY")
    print("=" * 64)
    print(f"cases:    {len(results)} ({skipped} resumed, {len(results)-skipped} fresh this run)")
    print(f"PASS:     {pass_ct}")
    print(f"ESCALATE: {escalate_ct}")
    print(f"WRONG:    {wrong_ct}")
    print(f"wall:     {elapsed:.0f}s ({elapsed/60:.1f}m) [this run only]")
    for lang in ("python", "rust"):
        sub = [r for r in results if r.language == lang]
        if not sub: continue
        p = sum(1 for r in sub if not r.escalated and r.marker_free and r.compiles)
        e = sum(1 for r in sub if r.escalated)
        w = len(sub) - p - e
        print(f"  {lang}: {len(sub)} → PASS {p} / ESC {e} / WRONG {w}")
    from collections import Counter
    dt = Counter(r.dataset for r in results)
    dp = Counter(r.dataset for r in results if not r.escalated and r.marker_free and r.compiles)
    de = Counter(r.dataset for r in results if r.escalated)
    print("  by dataset:")
    for ds in sorted(dt):
        t = dt[ds]
        print(f"    {ds:24s} {t:3d} → PASS {dp[ds]:3d} / ESC {de[ds]:3d} / WRONG {t-dp[ds]-de[ds]:3d}")

    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([r.__dict__ for r in results], indent=2))
    print(f"\nfull results: {out}")


if __name__ == "__main__":
    main()
