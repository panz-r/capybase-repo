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
  PASS       — orch.run() did not escalate; resolved file is marker-free,
               brace-balanced, AND sim >= 0.95 (matches the oracle closely).
  NEAR_MATCH — marker-free and brace-balanced, but sim 0.80–0.95. The
               resolution is defensible but imperfect — the oracle's answer
               isn't the only valid one (exclusive choices, import ordering,
               doc-comment style differences). Investigate before trusting.
  ESCALATE   — orch.run() escalated (human required). The SAFE outcome.
  ORACLE_DIVERGENT — marker/brace failure OR sim < 0.80 (genuinely different
               from the oracle).

IMPORTANT — VALIDATION GAP for Rust:
  The temp repo has NO Cargo.toml, so cargo check/test never runs. The
  orchestrator falls back to standalone rustc (with E0432/E0433 suppressed)
  or silent-pass. The harness's post-check for Rust is brace-balance only.
  So PASS/NEAR_MATCH for Rust means "marker-free + braces balanced + high
  oracle similarity" — NOT "compiles in the real crate." Python cases DO
  get py_compile. This gap is intentional (cheap standalone Rust checks are
  undecidable without the full crate), but it means sim score is the primary
  quality signal for Rust.

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
from tests._realworld_build import C_BUILD_COMMANDS  # noqa: E402

TESTDATA = Path(__file__).resolve().parent.parent / "extracted-testdata" / "realworld"

# The configure/prepare step that must run ONCE before the in-loop ``make`` gate,
# because the production TestRunner uses shlex.split (no shell ``&&``). Re-running
# configure in _materialize_conflict (after git archive extracts the tree) means
# the in-loop pre_continue is a single ``make`` command. Empty = no prepare needed
# (redis ships a ready Makefile). Add entries as new C repos enter the corpus.
C_PREPARE_COMMANDS: dict[str, str] = {
    "redis-history": "",
    "jsonc-history": "cmake -B build -S . -DCMAKE_POLICY_VERSION_MINIMUM=3.5",
    "sqlite-history": "./configure",
}


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
    conflict_path: str = ""
    merge_sha: str = ""
    source_url: str = ""


class _NoConflictError(Exception):
    """The git rebase didn't produce a conflict for this case (the three versions
    don't conflict at git's merge level). The harness skips it as a non-conflict."""


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
    verdict: str = ""  # PASS | NEAR_MATCH | ORACLE_DIVERGENT | ESCALATE
    compiles_cargo: bool | None = None  # None when cargo didn't run
    terminal_reason: str = ""  # disjoint escalation classification
    # FR2a flight recorder: the orchestrator's session_id (the per-case artifact
    # root under .rebase-agent/sessions/<session_id>/). Populated when
    # --preserve-flights copies the session dir out; None otherwise. The flight
    # manifest maps case_id → session_id → artifacts for replay.
    session_id: str = ""


def _classify_terminal_reason(reason: str) -> str:
    """Classify an escalation reason into a disjoint terminal category.

    Returns one of:
      OVERSIZED           — oversized guard fired (file too large for model)
      MODEL_EMPTY         — model returned empty (not oversized)
      MODEL_NEEDS_HUMAN   — model self-reported needs_human
      CARGO_NO_PROGRESS   — convergence with cargo/syntax errors
      PROOF_NO_PROGRESS   — convergence without cargo errors (preservation only)
      WHOLE_FILE_FAILED   — whole-file repair couldn't resolve
      WALL_TIME           — exceeded per-unit wall-time budget
      CASE_TIMEOUT        — exceeded per-case timeout (900s+ of CEGIS retries)
      NO_CONFLICT         — git didn't produce a conflict
      OTHER               — uncategorized
    """
    r = (reason or "").lower()
    if "too large" in r or "oversized" in r:
        return "OVERSIZED"
    if "no conflict" in r or "skipped" in r:
        return "NO_CONFLICT"
    if "case timeout" in r:
        return "CASE_TIMEOUT"
    if "wall-time" in r or "wall_time" in r:
        return "WALL_TIME"
    if "needs_human" in r:
        return "MODEL_NEEDS_HUMAN"
    if "empty resolution" in r or "empty res" in r:
        return "MODEL_EMPTY"
    if "whole-file" in r or "whole_file" in r:
        return "WHOLE_FILE_FAILED"
    if "convergence" in r:
        if "stalled" in r or "unaccounted" in r:
            return "PROOF_NO_PROGRESS"
        return "CARGO_NO_PROGRESS"
    if "could not resolve" in r:
        if "error:" in r or "syntax" in r or "delimiter" in r:
            return "CARGO_NO_PROGRESS"
        return "MODEL_EMPTY"
    return "OTHER"


