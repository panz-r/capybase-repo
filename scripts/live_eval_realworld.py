#!/usr/bin/env python3
"""Live-model evaluation harness for the realworld conflict corpus.

Drives the capybase Orchestrator with a REAL OpenAICompatibleClient against
the configured local model, on the genuine git merge conflicts under
extracted-testdata/realworld/. Each case is materialized as a real git repo
with the conflict markers on disk, then `orch.run()` resolves it end-to-end
(extraction → resolution → file write → test gate) — the authentic system path.

NOT part of the hermetic test suite — makes real network calls. The endpoint
is NEVER hardcoded in this file. Resolve it with a provider config — the
canonical mechanism (JSON under ~/.config/capybase/providers/, outside the
repo; `capybase provider list` shows what's configured):

    .venv/bin/python scripts/live_eval_realworld.py --provider <name> ...

Explicit per-field overrides: --base-url / --model / --api-key / --profile
flags, or the CAPYBASE_BASE_URL / CAPYBASE_MODEL / CAPYBASE_API_KEY /
CAPYBASE_PROFILE env vars. A run without a calibration profile is an ERROR —
profiles are expensive and never auto-created or substituted.

Verdict per case:
  PASS       — orch.run() did not escalate; resolved file is marker-free,
               brace-balanced, AND sim >= PASS_THRESHOLD (matches the oracle
               closely).
  WORKING    — marker-free, compiles/builds, but sim below the PASS bar AND
               the output preserves both sides' changes (>= 0.5 of each
               side's changed-line content). A correct, functioning merge
               that legitimately differs from the repo-derived oracle: the
               human resolution dropped one side's working code for reasons
               outside the merge inputs (project direction, planning), which
               no content signal carries. Distinct near-success outcome —
               good for the system, not identical to history.
  NEAR_MATCH — marker-free and brace-balanced, sim 0.80–PASS_THRESHOLD, and
               NOT both-sides-preserving. The resolution is defensible but
               imperfect — investigate before trusting.
  ESCALATE   — orch.run() escalated (human required). The SAFE outcome.
  ORACLE_DIVERGENT — marker/brace failure OR sim < 0.80 without the
               preservation property (genuinely different from the oracle).
  GATE_UNAVAILABLE — sim >= 0.95 content that the build/validation gate
               rejected, where the ORACLE itself fails the same gate
               (oracle_builds=False, probed post-hoc while the materialized
               tree still exists). The gate cannot distinguish the resolver's
               output from the human resolution — the case measures the
               sandbox, not the resolver (protobuf-0055/0065, fmt-0003,
               tokio-0110 classes). Distinct from PASS: not counted as a
               resolution success.

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
import re
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
from capybase.provider_config import (  # noqa: E402
    ProviderError,
    ResolvedProvider,
    apply_to_config,
    resolve_provider,
)
from capybase.resolution_engine import ResolutionEngine  # noqa: E402
from tests._realworld_build import (  # noqa: E402
    C_BUILD_COMMANDS,
    C_TEST_COMMANDS,
)
from capybase.verification import (  # noqa: E402
    _ccache_enabled,
    _ccache_env,
    _run_shell_tree,
)

TESTDATA = Path(__file__).resolve().parent.parent / "extracted-testdata" / "realworld"

#: Minimum oracle similarity for PASS. Configurable via env var for
#: autonomous operation (where a compiling merge that preserves both
#: sides' intent is a success even below 0.95). Default 0.90.
PASS_THRESHOLD = float(os.environ.get("CAPYBASE_PASS_THRESHOLD", "0.90"))

# The configure/prepare step that must run ONCE before the in-loop ``make`` gate,
# because the production TestRunner uses shlex.split (no shell ``&&``). Re-running
# configure in _materialize_conflict (after git archive extracts the tree) means
# the in-loop pre_continue is a single ``make`` command. Empty = no prepare needed
# (redis ships a ready Makefile). Add entries as new C repos enter the corpus.
#
# IMPORTANT: json-c and other C repos changed build systems across their history
# (older commits used autotools/configure.ac, newer use cmake). The per-dataset
# default here is the PREFERRED prepare for the majority commit; the materializer
# probes the extracted tree and adapts (cmake → autotools fallback) per case.
C_PREPARE_COMMANDS: dict[str, str] = {
    "redis-history": "",
    "jsonc-history": "cmake -B build -S . -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
    "sqlite-history": "./configure && make -j{jobs}",
    "nlohmann-json-history": "cmake -B build -S . -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
    "clickhouse-history": "cmake -B build -S . -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
    "protobuf-history": "cmake -B build -S . -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -Dprotobuf_BUILD_TESTS=OFF",
}

# Per-case build-command cache: populated by _materialize_conflict after it
# probes the extracted tree's build system. _config_for reads from here so the
# in-loop build gate matches whatever prepare actually ran. Keyed by case.id.
_DETECTED_BUILD_CMD: dict[str, str] = {}
# Sprint-20 S20.2: toolchain-era preflight cache (case_id -> probe dict,
# None when no usable gate). Populated on the first run of a case; the
# majority repeats reuse it (pristine sides and the oracle are identical
# across repeats — re-probing would only burn build time).
_TOOLCHAIN_PROBE_CACHE: dict[str, dict | None] = {}


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
    # Sprint-20 S20.11: control-flow skeleton similarity to the oracle
    # (EVAL ONLY — never a gate). High with low matches_oracle flags an
    # idiomatic rewrite candidate.
    skeleton_similarity: float = 0.0
    elapsed: float = 0.0
    reason: str = ""
    verdict: str = ""  # PASS | WORKING | NEAR_MATCH | ORACLE_DIVERGENT | ESCALATE | ESCALATE_TOOLCHAIN | GATE_UNAVAILABLE
    compiles_cargo: bool | None = None  # None when cargo didn't run
    terminal_reason: str = ""  # disjoint escalation classification
    conflict_region_count: int = 0  # number of <<<<<<< regions (for timeout classification)
    # Side-preservation fractions (the WORKING classification): share of each
    # side's changed-line content the resolved output preserves — added/
    # changed lines present in the output, deleted lines absent. None when
    # not measurable (empty output, or a side with no changes vs base).
    loser_preservation: float | None = None
    winner_preservation: float | None = None
    # FR2a flight recorder: the orchestrator's session_id (the per-case artifact
    # root under .rebase-agent/sessions/<session_id>/). Populated when
    # --preserve-flights copies the session dir out; None otherwise. The flight
    # manifest maps case_id → session_id → artifacts for replay.
    session_id: str = ""
    # Variance-aware evaluation (--repeat-nonpass): all verdicts observed
    # across the repeat runs for this case, in order (first run first).
    # Empty when the case passed first try or repeats are off. The stored
    # record is the first run whose verdict equals the majority.
    repeat_verdicts: list = None
    # WS1c oracle-build-check: does expected_resolved pass the SAME build
    # gate the merge faced (C: the tree build; rust-with-crate: the cargo
    # new-error delta vs the one-side-blanked baseline)? None when no gate
    # ran or the probe was undecidable. True + sim >= 0.95 + a gate-rejected
    # verdict => GATE_UNAVAILABLE: the case measures the sandbox, not the
    # resolver. Probed only for cases heading to a non-clean verdict.
    oracle_builds: bool | None = None
    # Sprint-25 decision 1: the project's own tests run on the resolver's
    # output tree (post-hoc, divergent band only). True → the WORKING
    # verdict regardless of preservation (tests pass = un-gameable merge
    # value: capybase never writes tests). None = no command / couldn't run.
    output_tests: bool | None = None
    # Sprint-20 S20.2: toolchain-era preflight — both pristine sides AND
    # the oracle fail the real gate with identical compile-error
    # signatures (un-passable under this toolchain; the tokio-0109
    # class). toolchain_probe carries the per-side rc/signature audit.
    toolchain_dead: bool = False
    toolchain_probe: dict = None


def _classify_terminal_reason(reason: str) -> str:
    """Classify an escalation reason into a disjoint terminal category.

    Returns one of:
      SAFE_STOP           — safety guard caught a real danger (resurrection)
      SAFE_SKIP           — no real conflict (git resolved cleanly)
      OVERSIZED           — oversized guard fired (file too large for model)
      MODEL_EMPTY         — model returned empty (not oversized)
      MODEL_NEEDS_HUMAN   — model self-reported needs_human
      TIMEOUT_CONVERGENCE — CEGIS loop failed to converge (no-progress / wall-time)
      TIMEOUT_THROUGHPUT  — per-case timeout on a many-region file (>20 units)
      TIMEOUT_CAPABILITY  — per-case timeout on a small file (model can't solve it)
      REPAIR_FAILURE      — whole-file repair couldn't resolve a unit
      TOOLCHAIN_ERA       — preflight: sides+oracle all fail the gate identically
      OTHER               — uncategorized
    """
    r = (reason or "").lower()
    # Sprint-20 S20.2: the preflight classification (un-passable case, not
    # a resolver outcome).
    if "toolchain-era" in r:
        return "TOOLCHAIN_ERA"
    # Safety stops (true-positive catches) — highest priority classification.
    if "resurrection" in r:
        return "SAFE_STOP"
    # Safety skips (not real conflicts).
    if "no conflict" in r or "skipped (no conflict)" in r:
        return "SAFE_SKIP"
    if "too large" in r or "oversized" in r:
        return "OVERSIZED"
    if "case timeout" in r:
        return "TIMEOUT_CASE"
    if "wall-time" in r or "wall_time" in r:
        return "TIMEOUT_CONVERGENCE"
    if "no hard-failure progress" in r:
        return "TIMEOUT_CONVERGENCE"
    if "needs_human" in r:
        return "MODEL_NEEDS_HUMAN"
    if "empty resolution" in r or "empty res" in r:
        return "MODEL_EMPTY"
    if "whole-file" in r or "whole_file" in r:
        return "REPAIR_FAILURE"
    if "convergence" in r:
        return "TIMEOUT_CONVERGENCE"
    if "could not resolve" in r:
        if "error:" in r or "syntax" in r or "delimiter" in r:
            return "TIMEOUT_CONVERGENCE"
        return "MODEL_EMPTY"
    return "OTHER"


def load_cases(
    *,
    limit: int | None = None,
    lang: str | None = None,
    case_ids: list[str] | None = None,
    dropped_ids: list[str] | None = None,
) -> list[Case]:
    """Load realworld conflict cases from extracted-testdata.

    ``case_ids`` (when given) selects a subset by exact id match — used by the
    ``--case`` flag for targeted single-case reruns (e.g. verifying a fix
    against one case in seconds rather than a full 5-hour run).

    ``dropped_ids`` (when a list is passed) receives the ids of cases the
    48K size guard excluded, so the caller can surface a subset-run warning
    (the shard-4 first-launch incident: 80/167 ran while exit=0 looked
    complete)."""
    from capybase.orchestrator import _LOCKFILE_TAKEOVER_NAMES as _LOCK_NAMES
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
        # Entity splitting is always-on and adaptively breaks oversized multi-
        # entity marker blocks into per-entity sub-units whose prompts fit the
        # window, so for those cases the guard is a false proxy — lift it via
        # CAPYBASE_SKIP_SIZE_GUARD=1. (Cases it can't help — a single oversized
        # entity, or an un-splittable language — still need the guard.)
        # Lockfile exemption (sprint-20 S20.5): a Cargo.lock case never builds
        # an LLM prompt — the lockfile takeover resolves the whole file
        # deterministically pre-cascade — so the window-size rationale doesn't
        # apply (both corpus Cargo.lock cases are >48K marker files that were
        # silently unloadable). If the takeover declines at runtime, the
        # oversized-prompt guard still protects the per-unit path.
        _is_lockfile = ((c.path or "").rsplit("/", 1)[-1].lower()
                        in _LOCK_NAMES)
        _skip_guard = os.environ.get("CAPYBASE_SKIP_SIZE_GUARD", "") == "1"
        if (not _skip_guard and not _is_lockfile
                and len(c.marker_original) > 48 * 1024):
            if dropped_ids is not None:
                dropped_ids.append(c.id)
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


def _resolve_c_build(repo: Path, dataset: str, default_prepare: str) -> tuple[str, str]:
    """Probe the extracted tree's build system and return (prepare_cmd, build_cmd).

    C repos change build systems across their history (json-c moved from
    autotools to cmake). The per-dataset ``default_prepare`` is preferred, but
    when its prerequisite is absent we fall back to a working alternative so
    the build gate isn't a false rejector of correct resolutions.

    Returns (prepare, build) command strings. ``prepare`` runs once in
    _materialize_conflict; ``build`` is the in-loop gate command. Both use
    ``shell=True``.

    Detection order:
      1. cmake  — CMakeLists.txt present → cmake -B build ... / cmake --build build
      2. autotools — configure.ac or Makefile.am present → autoreconf+configure / make
      3. pre-configured — a ``configure`` script exists → ./configure / make
      4. ready — a Makefile exists (redis) → (no prepare) / make
      5. unknown → (no prepare) / true (brace-balance + gcc -fsyntax-only only)
    """
    has_cmake = (repo / "CMakeLists.txt").exists()
    has_autotools = (repo / "configure.ac").exists() or (repo / "Makefile.am").exists()
    has_configure = (repo / "configure").exists() and (repo / "configure").stat().st_mode & 0o111
    has_makefile = (repo / "Makefile").exists()

    if has_cmake:
        # Default prepare is cmake; use it. Build with cmake --build.
        if default_prepare and "cmake" in default_prepare:
            return default_prepare, "cmake --build build"
        return ("cmake -B build -S . -DCMAKE_POLICY_VERSION_MINIMUM=3.5",
                "cmake --build build")
    if has_autotools:
        # Generate configure from configure.ac, run it, then build derived
        # headers only (not the full project — that takes too long for the
        # case budget). The headers (parse.h, opcodes.h, sqlite3.h, etc.)
        # are needed for gcc -fsyntax-only verification. The build_cmd stays
        # "make -j{jobs}" so verify_file can do targeted builds (.lo/.o).
        # The job count is resolved in Python (not $(nproc)) because the
        # TestRunner invokes the pre_continue command WITHOUT a shell —
        # a literal -j$(nproc) argument is an invalid make option (usage
        # text, rc=2, no attributable error lines).
        _jobs = max(4, (os.cpu_count() or 4))
        return ("autoreconf -fi >/dev/null 2>&1; ./configure >/dev/null 2>&1",
                f"make -j{_jobs}")
    if has_configure:
        _jobs = max(4, (os.cpu_count() or 4))
        return ("./configure >/dev/null 2>&1",
                f"make -j{_jobs}")
    if has_makefile:
        _jobs = max(4, (os.cpu_count() or 4))
        return ("", f"make -j{_jobs}")
    # Unknown build system — no whole-tree gate. The CcsSyntaxValidator
    # (gcc -fsyntax-only) still gates per-unit; brace-balance is the only
    # whole-file check. This is honest (we can't build what we can't detect).
    return ("", "true")


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

        # For C cases: run the build-prepare step (e.g. ./configure) AFTER the
        # rebase, not before. The rebase creates commits that don't touch the
        # untracked build dir, but git checkout during rebase CAN leave the
        # tree in a state where cmake's cached paths are stale. Running prepare
        # after the rebase ensures the build dir is fresh on the final
        # conflicted state that the orchestrator will resolve.
        # NOTE: the prepare runs inside _materialize_conflict which is called
        # BEFORE the orchestrator. The orchestrator's rebase is done here;
        # the prepare is deferred to after it via a flag the caller checks.
        # Actually, _materialize_conflict IS the function that sets up the
        # rebase, so the prepare needs to run at the END of this function
        # (after the rebase at line 295). Moved below.

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

    # Reconstruct the conflict file using git merge-file on the three
    # authoritative case versions — but ONLY for massively asymmetric
    # cases. The rebase's merge engine (merge-ort/merge-recursive) can
    # produce wider conflict regions than git merge-file when one side
    # massively rewrote the file (e.g., 8000-line rewrite vs 1-line
    # change), incorrectly including non-conflicting deletions in the
    # conflict region. For symmetric conflicts, the rebase's markers are
    # equivalent or tighter — reconstructing would only risk regression.
    # Asymmetry heuristic: one side's line count differs from base by >30%.
    _base_n = len(case.base.splitlines())
    _cur_n = len(case.current.splitlines())
    _rep_n = len(case.replayed.splitlines())
    _asymmetric = _base_n > 0 and (
        abs(_cur_n - _base_n) / _base_n > 0.30
        or abs(_rep_n - _base_n) / _base_n > 0.30
    )
    if _asymmetric:
        import tempfile as _tf_merge
        with _tf_merge.NamedTemporaryFile(mode="w", suffix=".cur", delete=False) as _cf:
            _cf.write(case.current); _cur_p = _cf.name
        with _tf_merge.NamedTemporaryFile(mode="w", suffix=".base", delete=False) as _bf:
            _bf.write(case.base); _base_p = _bf.name
        with _tf_merge.NamedTemporaryFile(mode="w", suffix=".rep", delete=False) as _rf:
            _rf.write(case.replayed); _rep_p = _rf.name
        try:
            _merge_proc = subprocess.run(
                ["git", "merge-file", "-p", "--diff3", _cur_p, _base_p, _rep_p],
                capture_output=True, text=True, timeout=30,
            )
            if _merge_proc.returncode > 0 and _merge_proc.stdout:
                # Overwrite the worktree file with the corrected conflict.
                (repo / case.path).write_text(_merge_proc.stdout)
        except Exception:
            pass  # fall back to the rebase's conflict file
        finally:
            from pathlib import Path as _Pf_merge
            for _p in (_cur_p, _base_p, _rep_p):
                _Pf_merge(_p).unlink(missing_ok=True)

    # For C cases: run the build-prepare step AFTER the rebase, so the build
    # dir is fresh on the final conflicted state. The rebase's git checkouts
    # don't destroy untracked files (build/), but cmake's cached paths may be
    # stale after the checkout operations. Running prepare here ensures the
    # orchestrator's verify_file build gate finds a valid build dir.
    #
    # The prepare command is ADAPTIVE: C repos change build systems across
    # their history (json-c moved from autotools to cmake). The per-dataset
    # default is preferred, but we probe the extracted tree and fall back when
    # the default's prerequisite is absent (e.g. no CMakeLists.txt → autotools).
    # The detected build command is stashed in _DETECTED_BUILD_CMD so _config_for
    # can set the matching in-loop gate.
    if case.language in ("c", "cpp", "c++"):
        default_prepare = C_PREPARE_COMMANDS.get(case.dataset, "")
        if "{jobs}" in default_prepare:
            # Resolve here (not $(nproc)): some runners invoke commands
            # without a shell, where a literal -j$(nproc) is an invalid
            # make option (usage text, rc=2, no attributable errors).
            default_prepare = default_prepare.format(
                jobs=max(4, (os.cpu_count() or 4)))
        prepare, build_cmd = _resolve_c_build(repo, case.dataset, default_prepare)
        prepare_ok = True

        # Fix sqlite's tool/lemon.c: the parser generator has K&R-style
        # forward declarations (void FuncName();) that conflict with the
        # definitions (void FuncName(struct lemon *)) under C11+. This
        # prevents lemon from compiling on modern GCC, which blocks the
        # entire sqlite build (lemon generates parse.h, opcodes.h, etc.).
        # Patch the 6 conflicting declarations with proper prototypes.
        _lemon_path = repo / "tool" / "lemon.c"
        if _lemon_path.exists():
            _lemon_src = _lemon_path.read_text()
            if re.search(r'^void\s+\w+\s*\(\s*\)\s*;', _lemon_src, re.MULTILINE):
                _func_defs = {}
                for m in re.finditer(r'^(void\s+(\w+)\s*\(([^)]{0,200})\))', _lemon_src, re.MULTILINE):
                    _func_defs[m.group(2)] = m.group(3)
                _fixed = _lemon_src
                for m in re.finditer(r'^(void\s+(\w+)\s*\(\s*\)\s*;)', _lemon_src, re.MULTILINE):
                    _fn = m.group(2)
                    if _fn in _func_defs:
                        _fixed = _fixed.replace(m.group(1), f'void {_fn}({_func_defs[_fn]});')
                if _fixed != _lemon_src:
                    _lemon_path.write_text(_fixed)

        if prepare:
            # Run the prepare step (configure/cmake). The prepare is
            # deterministic per source tree but takes ~30s (cmake) to ~3-5
            # minutes (autoreconf+configure on old autotools trees —
            # protobuf's 2015-era commits intermittently exceeded the old
            # 180s budget under load, leaving no Makefile and a bare `make`
            # gate that poisoned verification). We accept this overhead per
            # case rather than caching, because cached Makefiles contain
            # absolute paths (TOP=/var/tmp/capy-rw-OLD/r) that break when
            # restored into a different temp dir.
            try:
                _prepare_timeout = 300 if "autoreconf" in prepare else 180
                proc = _run_shell_tree(prepare, cwd=str(repo),
                                       timeout=_prepare_timeout)
                prepare_ok = proc.returncode == 0
            except Exception:  # noqa: BLE001 — best-effort
                prepare_ok = False
        # If prepare failed (missing autotools macros, no compiler, etc.),
        # don't saddle the build gate with a command that can't work — it
        # would reject every resolution, even perfect ones. Fall back to
        # ``true`` so the per-unit gcc -fsyntax-only gate (CcsSyntaxValidator)
        # is the only compile check. This is honest: we can't whole-tree-build
        # a tree whose build system we can't complete, but the per-unit syntax
        # gate still catches structural defects.
        # Also verify the build directory exists when using cmake — a missing
        # build/ dir causes a 900s timeout (cmake --build on a non-existent
        # directory hangs or fails repeatedly inside the orchestrator loop).
        if prepare_ok and "cmake --build" in build_cmd:
            if not (repo / "build").is_dir():
                prepare_ok = False
        # Same honesty for make-based gates: a `make`/`make -jN` build
        # command with no Makefile in the tree fails instantly with
        # "No targets specified and no makefile found" — previously a
        # poisoned hard failure in every Phase 2 verify. If prepare
        # didn't actually leave a Makefile, degrade to "true" and let the
        # per-unit gcc -fsyntax-only gate carry compile checking.
        if prepare_ok and build_cmd.strip().startswith("make"):
            if not (repo / "Makefile").exists():
                prepare_ok = False
        _DETECTED_BUILD_CMD[case.id] = build_cmd if prepare_ok else "true"
        # Generate sqlite's derived headers (parse.h, opcodes.h, sqlite3.h,
        # keywordhash.h) after configure. These are needed by gcc -fsyntax-only
        # for per-file verification. They require the lemon parser generator
        # (tool/lemon.c, already patched) + mkkeywordhash + mkopcodeh. Building
        # just these targets (not the full project) is ~15-20s vs 75s for make.
        # Skip if the build cache was a hit (headers already present).
        if (
            prepare_ok
            and (repo / "tool" / "lemon.c").exists()
            and (repo / "Makefile").exists()
        ):
            try:
                # Build lemon, then the derived headers. These are the
                # prerequisite targets for compiling any sqlite source file.
                _run_shell_tree(
                    "make lemon sqlite3.h >/dev/null 2>&1 && "
                    "make parse.h >/dev/null 2>&1 && "
                    "make keywordhash.h >/dev/null 2>&1 && "
                    "make opcodes.h >/dev/null 2>&1",
                    cwd=str(repo), timeout=120,
                    env=_ccache_env() if _ccache_enabled() else None,
                )
            except Exception:  # noqa: BLE001 — header generation is advisory
                pass
        # Autotools prepares regenerate TRACKED files (config.h.in etc.)
        # after the rebase has stopped — leaving them unstaged makes the
        # worktree dirty, and `git rebase --continue` then refuses with
        # "You must edit all merge conflicts and then mark them as
        # resolved" on EVERY continue (jsonc 0013/0014/0016 spun to their
        # case timeouts on exactly this). The regeneration's useful
        # products are untracked (build/, generated headers); restore the
        # tracked side effects so the continue can proceed.
        try:
            import subprocess as _sp_restore
            _diff = _sp_restore.run(
                ["git", "diff", "--name-only"],
                cwd=str(repo), capture_output=True, text=True,
            )
            _dirty = [
                ln for ln in (_diff.stdout or "").splitlines()
                if ln.strip() and ln.strip() != case.path
            ]
            if _dirty:
                _sp_restore.run(
                    ["git", "checkout", "--", *_dirty],
                    cwd=str(repo), capture_output=True, text=True,
                )
        except Exception:  # noqa: BLE001 — restoration is advisory
            pass


# The resolved provider (endpoint host+model + required calibration profile),
# set once in main() from CLI flags > env > provider file. No endpoint default
# lives in this file; a run without a resolution refuses to start.
_PROVIDER: ResolvedProvider | None = None


def _require_provider() -> ResolvedProvider:
    if _PROVIDER is None:
        raise SystemExit(
            "no provider resolved: pass --provider NAME (see `capybase provider "
            "list`), set CAPYBASE_PROVIDER, or give explicit --base-url/--model "
            "/--profile flags"
        )
    return _PROVIDER


def _config_for(case: Case, *, has_crate: bool = False) -> Config:
    cfg = Config()
    # Endpoint + calibration profile, resolved once in main() from
    # CLI flags > env > provider file (see provider_config.resolve_provider).
    # Layering: the profile's capability/quality knobs apply FIRST (explicitly
    # selected, so name-mismatch reuse is allowed); the harness's corpus-tuned
    # values below (max_tokens sizing, context window, timeouts) then override
    # the knobs the harness knows better empirically; CAPYBASE_CONTEXT_WINDOW
    # and friends remain the final per-run env overrides.
    resolved = _require_provider()
    cfg, _profile_knobs = apply_to_config(cfg, resolved)
    cfg.model.temperature = 0.2
    # A/B kill switch for the whole-file takeover mechanisms (regression
    # attribution): setting CAPYBASE_DISABLE_TAKEOVER=1 runs the pre-af41b2e
    # resolution flow (no Phase-1 fast path, no asymmetry takeover, no
    # mid-band subsumption) so a failing case can be rerun to decide whether
    # the takeover or the underlying cascade is at fault.
    if os.environ.get("CAPYBASE_DISABLE_TAKEOVER", "") == "1":
        cfg.future.enable_true_side_asymmetry_takeover = False
        cfg.future.enable_midband_subsumption_takeover = False
        cfg.future.enable_wholesale_winner_floor = False
    # Output token cap proportional to conflict size: a 3-line conflict doesn't
    # need 8K tokens of generation headroom (the model would hallucinate
    # boilerplate, wasting time on the slow endpoint). Cap at 16× the conflict's
    # non-blank line count, ceiling at 8192. Floor at 2048 (was 512): the gemma
    # server bills a large hidden prefill against the completion budget
    # (~800 tokens on a 5K-char prompt, per the adjudication pre-fill
    # measurements) — a 512-1120 token cap leaves ~0-300 effective output
    # tokens, so the model's merge gets cut mid-JSON with finish_reason=length
    # (tokio-0108's attempt2 produced a CORRECT merge cut at 696 chars;
    # flask-0006's bigger prompt starved the output to empty). The four
    # "truncation-looping" specimen cases are this starvation, not looping.
    _conflict_lines = sum(1 for ln in (case.marker_original or "").splitlines() if ln.strip())
    cfg.model.max_tokens = min(8192, max(2048, _conflict_lines * 16))
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
    # Self-consistency is DISABLED (samples=1). With n=2, Shannon entropy is
    # binary {0,1}, so the consensus entropy gate escalates on ANY disagreement
    # — even when both candidates are valid. The intent coverage ranker still
    # runs (it's a no-op with 1 candidate). Enable with samples>=3 (odd) for a
    # stronger model.
    # Test gate:
    # - Python: py_compile (always available)
    # - Rust with full crate: the orchestrator's _run_cargo_syntax_check runs
    #   `cargo check` naturally (it finds the real Cargo.toml). We don't need
    #   a separate test command — the syntax validator IS the cargo check.
    # - Rust without crate: 'true' (brace-balance is the only gate).
    if case.language == "python":
        cfg.tests.pre_continue = f"python3 -m py_compile {case.path}"
    elif case.language in ("c", "cpp", "c++"):
        # The in-loop whole-tree gate. The build command is matched to whatever
        # prepare actually ran in _materialize_conflict (stored in
        # _DETECTED_BUILD_CMD). This handles C repos that changed build systems
        # across their history (cmake → autotools fallback). Falls back to the
        # per-dataset default from C_BUILD_COMMANDS, then "true".
        cfg.tests.pre_continue = (_DETECTED_BUILD_CMD.get(case.id)
                                  or C_BUILD_COMMANDS.get(case.dataset, "")
                                  or "true")
    else:
        cfg.tests.pre_continue = "true"
    cfg.tests.final = cfg.tests.pre_continue
    cfg.tests.required = False  # harness judges; don't double-gate
    # Build-target narrowing: for sqlite and redis, compile only the conflict
    # file's translation unit instead of the full project. sqlite's Makefile
    # has per-object rules (delete.o:, update.o:, etc.) and redis has a %.o
    # pattern rule. This cuts build verification from ~54s (full make) to
    # ~2-5s (single object). Falls back to full build if no target rule.
    # json-c uses cmake (awkward per-object targets); leave empty.
    _C_BUILD_TARGETS = {
        "sqlite-history": "make {stem}.lo",
        "redis-history": "make {stem}.o",
    }
    if case.language in ("c", "cpp", "c++"):
        _target = _C_BUILD_TARGETS.get(case.dataset, "")
        if _target:
            cfg.validation.cc_build_target_template = _target
    cfg.future.enable_structural_resolver = True
    # Sprint-23 mechanisms: F1 is always-on (smart conditions in the
    # orchestrator); R3 best-of-N is config-gated (default False,
    # enabled here for the specimen/full runs)
    cfg.future.enable_best_of_n = True
    cfg.future.enable_combination_search = True
    # Sprint-21 S21.5 cohort validation: the member-split composition is
    # OFF by default; the env gate flips it for the validation run (the
    # pre-registered acceptance: 15-case oversized cohort, majority-of-3,
    # prompts under the 8K window, sqlite entity-splitting must-hold).
    if os.environ.get("CAPYBASE_ENABLE_MEMBER_SPLIT", "") == "1":
        cfg.future.enable_class_member_splitting = True
    # Sprint-21 few-shot A/B: golden-path experiment gate. Enables the
    # POLICY (user directive, 2026-08-28): the memory path is RELEVANT
    # IN ACTUAL USE but DISABLED IN EVAL RUNS. In production the store
    # self-populates from the user's own accepted resolutions under
    # their current toolchain — freshness is inherent. In evals a seeded
    # store replays STALE resolutions (the 2026-08-28 harvest incident:
    # 5 baseline PASSes flipped because exact_reuse replayed sprint-21-
    # era resolutions that no longer compile), and any memory-on number
    # stops being apples-to-apples with prior rows. Default OFF; the
    # flag remains ONLY for a deliberate, journaled A/B with a re-seeded
    # and freshness-validated store (next sprint's design).
    cfg.memory.enabled = False
    cfg.future.enable_rag = False
    if os.environ.get("CAPYBASE_GOLDEN_PATH", "") == "1":
        cfg.memory.enabled = True
        cfg.future.enable_rag = True
        cfg.memory.store_path = os.environ.get(
            "CAPYBASE_MEMORY_DIR",
            "/var/tmp/capybase-live/s21/memory/experiences.jsonl")
        print("!! CAPYBASE_GOLDEN_PATH=1: memory ON in an EVAL run — "
              "policy violation unless this is the deliberate, "
              "freshness-validated A/B. Baseline rows must be memory-off.")
    cfg.policy.max_retries_per_unit = 2  # cap CEGIS retries for throughput
    # Disable the verifier model jury for high-region-count conflicts.
    # The jury makes 4 separate LLM calls (model + assertion + reflection +
    # guardrail) per non-fast_verify unit, at ~12s each = 48s per unit.
    # For 89-region files, even 7 non-deterministic units × 48s = 336s →
    # timeout. The Phase 2 whole-file build gate is the real verifier.
    # Threshold: >40 non-blank conflict lines ≈ >10 regions (each region
    # has ~3-4 non-blank lines: base/current/replayed).
    if _conflict_lines > 120:
        cfg.validation.enable_verifier_model = False
        cfg.validation.enable_verifier_assertion = False
        cfg.validation.enable_verifier_reflection = False
        cfg.validation.enable_verifier_guardrail = False
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
    # File-level wall deadline: an outer cap on total resolution + repair time
    # per file. The whole-file repair loop creates nested _resolve_unit calls,
    # each with a fresh 360s per-unit budget — without this cap, 2 repair
    # iterations × ~5 model calls × ~100s = ~1000s, blowing the 900s case
    # timeout. 600s gives each file a generous shot while leaving 300s headroom
    # for materialization + build preparation under the case cap.
    cfg.policy.max_wall_time_per_file_seconds = 600
    # Whole-file repair retries: 1 (down from 2). For a ~100s/generation model,
    # 2 repair iterations × nested _resolve_unit is too generous — the first
    # repair attempt is the most likely to converge; subsequent retries on the
    # same conflict rarely produce a better result (the convergence detector
    # already catches identical failures). Combined with the file-level
    # deadline, this ensures cases complete within the timeout.
    cfg.policy.max_whole_file_repair_retries = 1
    # Tiered Phase 2 verification: bound the whole-file repair loop to 200s
    # wall time with at most 1 model re-resolve. This replaces the
    # multi-iteration CEGIS loop that could run 3-6 × (100s model + 75s
    # build) = 525-1050s, blowing the 900s case timeout for sqlite.
    cfg.policy.max_whole_file_repair_seconds = 200
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
    """True if the text contains git conflict markers.

    Checks for ``<<<<<<<``, ``=======``, ``>>>>>>>`` at the START of a line
    (after whitespace stripping). This avoids false positives from comment
    decorators like ``// ===================================================================``
    (common in protobuf/Google C++ style) which contain ``=======`` as a
    substring but are not conflict markers.
    """
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("<<<<<<<") or stripped.startswith(">>>>>>>"):
            return True
        # Git's conflict separator is exactly 7 '=' at line start (after
        # stripping). Comment decorators have a non-'=' prefix (// or #) or
        # more than 7 '=' and must NOT match.
        if stripped == "=======":
            return True
    return False


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

    LINKER-ERROR TOLERANCE: a build that fails only at the link step
    (collect2/ld, multiple-definition, undefined-reference) is treated as a
    COMPILE PASS. This mirrors the orchestrator's own verification logic:
    linker errors are infrastructure (vendored deps compiled with
    conflicting flags, modern GCC's -fno-common default breaking older
    headers, missing sibling objects) — NOT model defects. Without this,
    12 redis cases at sim=1.00 (perfect oracle merge) were classified as
    'divergent' solely because redis's vendored hiredis/junkalloc header
    defines globals that multiply-define under -fno-common.
    """
    # Prefer the adaptively-detected build command (set by _materialize_conflict
    # via _resolve_c_build), which matches whatever prepare actually ran. Falls
    # back to the static per-dataset default.
    cmd = _DETECTED_BUILD_CMD.get(case.id) or C_BUILD_COMMANDS.get(case.dataset, "")
    if not cmd or cmd == "true":
        return None
    try:
        proc = _run_shell_tree(cmd, cwd=str(repo), timeout=300)
        if proc.returncode == 0:
            return True
        stderr = (proc.stderr or "") + (proc.stdout or "")
        err_lines = stderr.splitlines()
        # Linker error → compile passed; link is infrastructure.
        is_linker_error = any(
            sig in stderr for sig in
            ("collect2:", "ld returned", "undefined reference",
             "multiple definition")
        )
        if is_linker_error:
            return True
        # Sibling-file error → the error is in a file the merge didn't touch.
        # Mirrors the verification engine's error-localization logic: parse the
        # gcc file:line:col: prefix and compare against the conflict file stem.
        # A whole-tree build (make) compiles many TUs; a pre-existing error in
        # tool/lemon.c or deps/hiredis.c is NOT a merge defect.
        from pathlib import Path as _P
        import re as _re
        conflict_stem = _P(case.path).stem
        _file_re = _re.compile(r"([^\s:][^\s:]*?)\.([chp]+)(?:\+\+)?:\d+:\d+:\s*(?:error|warning):", _re.IGNORECASE)
        has_conflict_file_error = False
        for ln in err_lines:
            if "error" not in ln.lower():
                continue
            # Skip make/cmake driver lines.
            if (ln.startswith("make[") or ln.startswith("make:")
                    or "CMake Error" in ln or ln.startswith("ninja:")
                    or "Error 1" in ln or "Error 2" in ln):
                continue
            m = _file_re.search(ln)
            if m:
                stem = _P(m.group(1) + "." + m.group(2)).stem
                if stem == conflict_stem:
                    has_conflict_file_error = True
                    break
        if not has_conflict_file_error:
            # All errors are in sibling files, -Werror, or build-driver lines →
            # the merge compiled fine; build failure is pre-existing infrastructure.
            return True
        return False
    except Exception:  # noqa: BLE001 — best-effort; treat as "couldn't check"
        return None


_CC_ERROR_LINE_RE = re.compile(r"^\S+?:\d+:\d+:\s*(error:.*)$")

# Environmental failure signatures (sprint-20 E2, post-reboot find): a
# dependency-fetch/network failure is identical across all three probe
# texts BY CONSTRUCTION (it never depends on content), so the strict
# "identical signatures" condition is trivially satisfied — sea-orm
# 0002/0009/0010/0011/0012/0028 were misclassified era-dead on exactly
# this ("failed to get `sea-query` as a dependency"). The probe only
# classifies TOOLCHAIN-era compile errors, never environment failures.
_PROBE_ENVIRONMENTAL_PATTERNS = (
    "failed to get ", "failed to fetch", "no matching package named",
    "network", "registry error", "does not have a lock file",
    "failed to download",
)


def _compile_error_signature(output: str, language: str) -> list[str]:
    """Normalized compile-error messages from a gate/cargo output.

    rustc/cargo top-level error lines carry no location prefix
    (``error[E0308]: msg``) and are kept whole; gcc/clang lines carry
    ``file:line:col:`` prefixes that are stripped (the same conflict
    file is compiled in every probe — locations carry no signal).
    Sorted+deduped so outputs compare by content, not position. An
    EMPTY signature means "no real compile errors" (usage text, make
    driver noise, environment output) and can never classify a case —
    the cf50f4b broken-gate class produces no signature.
    """
    sig: set[str] = set()
    for ln in (output or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if language == "rust":
            if ln.startswith("error[") or ln.startswith("error:"):
                # Cargo/rustc driver summaries carry counts that vary with
                # warning totals ("...due to 6 previous errors; 71 warnings
                # emitted") — not error content, and they break signature
                # equality between sides that differ by a single warning
                # (tokio-0109: 71 vs 70). Exclude them.
                if (ln.startswith("error: could not compile")
                        or ln.startswith("error: aborting due to")):
                    continue
                sig.add(ln)
        else:
            m = _CC_ERROR_LINE_RE.match(ln)
            if m:
                sig.add(m.group(1).strip())
    return sorted(sig)


def _toolchain_era_probe(repo: Path, case: "Case", *, has_crate: bool) -> dict | None:
    """Sprint-20 S20.2: compile both pristine sides + the oracle in the
    materialized worktree BEFORE the resolution pipeline runs.

    tokio-0109 lesson: when historical code (both sides AND the oracle)
    doesn't compile under the eval's newer toolchain, the case is
    un-passable — the full majority-of-3 pipeline can only ever produce
    an honest escalate, at full budget cost. Classification is strict:
    all three texts fail the REAL gate command, the failures carry real
    compile errors, and the two sides' normalized error signatures are
    IDENTICAL (era-intrinsic, not content-dependent). Anything less
    returns toolchain_dead=False and the case runs normally.

    Returns a probe dict (cached per case id across majority repeats —
    the pristine sides and the oracle don't change between runs) or
    None when no usable in-crate gate exists: python (no era class),
    rust without a crate (standalone rustc on one file fails on
    ``use crate::`` paths for era-independent reasons — not evidence),
    or a degraded/absent C gate command.
    """
    if case.language == "python":
        return None
    if case.language == "rust" and not has_crate:
        return None
    if case.language == "rust":
        gate = "cargo check"
    else:
        gate = (_DETECTED_BUILD_CMD.get(case.id)
                or C_BUILD_COMMANDS.get(case.dataset, ""))
    if not gate or gate == "true":
        return None
    target = repo / case.path
    if not target.exists():
        return None
    saved = target.read_bytes()
    probes: dict[str, dict] = {}
    try:
        for name, text in (("current", case.current),
                           ("replayed", case.replayed),
                           ("oracle", case.expected_resolved)):
            target.write_text(text, encoding="utf-8")
            try:
                proc = _run_shell_tree(gate, cwd=str(repo), timeout=240)
                out = (proc.stderr or "") + (proc.stdout or "")
                probes[name] = {
                    "rc": proc.returncode,
                    "sig": _compile_error_signature(out, case.language),
                }
            except Exception as exc:  # noqa: BLE001 — probe is best-effort
                probes[name] = {
                    "rc": None, "sig": [],
                    "error": f"{type(exc).__name__}: {str(exc)[:120]}",
                }
    finally:
        target.write_bytes(saved)  # restore the conflicted content exactly
    # Sprint-24 cycle-G: the conditional-omission case. A full-tree gate
    # can pass rc=0 for all three texts while the CONFLICT FILE's target is
    # conditionally omitted from the build — sqlite's configure drops the
    # tcl extension when tcl.h is absent, so `make -j12` "builds the oracle"
    # without ever compiling tclsqlite.c (sqlite-0040: oracle probe rc 0
    # while every resolution-time `make tclsqlite.lo` died on tcl.h). Probe
    # the conflict file's own object target with the oracle text in place:
    # when it fails (and the rule exists), the file-level gate can never
    # validate ANY resolution — toolchain-dead for the conflict file.
    target_probe = None
    _target_sigs: dict[str, list[str]] = {}
    if (case.language in ("c", "cpp", "c++")
            and probes and all(p["rc"] == 0 for p in probes.values())):
        stem = Path(case.path).stem
        try:
            for suffix in (".lo", ".o"):
                cmd = f"make {stem}{suffix}"
                _suffix_ok = True
                for name, text in (
                        ("oracle", case.expected_resolved),
                        ("current", case.current),
                        ("replayed", case.replayed)):
                    target.write_text(text, encoding="utf-8")
                    tp = _run_shell_tree(cmd, cwd=str(repo), timeout=120)
                    out = (tp.stderr or "") + (tp.stdout or "")
                    if "No rule to make target" in out:
                        _suffix_ok = False
                        break  # wrong suffix — try the other
                    _target_sigs[name] = _compile_error_signature(
                        out, case.language)[:5]
                if _suffix_ok:
                    target_probe = {
                        "cmd": cmd,
                        "rc": 0 if not _target_sigs.get("oracle") else 2,
                        "sig": _target_sigs.get("oracle", [])[:3],
                        "sides_sig": _target_sigs.get("current", [])[:3],
                    }
                    break
        except Exception:  # noqa: BLE001 — probe is best-effort
            target_probe = None
        finally:
            target.write_bytes(saved)
    # Mixed-signature semantics (sprint-21 S21.1): the 8 "environmentally
    # purged" cases re-ran and re-classified era-dead legitimately — their
    # probes carried environmental lines AND genuine era compile errors.
    # Declining on ANY environmental line over-triggers (false-negative
    # corrections); decline only when EVERY signature line is
    # environmental (a pure environment failure has no content signal).
    _env_lines = sum(
        1 for p in probes.values() for s in (p.get("sig") or [])
        if any(pat in s for pat in _PROBE_ENVIRONMENTAL_PATTERNS))
    _total_lines = sum(
        len(p.get("sig") or []) for p in probes.values())
    _environmental = _total_lines > 0 and _env_lines == _total_lines
    dead = (
        probes["current"]["rc"] not in (0, None)
        and probes["replayed"]["rc"] not in (0, None)
        and probes["oracle"]["rc"] not in (0, None)
        and bool(probes["current"]["sig"])
        and probes["current"]["sig"] == probes["replayed"]["sig"]
        and not _environmental
    )
    # Conditional-omission dead: full gate green everywhere, but the
    # oracle's own conflict-file target fails — the pass-criterion file is
    # not in the build the gate measures.
    if (not dead and target_probe is not None
            and target_probe["rc"] not in (0, None)):
        dead = True
    # Signature equivalence (sprint-25 item 2): when ALL THREE texts fail
    # the conflict-target build with IDENTICAL signatures, the errors are
    # era/content-intrinsic (redis-0049: the era code lives in the sides
    # and the oracle alike) — the resolver cannot distinguish its output
    # from the human resolution. Semantically honest: identical signatures
    # across all three is content evidence, not denominator trimming.
    _all3 = (_target_sigs.get("oracle") and _target_sigs.get("current")
             and _target_sigs.get("replayed"))
    if (not dead and _all3
            and _target_sigs["oracle"] == _target_sigs["current"]
            and _target_sigs["current"] == _target_sigs["replayed"]):
        dead = True
    return {"toolchain_dead": dead, "gate": gate, "probes": probes,
            "environmental": _environmental,
            "conflict_target_probe": target_probe}


def _mark_toolchain_dead(res: "CaseResult", probe: dict, t0: float) -> "CaseResult":
    res.elapsed = time.time() - t0
    res.escalated = True
    res.toolchain_dead = True
    res.toolchain_probe = probe
    res.reason = (
        "toolchain-era: both pristine sides and the oracle fail the gate "
        f"with identical compile errors ({probe.get('gate', '')})"
    )
    return res


def _oracle_builds(repo: Path, case: Case, crate_source: Path | None) -> bool | None:
    """Does the ORACLE (expected_resolved) pass the same gate the merge faced?

    Writes expected_resolved into the materialized tree and runs the gate the
    resolver itself faced: the C/C++ tree build for c/cpp cases, the cargo
    new-error delta (vs the one-side-blanked baseline — the orchestrator's own
    baseline construction) for rust-with-crate cases. Returns None when no
    gate applies or the probe is undecidable. Used by the GATE_UNAVAILABLE
    classification: a sim >= 0.95 merge the gate rejected is a sandbox
    artifact, not a resolver failure, when the human resolution fails the
    same gate.
    """
    target = repo / case.path
    saved = target.read_bytes() if target.exists() else None
    try:
        target.write_text(case.expected_resolved)
        if case.language in ("c", "cpp", "c++"):
            return _c_builds(repo, case)
        if case.language == "rust" and crate_source is not None:
            from capybase.adapters import lsp as lsp_mod
            from capybase.verification import (
                _blank_markers_one_side,
                compute_diagnostic_delta,
            )
            runner = lsp_mod.RustAnalyzerRunner(timeout=300)
            baseline = runner.check(
                _blank_markers_one_side(case.marker_original, "rust"),
                path=case.path, repo_root=str(repo))
            oracle = runner.check(
                case.expected_resolved, path=case.path, repo_root=str(repo))
            if not baseline.checked or not oracle.checked:
                return None
            new = compute_diagnostic_delta(
                list(baseline.errors), list(oracle.errors))
            return len(new) == 0
        return None
    except Exception:  # noqa: BLE001 — best-effort probe
        return None
    finally:
        if saved is not None:
            target.write_bytes(saved)


def _token_jaccard(a: str, b: str) -> float:
    ta, tb = set(a.split()), set(b.split())
    if not ta and not tb: return 1.0
    u = ta | tb
    return len(ta & tb) / len(u) if u else 0.0


# Sprint-20 S20.11 — control-flow skeleton intent metric (EVAL ONLY;
# never a production gate — the compiler is the authority). Flags
# "idiomatic rewrites": outputs whose token similarity to the oracle is
# low but whose structural intent (the ordered control-flow/definition
# keyword stream) is preserved. Informs future metric design; the
# verdict chain is untouched.
_SKELETON_KEYWORDS = frozenset(
    "if else elif for while do switch case default match guard try catch "
    "finally return break continue throw raise yield def fn func impl "
    "trait class struct enum interface namespace union typedef".split())


def _skeleton_signature(text: str) -> list[str]:
    """Ordered control-flow/definition keyword stream — the code's
    structural intent, ignoring naming, formatting, and idiom swaps."""
    import re as _re
    return [t for t in _re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text or "")
            if t in _SKELETON_KEYWORDS]


def _skeleton_similarity(a: str, b: str) -> float:
    """Sequence similarity of two skeletons (difflib ratio on the keyword
    streams). High similarity with LOW token jaccard flags an idiomatic
    rewrite rather than a wrong merge."""
    import difflib as _dl
    sa, sb = _skeleton_signature(a), _skeleton_signature(b)
    if not sa or not sb:
        return 0.0
    return _dl.SequenceMatcher(None, sa, sb, autojunk=False).ratio()


#: Minimum per-side preservation for the WORKING verdict: the output must
#: carry at least this share of EACH side's changed-line content, so the
#: label means "both sides' work survived", not "half a merge".
WORKING_PRESERVATION_MIN = 0.50


def _norm_lines(text: str) -> set[str]:
    return {ln.strip() for ln in text.splitlines() if ln.strip()}


def _side_preservation(base_text: str, side_text: str, output_text: str) -> float | None:
    """Share of one side's changes the output preserves (0.0–1.0).

    Added/changed lines (the side's side of the base diff) count as
    preserved when present in the output; deleted base lines count as
    preserved when absent from it. Whitespace-normalized line-set
    membership, so indentation drift doesn't dent the fraction. Returns
    None when the side made no changes vs base (nothing to preserve — the
    WORKING property is vacuous, not satisfied).
    """
    import difflib as _dl

    b, s = base_text.splitlines(), side_text.splitlines()
    added: list[str] = []
    deleted: list[str] = []
    for tag, i1, i2, j1, j2 in _dl.SequenceMatcher(None, b, s, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if tag != "delete":
            added.extend(s[j1:j2])
        if tag != "insert":
            deleted.extend(b[i1:i2])
    if not added and not deleted:
        return None
    out = _norm_lines(output_text)
    n_ok = 0
    n_tot = 0
    for ln in added:
        if ln.strip():
            n_tot += 1
            n_ok += ln.strip() in out
    for ln in deleted:
        if ln.strip():
            n_tot += 1
            n_ok += ln.strip() not in out
    return n_ok / n_tot if n_tot else None


def _preservation_fields(case, content: str) -> tuple[float | None, float | None]:
    """(loser, winner) side-preservation fractions for a resolved output.

    Loser/winner by full-file churn (the merge_intent seam) — the loser is
    the lower-churn side whose small changes the oracle may have dropped.
    """
    if not content:
        return None, None
    try:
        from capybase.merge_intent import side_churn
        c = side_churn(case.base, case.current)
        r = side_churn(case.base, case.replayed)
        loser_side, winner_side = (
            (case.replayed, case.current) if c >= r else (case.current, case.replayed))
        return (
            _side_preservation(case.base, loser_side, content),
            _side_preservation(case.base, winner_side, content),
        )
    except Exception:
        return None, None


def _is_working(r: "CaseResult") -> bool:
    """WORKING: compiling, marker-free, below the PASS bar, and preserving
    both sides' changes — a functioning both-features merge the oracle
    diverged from for out-of-band (human/planning) reasons."""
    if (
        not r.escalated
        and r.marker_free
        and r.compiles
        and r.matches_oracle < PASS_THRESHOLD
        and getattr(r, "output_tests", None) is True
    ):
        return True  # the project's own tests accept the merge
    return (
        not r.escalated
        and r.marker_free
        and r.compiles
        and r.matches_oracle < PASS_THRESHOLD
        and r.loser_preservation is not None
        and (r.winner_preservation or 0.0) >= WORKING_PRESERVATION_MIN
        and r.loser_preservation >= WORKING_PRESERVATION_MIN
    )


def _verdict_chain(r: "CaseResult") -> str:
    """The pure per-run verdict chain (module-level so tests can pin it).

    ESCALATE / PASS / WORKING / NEAR_MATCH / ORACLE_DIVERGENT per the fields,
    then the GATE_UNAVAILABLE override: a sim >= 0.95 gate rejection where
    the ORACLE fails the same gate (oracle_builds probed on the live tree) is
    a sandbox artifact, not a resolver failure. ESCALATE_TOOLCHAIN comes
    first: the preflight proved all three texts (both sides + oracle) fail
    the gate identically — the case is un-passable by construction."""
    if getattr(r, "toolchain_dead", False):
        return "ESCALATE_TOOLCHAIN"
    if r.escalated:
        verdict = "ESCALATE"
    elif r.marker_free and r.compiles:
        if r.matches_oracle >= PASS_THRESHOLD:
            verdict = "PASS"
        elif _is_working(r):
            verdict = "WORKING"
        elif r.matches_oracle >= 0.80:
            verdict = "NEAR_MATCH"
        else:
            verdict = "ORACLE_DIVERGENT"
    else:
        verdict = "ORACLE_DIVERGENT"
    if (verdict in ("ESCALATE", "ORACLE_DIVERGENT")
            and getattr(r, "oracle_builds", None) is False
            and r.matches_oracle >= 0.95):
        return "GATE_UNAVAILABLE"
    return verdict


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
    res.conflict_region_count = case.marker_original.count("<<<<<<<")
    t0 = time.time()
    # Sprint-20 S20.2: a case already classified toolchain-era by an
    # earlier repeat skips even materialization — the pristine sides and
    # the oracle are identical across repeats by construction.
    _cached_probe = _TOOLCHAIN_PROBE_CACHE.get(case.id)
    if _cached_probe is not None and _cached_probe.get("toolchain_dead"):
        return _mark_toolchain_dead(res, _cached_probe, t0)
    owns_td = td is None
    if owns_td:
        # Worktrees live on the TMPFS (/tmp): fast builds, wiped on
        # reboot (disposable by design — results/journals are disk-backed
        # under /var/tmp). Env-overridable.
        td = tempfile.mkdtemp(
            prefix="capy-rw-",
            dir=os.environ.get("CAPYBASE_WORKTREE_DIR", "/tmp"))
    try:
        repo = Path(td) / "r"
        try:
            _materialize_conflict(case, repo, crate_source=crate_source)
        except _NoConflictError as exc:
            res.elapsed = time.time() - t0
            # Sprint-22 P1: git resolved cleanly — nothing to resolve.
            # Mark as SAFE_SKIP (excluded from the real-conflict
            # denominator) instead of counting it as a failure.
            res.escalated = True
            res.terminal_reason = "SAFE_SKIP"
            res.reason = f"skipped (no conflict): {exc}"
            return res
        except Exception as exc:
            res.elapsed = time.time() - t0
            res.reason = f"setup failed: {type(exc).__name__}: {str(exc)[:100]}"
            res.escalated = True
            return res
        # Sprint-20 S20.2: toolchain-era preflight — one probe triple per
        # case (cached across repeats), strictly before the pipeline
        # spends any budget. Declines to classify on anything short of
        # identical real compile errors on both sides AND an oracle
        # failure; passable cases are behavior-identical (the probes
        # restore the conflicted file byte-exact and warm the build).
        if _cached_probe is None:
            _cached_probe = _toolchain_era_probe(
                repo, case, has_crate=crate_source is not None)
            _TOOLCHAIN_PROBE_CACHE[case.id] = _cached_probe
        if _cached_probe is not None and _cached_probe.get("toolchain_dead"):
            return _mark_toolchain_dead(res, _cached_probe, t0)
        if _cached_probe is not None:
            # Declined classification — still recorded for the audit trail
            # (the harvest census reads per-case probe outcomes).
            res.toolchain_probe = _cached_probe
        cfg = _config_for(case, has_crate=crate_source is not None)
        engine = ResolutionEngine(cfg.model, client=client)
        orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                            out=lambda *_a, **_k: None)
        try:
            step = orch.run()
            res.escalated = bool(step.escalated)
            res.reason = step.reason or ""
        except Exception as exc:
            # A swallowed orchestrator exception is undiagnosable from the
            # truncated reason alone (jsonc-0001's TypeError hid for a whole
            # soak). Print the full traceback to stderr — it lands in the
            # per-case log next to the summary.
            import traceback as _tb
            _tb.print_exc()
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
        if case.language in ("c", "cpp", "c++") and content:
            c_builds_result = _c_builds(repo, case)
        # WS1c oracle-build-check — only for cases heading to a non-clean
        # verdict (cost: one tree build / two cargo runs per failing case;
        # clean passes never need reclassification). The predicate mirrors
        # the verdict chain: escalated, markers left, empty output, or a
        # structural-gate failure on a code file.
        # E1 (sprint-22): ALSO fire when the c/cpp tree build FAILED on a
        # marker-free, non-escalated buffer — the four cpp DIV regressions
        # (clickhouse-0049 et al.) left oracle_builds=None, so the
        # GATE_UNAVAILABLE sandbox-artifact rescue could not even be
        # evaluated. Probing classifies them honestly instead.
        oracle_builds_result: bool | None = None
        from capybase.verification import structural_gate_applies as _sga_probe
        _gate_failed_clean_buffer = (
            c_builds_result is False and not res.escalated and bool(content)
            and not _contains_markers(content))
        if content and (
                res.escalated
                or _contains_markers(content)
                or (_sga_probe(case.path)
                    and not _brace_balanced(content, case.language))
                or _gate_failed_clean_buffer):
            try:
                oracle_builds_result = _oracle_builds(repo, case, crate_source)
            except Exception:  # noqa: BLE001 — classification is best-effort
                oracle_builds_result = None
        # Sprint-25 decisions 1+3: the output-tests probe. When the merge is
        # marker-free, non-escalated, and BELOW the PASS bar (the divergent
        # band — clear PASSes never pay the test cost), run the dataset's
        # test command on the output tree. A pass upgrades the verdict to
        # WORKING: the project's own tests accept the merge. A timeout or
        # infrastructure failure records None (not False) — an unrunnable
        # suite says nothing about the merge.
        output_tests_result: bool | None = None
        _test_cmd = C_TEST_COMMANDS.get(case.dataset, "")
        if (_test_cmd and content and not res.escalated
                and not _contains_markers(content)
                and _token_jaccard(content, case.expected_resolved)
                < PASS_THRESHOLD):
            try:
                _tp = _run_shell_tree(_test_cmd, cwd=str(repo), timeout=900)
                _t_out = (_tp.stderr or "") + (_tp.stdout or "")
                if ("timed out after" in _t_out
                        or getattr(_tp, "timed_out", False)):
                    output_tests_result = None
                elif any(_pat in _t_out for _pat in (
                        # Environmental failures say nothing about the merge:
                        # offline cargo/ctest cannot fetch or configure — a
                        # suite that cannot RUN is not a suite that failed.
                        "failed to download", "network", "offline",
                        "does not have a lock file", "no such file or "
                        "directory: Cargo.lock", "error: could not find "
                        "`cargo`", "ctest: not found")):
                    output_tests_result = None
                else:
                    output_tests_result = _tp.returncode == 0
            except Exception:  # noqa: BLE001 — probe is best-effort
                output_tests_result = None
    finally:
        # D3: when the main thread owns the temp dir, it cleans up after the
        # worker returns or times out. When we own it, clean up here.
        if owns_td:
            import shutil
            shutil.rmtree(td, ignore_errors=True)
    res.elapsed = time.time() - t0
    res.oracle_builds = oracle_builds_result
    res.output_tests = output_tests_result
    res.marker_free = not _contains_markers(content) if content else False
    # Non-code files (markdown, lockfiles, prose): marker-free is the only
    # structural gate. Brace-balance on prose rejects perfect merges — a
    # CHANGELOG code fence or template placeholder with an unbalanced brace
    # classified four sim-1.000 axum CHANGELOG.md merges as ORACLE_DIVERGENT
    # (sprint-16 census). The C build gate is skipped too: a docs-only
    # conflict can't affect compilation, so the tree build's outcome would
    # be orthogonal to the merge.
    from capybase.verification import structural_gate_applies as _sga
    if not content:
        res.compiles = False
    elif not _sga(case.path):
        res.compiles = True
    elif case.language == "python":
        res.compiles = _py_compiles(content)
    elif case.language in ("c", "cpp", "c++"):
        # Use the build verdict captured before cleanup; fall back to brace-
        # balance if the build couldn't run (no command registered or no tree).
        res.compiles = c_builds_result if c_builds_result is not None else (
            _brace_balanced(content, case.language)
        )
    else:
        res.compiles = _brace_balanced(content, case.language)
    res.matches_oracle = _token_jaccard(content, case.expected_resolved) if content else 0.0
    # Sprint-20 S20.11: skeleton intent similarity (EVAL ONLY — never a
    # gate). Recorded on every result; the harvest cross-tabs it against
    # matches_oracle to surface idiomatic-rewrite candidates (low jaccard,
    # high skeleton).
    res.skeleton_similarity = (
        _skeleton_similarity(content, case.expected_resolved)
        if content else 0.0)
    res.loser_preservation, res.winner_preservation = _preservation_fields(case, content)
    return res


def _print_census(results_path: str) -> None:
    """Print a failure census report from an existing results JSON.

    Classifies each escalated case by root diagnostic category using
    ``_classify_ccs_parse_error`` and pattern matching on the reason string.
    The reviewer feedback's Stage A recommendation: don't build more rules
    blindly — classify the actual failures first. Makes every future run
    self-documenting.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from capybase.verification import _classify_ccs_parse_error
    from collections import Counter

    results = json.loads(Path(results_path).read_text())
    cases = results if isinstance(results, list) else results.get("cases", [])

    def classify(r: dict) -> str:
        reason = r.get("reason", "")
        terminal = r.get("terminal_reason", "")
        if not r.get("escalated"):
            return "RESOLVED"
        # Sprint-20 S20.2: un-passable under this toolchain — not a
        # resolver failure, budget was never spent.
        if r.get("toolchain_dead") or "toolchain-era" in reason:
            return "toolchain_era"
        # Try gcc parse-error classification first
        cat = _classify_ccs_parse_error(reason)
        if cat:
            return cat
        # Infrastructure / build-system categories
        if "build is not a directory" in reason or "cmake" in reason.lower():
            return "build_system_config"
        if "collect2" in reason or "ld returned" in reason:
            return "linker_error"
        if "lemon.c" in reason or "tool/" in reason:
            return "pre_existing_tool_error"
        if "could not re-resolve" in reason:
            return "repair_loop_exhausted"
        if "splice coherence" in reason or "brace" in reason.lower():
            return "brace_imbalance"
        if "oversized prompt" in reason or terminal == "OVERSIZED":
            return "oversized_prompt"
        if "no hard-failure progress" in reason or terminal == "CARGO_NO_PROGRESS":
            return "no_progress_loop"
        if "GitError" in reason or terminal == "OTHER":
            return "git_state_error"
        if "needs_human" in reason.lower() or terminal == "MODEL_NEEDS_HUMAN":
            return "model_needs_human"
        if "undeclared" in reason or "unknown type" in reason:
            return "semantic_resolution"
        if "incomplete type" in reason:
            return "semantic_incomplete_type"
        return "unclassified"

    cats = Counter()
    details: dict[str, list[tuple[str, str]]] = {}
    for c in cases:
        cat = classify(c)
        cats[cat] += 1
        details.setdefault(cat, []).append((c.get("id", "?"), c.get("reason", "")[:120]))

    total = len(cases)
    escalated = sum(1 for c in cases if c.get("escalated"))
    resolved = total - escalated

    print("=" * 64)
    print("FAILURE CENSUS REPORT")
    print("=" * 64)
    print(f"Total cases:      {total}")
    print(f"Resolved:         {resolved}")
    print(f"Escalated:        {escalated}")
    print()
    print("Escalation root-diagnostic distribution:")
    for cat, count in cats.most_common():
        if cat == "RESOLVED":
            continue
        pct = 100 * count / max(escalated, 1)
        print(f"  {cat:35} {count:3d} ({pct:.0f}%)")
        for id, reason in details[cat][:2]:
            print(f"    {id:30} {reason[:90]}")
        if len(details[cat]) > 2:
            print(f"    ... ({len(details[cat]) - 2} more)")
    print()
    # Near-miss analysis
    near = [c for c in cases if c.get("escalated") and c.get("matches_oracle", 0) >= 0.95]
    print(f"Near-misses (sim >= 0.95): {len(near)} of {escalated} escalations")
    print(f"  (these are the highest-ROI repair targets)")


def _kill_stale_build_processes():
    """Sweep stale build processes via the shared capybase hygiene module.

    Kept as a thin wrapper so the atexit registration and the module's
    historical call sites stay stable; the implementation (cmdline + cwd
    matching, /proc scan, 5s budget) lives in capybase.process_hygiene so
    every entry point shares one net (sprint-20 S20.5b).
    """
    from capybase.process_hygiene import kill_stale_build_processes
    kill_stale_build_processes()


def main():
    # Kill stale compiler/ccache processes from previous runs before starting.
    _kill_stale_build_processes()
    import atexit
    atexit.register(_kill_stale_build_processes)
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument(
        "--provider", default=None, metavar="NAME_OR_PATH",
        help="provider config: a name under ~/.config/capybase/providers/ or a "
             "JSON path (canonical endpoint mechanism; env: CAPYBASE_PROVIDER)",
    )
    ap.add_argument("--base-url", default=None,
                    help="explicit LLM endpoint override (env: CAPYBASE_BASE_URL)")
    ap.add_argument("--model", default=None,
                    help="explicit model id override (env: CAPYBASE_MODEL)")
    ap.add_argument("--api-key", default=None,
                    help="explicit API key override (env: CAPYBASE_API_KEY)")
    ap.add_argument("--profile", default=None,
                    help="calibration profile name/path override (a run without "
                         "a profile is an error; env: CAPYBASE_PROFILE)")
    ap.add_argument("--embeddings-base-url", default=None,
                    help="explicit embeddings endpoint override")
    ap.add_argument("--embeddings-model", default=None,
                    help="explicit embeddings model override")
    ap.add_argument("--lang", choices=("rust", "python", "c", "cpp", "c++"), default=None)
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
    ap.add_argument("--repeat-nonpass", type=int, default=1,
                    help="Variance-aware evaluation: rerun a case whose first verdict "
                         "is not PASS until this many total runs exist (3 = first run "
                         "+ 2 retries) and keep the MAJORITY verdict. A single run at "
                         "temperature > 0 is not evidence — the sprint-16 tokio-0109/"
                         "0110 chase burned an hour on what repeat runs showed was "
                         "single-run variance. The kept record is the first run with "
                         "the majority verdict; all verdicts are stored in "
                         "repeat_verdicts. Cases that PASS first try are not rerun.")
    ap.add_argument("--preserve-flights", default=None,
                    help="Directory to copy per-case orchestrator session artifacts into "
                         "(FR2a flight recorder). Produces <dir>/flights/<case_id>/<session_id>/ "
                         "and <dir>/manifest.json. Required for shadow-jury replay.")
    ap.add_argument("--census", default=None,
                    help="Print a failure census report from an existing results JSON and exit. "
                         "Classifies each escalated case by root diagnostic category. Example: "
                         "--census /tmp/capybase-live/c-live-full-corpus.json")
    args = ap.parse_args()

    # Startup sweep: remove stale capy-rw-* temp dirs from prior runs that
    # were killed (SIGTERM/SIGKILL) before their atexit handler could run.
    # These leak ~50-200MB each (full crate tree) and accumulate across
    # killed eval runs. Safe because no two eval runs should coexist.
    import glob as _glob
    import shutil as _shutil_sweep
    for _stale in (_glob.glob("/tmp/capy-rw-*")
                   + _glob.glob("/var/tmp/capy-rw-*")):
        _shutil_sweep.rmtree(_stale, ignore_errors=True)

    # Export the ccache wiring into the eval process itself so EVERY child
    # build inherits it — not just the paths that pass _ccache_env()
    # explicitly. The orchestrator's Phase-2 build gate (_run_raw_test) and
    # the TestRunner's pre_continue run `make` with the inherited
    # environment; without this they compile cold (observed: cache counters
    # frozen during build-heavy runs). The PATH shim prefix is idempotent.
    if _ccache_enabled():
        os.environ.update(_ccache_env())

    if args.census:
        _print_census(args.census)
        return

    # Shared cargo registry cache so dependencies are fetched once and reused
    # across cases (the per-case temp repo is destroyed, but the cache persists).
    # This is essential for full-crate materialization to be practical.
    os.environ.setdefault("CARGO_HOME", "/var/tmp/capybase-cargo-cache")
    # ccache is handled by capybase's verification module (_ccache_env /
    # _ccache_enabled in verification.py) — it detects ccache at runtime,
    # wires it into build commands transparently, and falls back to plain
    # gcc if ccache fails or is absent. No harness-level setup needed.

    flights_dir = Path(args.preserve_flights) if args.preserve_flights else None
    if flights_dir is not None:
        flights_dir.mkdir(parents=True, exist_ok=True)

    dropped: list[str] = []
    cases = load_cases(limit=args.limit, lang=args.lang, case_ids=args.case,
                       dropped_ids=dropped)
    # E3 (sprint-23): an empty expected_resolved is a corpus extraction
    # defect (zenodo-0044) — the case is unpassable by construction and its
    # verdicts measure nothing. Exclude at load, loudly.
    _bad_oracle = [c.id for c in cases
                   if not (c.expected_resolved or "").strip()]
    if _bad_oracle:
        print("!" * 72)
        print(f"!! EXCLUDING {len(_bad_oracle)} CASE(S) WITH EMPTY ORACLE "
              f"(corpus defect — E3): {_bad_oracle}")
        print("!" * 72, flush=True)
        _bad = set(_bad_oracle)
        cases = [c for c in cases if c.id not in _bad]
    if dropped:
        print("!" * 72)
        print(f"!! SUBSET RUN: the 48K size guard DROPPED {len(dropped)} cases"
              f" (lang={args.lang or 'all'}) — this run is INCOMPLETE for the corpus")
        print("!! Full-corpus runs must launch with: env CAPYBASE_SKIP_SIZE_GUARD=1 ...")
        print("!! (Incident 2026-08-23: shard-4 first launch ran 80/167 this way,"
              " exit=0, looked complete)")
        print("!" * 72, flush=True)
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

    global _PROVIDER
    try:
        _PROVIDER = resolve_provider(
            provider=args.provider,
            base_url=args.base_url,
            model=args.model,
            api_key=args.api_key,
            profile=args.profile,
            embeddings_base_url=args.embeddings_base_url,
            embeddings_model=args.embeddings_model,
        )
    except ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(_PROVIDER.provider.describe())

    cfg0 = _config_for(cases[0])
    client = OpenAICompatibleClient(cfg0.model)
    print(f"endpoint: {cfg0.model.base_url} model={cfg0.model.model}")

    pass_ct = sum(1 for r in results if not r.escalated and r.marker_free and r.compiles and r.matches_oracle >= PASS_THRESHOLD)
    near_ct = sum(1 for r in results if not r.escalated and r.marker_free and r.compiles and 0.80 <= r.matches_oracle < PASS_THRESHOLD)
    # Verdict-based (not field-recomputed): resumed legacy results carry no
    # preservation fields, and _is_working would read them as non-WORKING —
    # the same conservative behavior the fresh loop produces.
    working_ct = sum(1 for r in results if r.verdict == "WORKING" or _is_working(r))
    escalate_ct = sum(1 for r in results if r.escalated)
    gate_ct = sum(1 for r in results if r.verdict == "GATE_UNAVAILABLE")
    wrong_ct = sum(1 for r in results
                   if not (r.escalated or (r.marker_free and r.compiles and r.matches_oracle >= 0.80)
                           or r.verdict == "GATE_UNAVAILABLE"))
    t_start = time.time()
    skipped = 0
    # Temp dirs for timed-out cases are deferred: the daemon worker thread may
    # still be accessing them when the main thread moves on. Destroying them
    # immediately causes GitError/FileNotFoundError crashes (the race that
    # produced the infra_crash bucket in v3). Cleaned up at the end of the run.
    deferred_cleanup: list[str] = []

    # Register an atexit handler so temp dirs are cleaned up even if the run is
    # killed (Ctrl-C, OOM, crash). Without this, killed runs leak their capy-rw-*
    # dirs in /var/tmp (observed: 90 leaked dirs = 6.7G after multiple runs).
    import atexit
    import signal as _signal
    def _cleanup_eval_temp_dirs():
        for td in deferred_cleanup:
            shutil.rmtree(td, ignore_errors=True)
    atexit.register(_cleanup_eval_temp_dirs)
    # Signal handlers: atexit doesn't fire on SIGTERM (what `timeout` sends).
    # Register explicit handlers so temp dirs are cleaned up on kill.
    def _signal_cleanup(signum, frame):
        _cleanup_eval_temp_dirs()
        # Re-raise to get the correct exit code
        raise SystemExit(128 + signum)
    for _sig in (_signal.SIGTERM, _signal.SIGINT):
        _signal.signal(_sig, _signal_cleanup)
    for i, case in enumerate(cases, 1):
        if case.id in done_ids:
            skipped += 1
            continue
        print(f"[{i}/{len(cases)}] {case.id} ({case.language}/{case.dataset}) ...", end=" ", flush=True)
        # Run with a per-case wall-clock cap so one hard case (endless CEGIS
        # retries) can't stall the whole run. Implemented via a watchdog thread
        # that interrupts the worker. If the cap fires, treat as an escalate.
        import threading
        import shutil

        def _execute_case() -> CaseResult:
            """One full attempt: fresh temp repo, worker thread, wall-clock cap."""
            # D3: create the temp dir in the MAIN thread so we own cleanup. The
            # worker receives it via `td=`; if the worker times out and is
            # abandoned, the main thread cleans up here (no leaked temp trees).
            _td = tempfile.mkdtemp(
                prefix="capy-rw-",
                dir=os.environ.get("CAPYBASE_WORKTREE_DIR", "/tmp"))
            # Resolve the crate source clone for full-tree materialization.
            # Maps dataset name → external-datasets clone dir. Enables cargo check.
            _crate = None
            if case.merge_sha:
                # Map dataset name → external-datasets clone dir. The convention is
                # dataset.replace("-history",""), but some repos use a dash the
                # dataset name omits (jsonc-history → external-datasets/json-c/).
                # The CLONE_OVERRIDES table covers those exceptions; everything else
                # follows the standard convention (redis, sqlite, tokio, ...).
                # fmt-history → fmtlib-fmt: without the override the clone misses,
                # cases get single-file repos, no build gate, and no
                # compile_commands.json.
                _CLONE_OVERRIDES = {
                    "jsonc-history": "json-c",
                    "fmt-history": "fmtlib-fmt",
                }
                _clone_name = _CLONE_OVERRIDES.get(
                    case.dataset,
                    case.dataset.replace("-history", "") if case.dataset else "",
                )
                _clone_path = Path(__file__).resolve().parent.parent / "external-datasets" / _clone_name
                if _clone_path.is_dir():
                    _crate = _clone_path
            _holder: list = []

            def _worker():
                try:
                    _holder.append(run_case(case, client, flights_dir=flights_dir,
                                            td=_td, crate_source=_crate))
                except Exception as exc:
                    _holder.append(CaseResult(
                        id=case.id, language=case.language, dataset=case.dataset,
                        escalated=True,
                        conflict_region_count=case.marker_original.count("<<<<<<<"),
                        reason=f"harness error: {type(exc).__name__}: {str(exc)[:100]}"))

            _th = threading.Thread(target=_worker, daemon=True)
            _th.start()
            _th.join(timeout=args.case_timeout or None)
            # D3: clean up the temp dir from the main thread. BUT only when the
            # worker has actually finished — if the thread is still alive
            # (timeout), destroying its temp dir causes a race: the daemon
            # thread tries to access .rebase-agent/sessions/ or run git, and
            # crashes with GitError/FileNotFoundError because the directory is
            # gone. Defer cleanup to the end of the run for timed-out cases.
            if _th.is_alive():
                # The worker is still in an LLM/CEGIS loop — abandon it (daemon)
                # and record an escalate. The next case starts fresh. DON'T
                # destroy the temp dir yet — the daemon thread may still write.
                deferred_cleanup.append(_td)
                print(f"\n      [TIMEOUT after {args.case_timeout}s — moving on]", end="")
                return CaseResult(
                    id=case.id, language=case.language, dataset=case.dataset,
                    escalated=True,
                    conflict_region_count=case.marker_original.count("<<<<<<<"),
                    reason=f"case timeout after {args.case_timeout}s (endless CEGIS retries)")
            # Worker finished — safe to clean up the temp dir now.
            shutil.rmtree(_td, ignore_errors=True)
            return _holder[0] if _holder else CaseResult(
                id=case.id, language=case.language, dataset=case.dataset,
                escalated=True, conflict_region_count=case.marker_original.count("<<<<<<<"),
                reason="worker produced no result")

        def _verdict_for(res: CaseResult) -> str:
            """Delegate to the module-level chain (kept as a closure for the
            repeat loop's readability; counters stay in the caller)."""
            return _verdict_chain(res)

        r = _execute_case()
        verdict = _verdict_for(r)
        r.repeat_verdicts = []
        if args.repeat_nonpass > 1 and verdict != "PASS":
            # Variance-aware majority: rerun non-PASS cases, keep the modal
            # verdict (the first run exhibiting it) so a single lucky or
            # unlucky draw doesn't label the case.
            from collections import Counter as _VC
            _verdicts = [verdict]
            _records = [r]
            for _k in range(args.repeat_nonpass - 1):
                print(f"\n      [repeat {_k + 2}/{args.repeat_nonpass}] ...", end=" ")
                _rr = _execute_case()
                _vv = _verdict_for(_rr)
                _rr.verdict = _vv
                _verdicts.append(_vv)
                _records.append(_rr)
                print(f"{_vv}  {_rr.elapsed:.0f}s  sim={_rr.matches_oracle:.2f}", end="")
            print()
            _maj, _ = _VC(_verdicts).most_common(1)[0]
            # The first run exhibiting the majority verdict (records[0].verdict
            # isn't assigned yet — index the verdict list instead).
            _kept = _records[_verdicts.index(_maj)]
            _kept.repeat_verdicts = _verdicts
            if _kept is not r:
                print(f"      [majority: {_maj} (verdicts: {','.join(_verdicts)})]",
                      end=" ")
                r, verdict = _kept, _maj
        print(f"{verdict}  {r.elapsed:.0f}s  sim={r.matches_oracle:.2f}  {r.reason[:60]}")
        if verdict == "PASS":
            pass_ct += 1
        elif verdict == "WORKING":
            working_ct += 1
        elif verdict == "NEAR_MATCH":
            near_ct += 1
        elif verdict == "ESCALATE":
            escalate_ct += 1
        elif verdict == "GATE_UNAVAILABLE":
            gate_ct += 1
        else:
            wrong_ct += 1
        r.verdict = verdict
        r.terminal_reason = _classify_terminal_reason(r.reason) if r.escalated else ""
        # Subclassify timeouts: throughput (many regions overwhelm the budget)
        # vs capability (few regions but the model can't solve them).
        if r.terminal_reason == "TIMEOUT_CASE":
            if r.conflict_region_count > 20:
                r.terminal_reason = "TIMEOUT_THROUGHPUT"
            else:
                r.terminal_reason = "TIMEOUT_CAPABILITY"
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

    # Clean up deferred temp dirs from timed-out cases. By now all daemon
    # threads have either finished or been killed on process exit, so it's
    # safe to destroy their temp dirs.
    for td in deferred_cleanup:
        shutil.rmtree(td, ignore_errors=True)

    elapsed = time.time() - t_start
    print("\n" + "=" * 64)
    print("REALWORLD LIVE EVAL SUMMARY")
    print("=" * 64)
    print(f"cases:    {len(results)} ({skipped} resumed, {len(results)-skipped} fresh this run)")
    print(f"PASS:       {pass_ct}")
    print(f"WORKING:    {working_ct}  (compiles + preserves both sides; diverged from")
    print(f"              the human oracle for out-of-band reasons — near-success)")
    print(f"NEAR_MATCH: {near_ct}  (sim 0.80–{PASS_THRESHOLD}: defensible but imperfect)")
    print(f"ESCALATE:   {escalate_ct}")
    print(f"ORACLE_DIVERGENT: {wrong_ct}  (sim < 0.80 or marker/brace failure)")
    print(f"GATE_UNAVAILABLE: {gate_ct}  (sim >= 0.95 gate rejection the oracle "
          f"shares — sandbox artifact, not a resolver failure)")
    # Sprint-20 S20.11 (eval-only): idiomatic-rewrite candidates —
    # non-clean verdicts with content whose token jaccard to the oracle
    # is low but whose control-flow skeleton is largely preserved.
    # Diagnostic for future metric design; never affects verdicts.
    idiomatic_ct = sum(
        1 for r in results
        if r.verdict in ("ORACLE_DIVERGENT", "NEAR_MATCH", "WORKING")
        and r.matches_oracle < 0.80
        and getattr(r, "skeleton_similarity", 0.0) >= 0.85)
    print(f"SKELETON-INTENT CANDIDATES: {idiomatic_ct}  (sim < 0.80 but "
          f"skeleton >= 0.85 — idiomatic rewrites; eval-only diagnostic)")
    print(f"wall:       {elapsed:.0f}s ({elapsed/60:.1f}m) [this run only]")
    # Real-conflict pass rate: excludes SAFE_SKIP (no real conflict) from the
    # denominator. This is the honest metric — a SAFE_SKIP isn't a resolution
    # the system produced, it's a case git already resolved cleanly.
    real_conflicts = [r for r in results if r.terminal_reason != "SAFE_SKIP"]
    real_pass = sum(1 for r in real_conflicts if r.verdict == "PASS")
    real_work = sum(1 for r in real_conflicts if r.verdict == "WORKING")
    safe_skip_ct = sum(1 for r in results if r.terminal_reason == "SAFE_SKIP")
    # Explicit denominator breakdown so pass-rate comparisons are meaningful
    # across runs (Sprint 8: 64/76 vs Sprint 9: 52/75 — the denominator
    # changed by 1 with no explanation).
    print(f"total: {len(results)} | SAFE_SKIP: {safe_skip_ct} | real conflicts: {len(real_conflicts)}")
    if real_conflicts:
        print(f"real-conflict PASS rate: {real_pass}/{len(real_conflicts)} = "
              f"{real_pass/len(real_conflicts)*100:.0f}%")
        print(f"real-conflict PASS+WORKING rate: {real_pass+real_work}/{len(real_conflicts)} = "
              f"{(real_pass+real_work)/len(real_conflicts)*100:.0f}%")
    for lang in ("python", "rust", "c", "cpp"):
        sub = [r for r in results if r.language == lang]
        if not sub: continue
        p = sum(1 for r in sub if r.verdict == "PASS")
        wk = sum(1 for r in sub if r.verdict == "WORKING")
        n = sum(1 for r in sub if r.verdict == "NEAR_MATCH")
        e = sum(1 for r in sub if r.escalated)
        g = sum(1 for r in sub if r.verdict == "GATE_UNAVAILABLE")
        w = len(sub) - p - wk - n - e - g
        print(f"  {lang}: {len(sub)} → PASS {p} / WORK {wk} / NEAR {n} / ESC {e} / GATE_UNAVAIL {g} / DIVERGE {w}")
    from collections import Counter
    dt = Counter(r.dataset for r in results)
    dp = Counter(r.dataset for r in results if r.verdict == "PASS")
    dw = Counter(r.dataset for r in results if r.verdict == "WORKING")
    dn = Counter(r.dataset for r in results if r.verdict == "NEAR_MATCH")
    de = Counter(r.dataset for r in results if r.escalated)
    dg = Counter(r.dataset for r in results if r.verdict == "GATE_UNAVAILABLE")
    print("  by dataset:")
    for ds in sorted(dt):
        t = dt[ds]
        w = t - dp[ds] - dw[ds] - dn[ds] - de[ds] - dg[ds]
        print(f"    {ds:24s} {t:3d} → PASS {dp[ds]:3d} / WORK {dw[ds]:3d} / NEAR {dn[ds]:3d} / ESC {de[ds]:3d} / GATE_UNAVAIL {dg[ds]:3d} / DIVERGE {w:3d}")
    # Terminal reason distribution for escalations
    from collections import Counter as _C
    tr = _C(r.terminal_reason for r in results if r.escalated)
    if tr:
        print(f"\n  escalation terminal reasons:")
        for reason, count in tr.most_common():
            print(f"    {reason:25s} {count}")

    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([r.__dict__ for r in results], indent=2))
    if dropped:
        print(f"\n!! SUBSET RUN — size guard dropped {len(dropped)} corpus cases;"
              " these results are NOT a full shard")
    print(f"\nfull results: {out}")


if __name__ == "__main__":
    main()