def load_cases(
    *,
    limit: int | None = None,
    lang: str | None = None,
    case_ids: list[str] | None = None,
) -> list[Case]:
    """Load realworld conflict cases from extracted-testdata.

    ``case_ids`` (when given) selects a subset by exact id match — used by the
    ``--case`` flag for targeted single-case reruns (e.g. verifying a fix
    against one case in seconds rather than a 5-hour full run)."""
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
            # Use the ACTUAL conflict_path from the dataset (includes the real
            # file extension like CHANGELOG.md, Cargo.toml, etc.) instead of the
            # synthetic conflict_NNNN.rs. This lets the orchestrator's
            # detect_language correctly classify the file — 49 of 175 cases had
            # mismatched extensions (CHANGELOG.md tagged as "rust", Cargo.toml
            # tagged as "rust", etc.) causing the structural parser to fail and
            # the prose value-resolution rule to decline.
            path=d.get("conflict_path") or d.get("path", f"{f.stem}.rs"),
            language=d.get("language", "rust"),
            base=d["base"], current=d["current"], replayed=d["replayed"],
            expected_resolved=d["expected_resolved"],
            marker_original=d["marker_original"],
            dataset=d.get("dataset", ""),
            conflict_path=d.get("conflict_path", ""),
            merge_sha=d.get("merge_sha", ""),
            source_url=d.get("source_url", ""),
        )
        if lang and c.language != lang:
            continue
        # --case selection: exact-id allowlist for targeted reruns. Applied
        # before the size guard so a selected case is never silently dropped.
        if case_ids and c.id not in case_ids:
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


def _materialize_conflict(case: Case, repo: Path, *, crate_source: Path | None = None) -> None:
    """Build a git history that produces the case's conflict markers on disk.

    Three commits: base, current (HEAD), replayed (the branch being rebased).
    A `git rebase` produces the UU conflict with case.marker_original on disk.

    When ``crate_source`` is provided (pointing to a local clone of the case's
    repo), the full crate tree at ``merge_sha`` is extracted first via
    ``git archive``, then the conflict file versions are overlaid on top.
    This gives the orchestrator's ``_run_cargo_syntax_check`` a real
    ``Cargo.toml`` and the full ``src/`` tree to compile against — turning
    the brace-balance gate into a real ``cargo check`` gate.
    """
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")

    # Optionally extract the full crate tree at merge_sha from the clone.
    # This provides Cargo.toml, Cargo.lock, and the full src/ tree so cargo
    # check can actually run. Falls back to single-file mode when no clone.
    if crate_source is not None:
        merge_sha = getattr(case, "merge_sha", "") or ""
        if merge_sha:
            try:
                import subprocess as _sp
                # git archive writes a tar of the tree at merge_sha; extract into repo.
                archive = _sp.run(
                    ["git", "-C", str(crate_source), "archive", merge_sha],
                    capture_output=True, check=True,
                )
                # Extract the tar into the repo directory.
                _sp.run(["tar", "-xf", "-", "-C", str(repo)],
                        input=archive.stdout, check=True)
            except Exception:  # noqa: BLE001 — best-effort; fall back to single-file
                pass

        # For C cases: run the build-prepare step (e.g. ./configure) once after
        # the tree is extracted, so the in-loop `make` gate works with the
        # no-shell production TestRunner (shlex.split can't handle `&&`). A
        # failed prepare is best-effort — the in-loop gate reports it honestly.
        if case.language == "c":
            prepare = C_PREPARE_COMMANDS.get(case.dataset, "")
            if prepare:
                try:
                    import subprocess as _sp
                    _sp.run(prepare, shell=True, cwd=str(repo),
                            capture_output=True, timeout=180)
                except Exception:  # noqa: BLE001 — best-effort
                    pass

    # Write the conflict file at its real path in all three versions.
    # (Overlays on top of the extracted tree.)
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
        # No conflict from git's view — the three versions don't actually
        # conflict at git's merge level (they touch different regions or the
        # replayed change is a subset of current). Skip this case (it's not a
        # real conflict for capybase to resolve). The harness records it as an
        # escalate so it's not counted as a WRONG merge.
        raise _NoConflictError(
            f"git rebase resolved cleanly (no conflict) for {case.id}"
        )


def _config_for(case: Case, *, has_crate: bool = False) -> Config:
    cfg = Config()
    cfg.model.base_url = os.environ.get("CAPYBASE_BASE_URL", "http://192.168.50.235:8086/v1")
    cfg.model.api_key = os.environ.get("CAPYBASE_API_KEY", "sk-local")
    cfg.model.model = os.environ.get("CAPYBASE_MODEL", "chat")
    cfg.model.temperature = 0.2
    cfg.model.max_tokens = 8192
    cfg.model.json_mode = True
    cfg.model.request_timeout_seconds = 600
    cfg.model.generation_timeout_seconds = 240
    # Context window: gemma-4-e4b has ~8K tokens (~32K chars). Setting this
    # enables the prompt builder's token-budget trimming (drops augmentation
    # sections like few-shot/history when the prompt is too large). The conflict
    # sides are NEVER trimmed (the model must see the actual conflict), but
    # knowing the limit lets the harness escalate early on oversized conflicts
    # instead of wasting 3 retries on empty responses.
    # Override via CAPYBASE_CONTEXT_WINDOW for models with different limits.
    cfg.model.context_window = int(os.environ.get("CAPYBASE_CONTEXT_WINDOW", "8192"))
    # Reserve more tokens for completion + prompt boilerplate. The default
    # completion_reserve=1024 only accounts for the model's output. But the
    # prompt's fixed boilerplate (intro/contract/rules + existing-imports
    # context + JSON formatting) adds ~800-1000 tokens that aren't trimmable.
    # Without this reserve, files that fit the marker threshold but push the
    # total prompt past the model's effective limit return empty responses.
    cfg.model.completion_reserve = int(os.environ.get("CAPYBASE_COMPLETION_RESERVE", "2048"))
    # Test gate:
    # - Python: py_compile (always available)
    # - Rust with full crate: the orchestrator's _run_cargo_syntax_check runs
    #   `cargo check` naturally (it finds the real Cargo.toml). We don't need
    #   a separate test command — the syntax validator IS the cargo check.
    # - Rust without crate: 'true' (brace-balance is the only gate).
    if case.language == "python":
        cfg.tests.pre_continue = f"python3 -m py_compile {case.path}"
    elif case.language == "c":
        # The in-loop whole-tree gate. configure already ran in
        # _materialize_conflict (so this is a single `make` command that works
        # with the no-shell TestRunner). Empty = no build command for this
        # dataset → the CcsSyntaxValidator (gcc -fsyntax-only) still gates
        # per-unit; brace-balance is the only whole-file check.
        cfg.tests.pre_continue = C_BUILD_COMMANDS.get(case.dataset, "") or "true"
    else:
        cfg.tests.pre_continue = "true"
    cfg.tests.final = cfg.tests.pre_continue
    cfg.tests.required = False  # harness judges; don't double-gate
    cfg.future.enable_structural_resolver = True
    cfg.future.enable_combination_search = True
    cfg.policy.max_retries_per_unit = 2  # cap CEGIS retries for throughput
    # Recovery retry budget: when the model self-reports needs_human, give it
    # one more attempt with a reframed prompt. Raised from 1 to 2 — the 12
    # MODEL_NEEDS_HUMAN cases in V6 had sim >= 0.85 (most >= 0.97), suggesting
    # the model CAN process these conflicts but gives up prematurely. A second
    # recovery attempt with different framing may produce output.
    cfg.policy.max_recovery_retries_per_unit = 2
    # Per-unit wall-time budget: escalate cleanly at the orchestrator level
    # (D2) rather than burning to the case cap and leaking the temp dir via an
    # abandoned daemon thread. 360s accommodates an on-premise weak LLM where a
    # single generation can take 80-120s, plus CEGIS retries. The wall-time
    # budget now EXCLUDES verification time (cargo check, rustc) — the budget
    # caps model/CEGIS loop iterations, not compilation time. This prevents a
    # slow first cargo check (dependency fetch) from eating the model's retry
    # budget. The v3 run lost 5 cases to the old 240s budget (all sim >= 0.92).
    cfg.policy.max_wall_time_per_unit_seconds = 360
    # Grant more whole-file repair cycles for complex cases where the model
    # produces near-correct output that fails the cross-hunk validation.
    cfg.policy.max_whole_file_repair_retries = 2
    # Suppress Rust crate-path errors (E0432/E0433) in the diagnostic delta —
    # these are undecidable standalone (need the full crate's dependency tree)
    # and cause false-positive rejections of near-correct Rust merges (5 cases
    # in the live eval with sim >= 0.95).
    if case.language == "rust":
        cfg.validation.rust_suppress_codes = ["E0432", "E0433"]
    # Phase 4 comment jury. Three operating modes via CAPYBASE_JURY_MODE:
    #   off     — never runs (default).
    #   shadow  — records hypothetical routing decisions, NO merge effect. The
    #             data is stored as jury_verdict artifacts under
    #             --preserve-flights for offline analysis + replay.
    #   enforce — acts on the four typed routes (accept / comment_counterexample
    #             / human_review / code_reopen). The Python canary scope.
    # The legacy CAPYBASE_SHADOW_JURY=1 maps to shadow (back-compat).
    jury_mode = os.environ.get("CAPYBASE_JURY_MODE", "").strip().lower()
    if jury_mode in ("off", "shadow", "enforce"):
        cfg.future.jury_mode = jury_mode
    elif os.environ.get("CAPYBASE_SHADOW_JURY", "").lower() in ("1", "true", "yes"):
        cfg.future.enable_shadow_jury = True  # back-compat → effective shadow
    # Autonomous code_reopen is separately gated (default off). Enable only when
    # positive-path evidence exists outside the shadow corpus.
    reopen = os.environ.get("CAPYBASE_JURY_CODE_REOPEN", "").strip().lower()
    if reopen in ("1", "true", "yes"):
        cfg.future.enable_jury_code_reopen = True
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


def _c_builds(repo: Path, case: Case) -> bool | None:
    """Run the C build command against the materialized temp repo tree.

    The orchestrator already wrote the resolved file into ``repo`` (which holds
    the full extracted tree + the prepare step from _materialize_conflict), so a
    real ``make`` compiles against the model's actual output and sibling files.
    Returns None when no build command is registered (caller falls back to
    brace-balance). Uses ``shell=True`` — the build command may chain, and this
    is a post-hoc harness check, not the production no-shell TestRunner.
    """
    import subprocess as _sp
    cmd = C_BUILD_COMMANDS.get(case.dataset, "")
    if not cmd:
        return None
    try:
        proc = _sp.run(cmd, shell=True, cwd=str(repo),
                       capture_output=True, text=True, timeout=300)
        return proc.returncode == 0
    except Exception:  # noqa: BLE001 — best-effort; treat as "couldn't check"
        return None


def _token_jaccard(a: str, b: str) -> float:
    ta, tb = set(a.split()), set(b.split())
    if not ta and not tb: return 1.0
    u = ta | tb
    return len(ta & tb) / len(u) if u else 0.0


def run_case(case: Case, client: OpenAICompatibleClient, *,
             flights_dir: Path | None = None,
             td: str | None = None,
             crate_source: Path | None = None) -> CaseResult:
    """Run one case. ``td`` is a pre-created temp dir (D3: the main thread owns
    cleanup so a timeout-abandoned daemon thread doesn't leak the temp tree).
    When ``td`` is None, a temp dir is created AND cleaned up within this call
    (the pre-D3 behavior, for non-timeout callers).
    ``crate_source``: when provided, the full crate tree at merge_sha is
    extracted into the temp repo so cargo check can run."""
    res = CaseResult(id=case.id, language=case.language, dataset=case.dataset)
    t0 = time.time()
    owns_td = td is None
    if owns_td:
        td = tempfile.mkdtemp(prefix="capy-rw-", dir="/var/tmp")
    try:
        repo = Path(td) / "r"
        try:
            _materialize_conflict(case, repo, crate_source=crate_source)
        except _NoConflictError as exc:
            res.elapsed = time.time() - t0
            res.escalated = True
            res.reason = f"skipped (no conflict): {exc}"
            return res
        except Exception as exc:
            res.elapsed = time.time() - t0
            res.reason = f"setup failed: {type(exc).__name__}: {str(exc)[:100]}"
            res.escalated = True
            return res
        cfg = _config_for(case, has_crate=crate_source is not None)
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
        # FR2a flight recorder: copy the per-case session artifacts out of the
        # temp repo. The session root contains the full §1 artifact list.
        res.session_id = getattr(orch, "session_id", "")
        if flights_dir is not None and res.session_id:
            try:
                import shutil
                dest = flights_dir / "flights" / case.id / res.session_id
                src = getattr(orch.paths, "root", None)
                if src is not None and src.exists():
                    shutil.copytree(src, dest, dirs_exist_ok=True)
            except Exception as exc:  # noqa: BLE001 — flight recorder is advisory
                res.reason = (res.reason + f" | flight copy failed: {exc}").strip(" |")
        # Read the resolved file.
        final = repo / case.path
        content = final.read_text() if final.exists() else ""
        # C post-hoc compile check must run WHILE the repo tree is on disk (the
        # finally below removes it). python/rust checks operate on the content
        # string alone, so they run after cleanup; the C build needs the tree.
        c_builds_result: bool | None = None
        if case.language == "c" and content:
            c_builds_result = _c_builds(repo, case)
    finally:
        # D3: when the main thread owns the temp dir, it cleans up after the
        # worker returns or times out. When we own it, clean up here.
        if owns_td:
            import shutil
            shutil.rmtree(td, ignore_errors=True)
    res.elapsed = time.time() - t0
    res.marker_free = not _contains_markers(content) if content else False
    if case.language == "python":
        res.compiles = _py_compiles(content) if content else False
    elif case.language == "c":
        # Use the build verdict captured before cleanup; fall back to brace-
        # balance if the build couldn't run (no command registered or no tree).
        res.compiles = c_builds_result if c_builds_result is not None else (
            _brace_balanced(content, case.language) if content else False
        )
    else:
        res.compiles = _brace_balanced(content, case.language) if content else False
    res.matches_oracle = _token_jaccard(content, case.expected_resolved) if content else 0.0
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--lang", choices=("rust", "python", "c"), default=None)
    ap.add_argument("--case", action="append", default=None, metavar="CASE_ID",
                    help="Select a specific case id (repeatable). Enables targeted "
                         "single-case reruns in seconds instead of a full 5-hour run. "
                         "Example: --case sea-orm-history-0016 --case tokio-history-0019")
    ap.add_argument("--out", default="/tmp/capybase-live/realworld-results.json")
    ap.add_argument("--skip-existing", action="store_true",
                    help="Skip cases whose id is already in --out (resume after a kill)")
    ap.add_argument("--case-timeout", type=int, default=1200,
                    help="Per-case wall-clock cap (seconds); 0 = no cap. Prevents one "
                         "hard case (endless CEGIS retries) from stalling the run. "
                         "Raised from 900 to 1200 in V6 — the dominant-counterexample "
                         "repair (one fix per iteration) can take more iterations to "
                         "converge but each is more focused.")
    ap.add_argument("--preserve-flights", default=None,
                    help="Directory to copy per-case orchestrator session artifacts into "
                         "(FR2a flight recorder). Produces <dir>/flights/<case_id>/<session_id>/ "
                         "and <dir>/manifest.json. Required for shadow-jury replay.")
    args = ap.parse_args()

    # Shared cargo registry cache so dependencies are fetched once and reused
    # across cases (the per-case temp repo is destroyed, but the cache persists).
    # This is essential for full-crate materialization to be practical.
    os.environ.setdefault("CARGO_HOME", "/var/tmp/capybase-cargo-cache")

    flights_dir = Path(args.preserve_flights) if args.preserve_flights else None
    if flights_dir is not None:
        flights_dir.mkdir(parents=True, exist_ok=True)

    cases = load_cases(limit=args.limit, lang=args.lang, case_ids=args.case)
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

    pass_ct = sum(1 for r in results if not r.escalated and r.marker_free and r.compiles and r.matches_oracle >= 0.95)
    near_ct = sum(1 for r in results if not r.escalated and r.marker_free and r.compiles and 0.80 <= r.matches_oracle < 0.95)
    escalate_ct = sum(1 for r in results if r.escalated)
    wrong_ct = sum(1 for r in results
                   if not (r.escalated or (r.marker_free and r.compiles and r.matches_oracle >= 0.80)))
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
        import shutil
        # D3: create the temp dir in the MAIN thread so we own cleanup. The
        # worker receives it via `td=`; if the worker times out and is
        # abandoned, the main thread cleans up here (no leaked temp trees).
        case_td = tempfile.mkdtemp(prefix="capy-rw-", dir="/var/tmp")
        # Resolve the crate source clone for full-tree materialization.
        # Maps dataset name → external-datasets clone dir. Enables cargo check.
        crate_source = None
        if case.merge_sha:
            clone_name = case.dataset.replace("-history", "") if case.dataset else ""
            clone_path = Path(__file__).resolve().parent.parent / "external-datasets" / clone_name
            if clone_path.is_dir():
                crate_source = clone_path
        result_holder: list = []
        def _worker():
            try:
                result_holder.append(run_case(case, client, flights_dir=flights_dir,
                                              td=case_td, crate_source=crate_source))
            except Exception as exc:
                result_holder.append(CaseResult(
                    id=case.id, language=case.language, dataset=case.dataset,
                    escalated=True,
                    reason=f"harness error: {type(exc).__name__}: {str(exc)[:100]}"))
        th = threading.Thread(target=_worker, daemon=True)
        th.start()
        th.join(timeout=args.case_timeout or None)
        # D3: clean up the temp dir from the main thread regardless of whether
        # the worker finished or was abandoned.
        shutil.rmtree(case_td, ignore_errors=True)
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
            # The resolution is marker-free and brace-balanced (or py_compiles
            # for Python). But the live eval does NOT run cargo check/test for
            # Rust — the temp repo has no Cargo.toml. So "compiles" here is a
            # weak gate (brace balance only). Classify by oracle similarity:
            #   sim >= 0.95 → PASS (matches the oracle closely)
            #   sim >= 0.80 → NEAR_MATCH (defensible but imperfect — the oracle's
            #                  answer isn't the only valid one, e.g. exclusive
            #                  choices, import reordering, doc-comment style)
            #   sim < 0.80 → ORACLE_DIVERGENT (genuinely different from the oracle)
            if r.matches_oracle >= 0.95:
                verdict = "PASS"; pass_ct += 1
            elif r.matches_oracle >= 0.80:
                verdict = "NEAR_MATCH"; near_ct += 1
            else:
                verdict = "ORACLE_DIVERGENT"; wrong_ct += 1
        else:
            verdict = "ORACLE_DIVERGENT"; wrong_ct += 1
        print(f"{verdict}  {r.elapsed:.0f}s  sim={r.matches_oracle:.2f}  {r.reason[:60]}")
        r.verdict = verdict
        r.terminal_reason = _classify_terminal_reason(r.reason) if r.escalated else ""
        results.append(r)
        # Incremental write: a kill won't lose progress.
        out.write_text(json.dumps([r.__dict__ for r in results], indent=2))
        # FR2a/FR2b: incremental flight manifest write. Maps case_id →
        # session_id → verdict → artifacts, so the shadow jury can replay
        # cases without rerunning code resolution. The manifest is the
        # resume source of truth for flights (alongside the results JSON).
        if flights_dir is not None:
            manifest_path = flights_dir / "manifest.json"
            manifest: list = []
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text())
                except Exception:
                    manifest = []
            manifest = [m for m in manifest if m.get("case_id") != r.id]
            manifest.append({
                "case_id": r.id, "session_id": r.session_id,
                "language": r.language, "dataset": r.dataset,
                "verdict": verdict, "elapsed": round(r.elapsed, 1),
                "matches_oracle": round(r.matches_oracle, 3),
                "escalated": r.escalated, "reason": r.reason[:200],
            })
            manifest_path.write_text(json.dumps(manifest, indent=2))

    elapsed = time.time() - t_start
    print("\n" + "=" * 64)
    print("REALWORLD LIVE EVAL SUMMARY")
    print("=" * 64)
    print(f"cases:    {len(results)} ({skipped} resumed, {len(results)-skipped} fresh this run)")
    print(f"PASS:       {pass_ct}")
    print(f"NEAR_MATCH: {near_ct}  (sim 0.80–0.95: defensible but imperfect)")
    print(f"ESCALATE:   {escalate_ct}")
    print(f"ORACLE_DIVERGENT: {wrong_ct}  (sim < 0.80 or marker/brace failure)")
    print(f"wall:       {elapsed:.0f}s ({elapsed/60:.1f}m) [this run only]")
    for lang in ("python", "rust"):
        sub = [r for r in results if r.language == lang]
        if not sub: continue
        p = sum(1 for r in sub if not r.escalated and r.marker_free and r.compiles and r.matches_oracle >= 0.95)
        n = sum(1 for r in sub if not r.escalated and r.marker_free and r.compiles and 0.80 <= r.matches_oracle < 0.95)
        e = sum(1 for r in sub if r.escalated)
        w = len(sub) - p - n - e
        print(f"  {lang}: {len(sub)} → PASS {p} / NEAR {n} / ESC {e} / DIVERGE {w}")
    from collections import Counter
    dt = Counter(r.dataset for r in results)
    dp = Counter(r.dataset for r in results if not r.escalated and r.marker_free and r.compiles and r.matches_oracle >= 0.95)
    dn = Counter(r.dataset for r in results if not r.escalated and r.marker_free and r.compiles and 0.80 <= r.matches_oracle < 0.95)
    de = Counter(r.dataset for r in results if r.escalated)
    print("  by dataset:")
    for ds in sorted(dt):
        t = dt[ds]
        print(f"    {ds:24s} {t:3d} → PASS {dp[ds]:3d} / NEAR {dn[ds]:3d} / ESC {de[ds]:3d} / DIVERGE {t-dp[ds]-dn[ds]-de[ds]:3d}")
    # Terminal reason distribution for escalations
    from collections import Counter as _C
    tr = _C(r.terminal_reason for r in results if r.escalated)
    if tr:
        print(f"\n  escalation terminal reasons:")
        for reason, count in tr.most_common():
            print(f"    {reason:25s} {count}")

    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([r.__dict__ for r in results], indent=2))
    print(f"\nfull results: {out}")


if __name__ == "__main__":
    main()
