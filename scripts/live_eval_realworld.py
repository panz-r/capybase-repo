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
  GATE_UNAVAILABLE — content the build/validation gate rejected where the
               ORACLE itself fails the same gate (oracle_builds=False,
               probed post-hoc while the materialized tree still exists):
               sim >= 0.95 for any verdict, >= 0.80 for escalations
               (S28-144 — the escalation cannot implicate the merge). The
               gate cannot distinguish the resolver's
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
from corpus._realworld_build import (  # noqa: E402
    C_BUILD_COMMANDS,
    C_PREPARE_COMMANDS,
    C_TEST_COMMANDS,
    resolve_c_build,
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
# default in C_PREPARE_COMMANDS is the PREFERRED prepare for the majority commit;
# the era-aware resolver (corpus._realworld_build.resolve_c_build) probes the
# extracted tree and adapts (cmake → autotools fallback) per case, injecting
# per-dataset era CFLAGS + include flags (C_DATASET_CFLAGS /
# dataset_include_flags — also in corpus._realworld_build). The era headers
# under /tmp/capybase-era-includes are extracted EAGERLY here by
# _ensure_dataset_includes (a network fetch that cannot live in the corpus
# suite); the corpus side reads the prefix passively.


#: Sprint-26 A5 (era recovery): the validated rust dep patches. Old tokio
#: trees pin security-framework "^0.2" — ALL crates.io 0.2.x versions are
#: yanked; the git-tag pin restores resolution (verified: vendor 169 crates,
#: offline build rc=0 with --cap-lints warn for the 2019 rustdoc-attribute
#: drift). Per-dataset because the pins differ by era.
RUST_DEP_PATCHES: dict[str, list[str]] = {
    "tokio-history": [
        '[patch.crates-io]\n'
        'security-framework = { git = '
        '"https://github.com/kornelski/rust-security-framework", tag = "v0.2.2" }\n',
    ],
    # sea-orm sub-item: the git tag's workspace carries sea-query-derive
    # while crates.io also supplies it via other deps — unify on ONE
    # source or `cargo vendor` dies on the duplicate (validated E2E).
    # The sea-query entry (A6) redirects the `^0.17.1` trees to the tag
    # that carries FromValueTuple; cargo IGNORES the patch wherever the
    # tag's semver doesn't satisfy a tree's own requirement, so older
    # (^0.11-0.16) and newer (^0.21+) eras are untouched by it.
    "sea-orm-history": [
        '[patch.crates-io]\n'
        'sea-query = { git = '
        '"https://github.com/SeaQL/sea-query.git", tag = "0.18.2" }\n'
        'sea-query-derive = { git = '
        '"https://github.com/SeaQL/sea-query.git", tag = "0.18.2" }\n',
    ],
}

#: Sprint-26 A5 (sea-orm sub-item): dep-LINE rewrites — the era trees carry
#: `sea-query = { version = "^0.18.0", git = ".../sea-query.git", ... }`;
#: the git source's default branch now serves 1.0.2 and resolution dies.
#: Pinning the git dep to the era's LAST 0.18.x tag restores resolution —
#: 0.18.2 specifically: 0.18.0 lacks Expr::as_enum which the trees call
#: (E0599 ×3 on 0007). cargo vendor then includes the pinned tree. Textual
#: replace on Cargo.toml: a tree without the fragment (later-era cases)
#: is a no-op. VERIFIED E2E on sea-orm-0007's merge_sha tree: 349 crates
#: vendored, offline build rc=0 (2.4s incremental, --cap-lints warn).
RUST_DEP_REWRITES: dict[str, list[tuple[str, str]]] = {
    "sea-orm-history": [
        (
            'sea-query.git", features',
            'sea-query.git", tag = "0.18.2", features',
        ),
        # A6: the `^0.17.1` trees (0015-0019, merges of 2021-10-12/13)
        # import sea_query::FromValueTuple, which NO 0.17.x ever shipped
        # (trait added 2021-10-12, first in 0.18.0). The version bump is
        # REQUIRED alongside the patch: cargo silently ignores
        # version-incompatible [patch.crates-io] entries ("was not used
        # in the crate graph"), so the patch alone redirects nothing.
        # Validated offline: with rewrite+patch, the b582d3aac and
        # 7bc647709 trees cargo check rc=0 (0017-0019 recover); the
        # 5339696da tree stays broken in active_model.rs (0015/0016 are
        # intrinsic — dependency-independent errors).
        (
            'sea-query = { version = "^0.17.1", features',
            'sea-query = { version = "0.18.2", features',
        ),
        # A7: `sqlite-bind-decimals` was deleted upstream (post-s26;
        # sea-orm-0002's s26 PASS no longer reproduces). The branch is
        # PR #480, merged to master as 890e22c (2022-10-17, the same
        # day as 0003's merge) — pin the rev. Validated offline: 0003's
        # tree (duplicate-key fixed) checks rc=0 with this rewrite.
        (
            'branch = "sqlite-bind-decimals"',
            'rev = "890e22c39b86a5f1ee65fb1e454270b813da505e"',
        ),
        # A8: 0029's tree expects a SIBLING sea-query checkout
        # (path dep) the isolated worktree never materializes. Dropping
        # the path lets crates.io resolve; ^0.11 lacks IntoCondition
        # (the unshipped-API class again) — 0.12.0 has it. Validated
        # offline: rc=0.
        (
            'sea-query = { path = "../sea-query", version = "^0.11" }',
            'sea-query = { version = "0.12.0" }',
        ),
    ],
}


def _merge_patch_entries(toml_text: str, patch_entries: list[str]) -> str:
    """Add patch entries to the manifest keeping ONE [patch.crates-io].

    Appending a second `[patch.crates-io]` table to a tree that already
    has one is a duplicate-key cargo error for every probe and rerun
    alike — sea-orm-0003's recorded `error: duplicate key` era-dead was
    exactly this. Entries whose package name the section already pins
    are SKIPPED (a second `sea-query = ...` key is the same error one
    level down; and the tree's own pin is there deliberately).
    """
    header = "[patch.crates-io]"

    def _key(entry: str) -> str:
        return entry.split("=", 1)[0].strip()

    lines = toml_text.splitlines(keepends=True)
    for i, ln in enumerate(lines):
        if ln.strip() == header:
            j = i + 1
            while j < len(lines) and not lines[j].lstrip().startswith("["):
                j += 1
            existing = {_key(l) for l in lines[i + 1:j] if "=" in l}
            fresh = [e for e in patch_entries if _key(e) not in existing]
            addition = "".join(e + "\n" for e in fresh)
            return "".join(lines[:j]) + addition + "".join(lines[j:])
    return (toml_text.rstrip("\n") + "\n\n" + header + "\n"
            + "".join(e + "\n" for e in patch_entries))


def _vendor_rust_deps(repo: Path, dataset: str) -> bool:
    """Patch yanked/broken deps, cargo vendor, and wire the offline config.

    Returns True when a vendor/ dir was created (the gate then builds
    offline via the source replacement). Best-effort: any failure leaves
    the tree as-is (the case fails the gate and classifies era honestly).
    """
    patches = RUST_DEP_PATCHES.get(dataset)
    rewrites = RUST_DEP_REWRITES.get(dataset)
    if not patches and not rewrites:
        return False
    ct = repo / "Cargo.toml"
    if not ct.exists() or (repo / "vendor").is_dir():
        return False
    import subprocess as _sp

    def _revert(_orig_toml, _lock, _orig_lock) -> None:
        # REVERT every vendoring side effect: a poisoned Cargo.toml
        # (patch section or tag pin the tree's own deps can't resolve
        # with) breaks cargo for ALL THREE era probes identically — a
        # false toolchain-dead that stole 13 s24-PASS tokio cases
        # (0001-0013: their era's lockfile can't vendor with the
        # security-framework pin; the leftover patch then failed every
        # probe's cargo check). Restore the manifest + lockfile and drop
        # any partial vendor dir; the case then runs on its materialized
        # state.
        ct.write_bytes(_orig_toml)
        if _orig_lock is not None:
            _lock.write_bytes(_orig_lock)
        elif _lock.exists():
            _lock.unlink()
        import shutil as _shutil
        _shutil.rmtree(repo / "vendor", ignore_errors=True)

    try:
        _orig_toml = ct.read_bytes()
        _lock = repo / "Cargo.lock"
        _orig_lock = _lock.read_bytes() if _lock.exists() else None
        if rewrites:
            _text = ct.read_text(encoding="utf-8")
            for _old, _new in rewrites:
                _text = _text.replace(_old, _new)
            ct.write_text(_text, encoding="utf-8")
        if patches:
            # each patch block is '[patch.crates-io]\n<entry>...' — keep
            # just the entries; the section header is (re)created once.
            entries = [ln.strip() for blk in patches
                       for ln in blk.splitlines()
                       if ln.strip() and ln.strip() != "[patch.crates-io]"]
            ct.write_text(
                _merge_patch_entries(
                    ct.read_text(encoding="utf-8"), entries),
                encoding="utf-8")
        v = _sp.run(
            ["cargo", "vendor", "vendor"],
            cwd=str(repo), capture_output=True, text=True, timeout=600)
        if v.returncode != 0 or not (repo / "vendor").is_dir():
            _revert(_orig_toml, _lock, _orig_lock)
            return False
        # The config cargo prints verbatim; write it ourselves (stable form).
        (repo / ".cargo").mkdir(exist_ok=True)
        (repo / ".cargo" / "config.toml").write_text(
            "[source.crates-io]\n"
            'replace-with = "vendored-sources"\n\n'
            "[source.vendored-sources]\n"
            'directory = "vendor"\n')
        return True
    except Exception:  # noqa: BLE001 — vendoring is best-effort
        # The exception path (vendor timeout, unexpected error) used to
        # return WITHOUT restoring — the rewritten/patched manifest then
        # failed every era probe identically (the duplicate-key class).
        # Restore here too; restore failures degrade to best-effort.
        try:
            _revert(_orig_toml, _lock, _orig_lock)
        except Exception:  # noqa: BLE001
            pass
        return False


def _ensure_dataset_includes() -> None:
    """Extract era headers (tcl8.6-dev) into the local prefix if absent.

    Runs once per process (called eagerly at startup so the corpus-side
    passive ``dataset_include_flags`` finds the prefix for every case).
    Best-effort: on any failure the prefix stays empty and the prepare
    simply omits the -I flags (the tcl cases then fail the gate and
    classify era honestly, as before).
    """
    from corpus._realworld_build import DATASET_INCLUDE_PREFIX
    if DATASET_INCLUDE_PREFIX.exists():
        return
    marker = DATASET_INCLUDE_PREFIX.parent / ".era-includes.ok"
    if marker.exists():
        return
    try:
        import subprocess as _sp
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            _sp.run(
                ["apt-get", "download", "tcl8.6-dev"],
                cwd=td, check=True, capture_output=True, timeout=120)
            deb = next(Path(td).glob("tcl8.6-dev*.deb"))
            DATASET_INCLUDE_PREFIX.mkdir(parents=True, exist_ok=True)
            # Extract the header tree AND the dev lib tree (tclConfig.sh +
            # libtcl8.6.so live under usr/lib — the OUTPUT-TEST build of
            # sqlite's testfixture needs both; D9-s27).
            _sp.run(
                ["dpkg-deb", "-x", str(deb), str(DATASET_INCLUDE_PREFIX / "tcl-lib")],
                check=True, capture_output=True, timeout=120)
            src = DATASET_INCLUDE_PREFIX / "tcl-lib" / "usr" / "include" / "tcl8.6"
            if src.is_dir():
                _sp.run(
                    ["cp", "-r", str(src), str(DATASET_INCLUDE_PREFIX / "tcl8.6")],
                    check=True, capture_output=True, timeout=60)
        # D9 (s27): tclConfig.sh bakes the BUILD MACHINE's paths
        # (TCL_INCLUDE_SPEC=/usr/include/tcl8.6, which lacks tcl.h here).
        # Point both specs at the extracted trees so sqlite's testfixture
        # builds: VERIFIED end-to-end (testfixture rc=0, quicktest rc=0 on
        # an oracle-resolved tree).
        for _cfg in (DATASET_INCLUDE_PREFIX / "tcl-lib").rglob("tclConfig.sh"):
            try:
                _cfg_text = _cfg.read_text()
                _cfg_text = _cfg_text.replace(
                    "TCL_INCLUDE_SPEC='-I/usr/include/tcl8.6'",
                    f"TCL_INCLUDE_SPEC='-I{DATASET_INCLUDE_PREFIX / 'tcl8.6'}'")
                _cfg_text = _cfg_text.replace(
                    "TCL_LIB_SPEC='-L/usr/lib/x86_64-linux-gnu -ltcl8.6'",
                    "TCL_LIB_SPEC='-L"
                    + str(_cfg.parent) + " -ltcl8.6'")
                _cfg.write_text(_cfg_text)
            except Exception:  # noqa: BLE001 — config patch is best-effort
                pass
        marker.touch()
    except Exception:  # noqa: BLE001 — best-effort; cases degrade to era
        pass

# The extracted tclConfig.sh path (sqlite's output-test build needs it).
def _tcl_config_sh() -> str:
    from corpus._realworld_build import DATASET_INCLUDE_PREFIX
    hits = list((DATASET_INCLUDE_PREFIX / "tcl-lib").rglob("tclConfig.sh")) \
        if (DATASET_INCLUDE_PREFIX / "tcl-lib").is_dir() else []
    return str(hits[0]) if hits else ""

# Per-case build-command cache: populated by _materialize_conflict after it
# probes the extracted tree's build system. _config_for reads from here so the
# in-loop build gate matches whatever prepare actually ran. Keyed by case.id.
_DETECTED_BUILD_CMD: dict[str, str] = {}
# Sprint-20 S20.2: toolchain-era preflight cache (case_id -> probe dict,
# None when no usable gate). Populated on the first run of a case; the
# majority repeats reuse it (pristine sides and the oracle are identical
# across repeats — re-probing would only burn build time).
_TOOLCHAIN_PROBE_CACHE: dict[str, dict | None] = {}

# S28-191(1): the oracle probe is iteration-invariant per case too
# (expected_resolved never changes) — pilot4's duckdb repeats burned
# 3x300s on three IDENTICAL failing probes. Same pattern as the
# toolchain cache: first probe memoized, repeats read the memo.
_ORACLE_PROBE_CACHE: dict[str, bool | None] = {}

# S28-191(3)/S28-192(c): a C tree build that TIMED OUT marks the tree's
# gate undecidable for this cold environment — later builds for the
# same case (the oracle probe's inner build) skip the second 300s burn
# (the content cannot be discriminated by a build that never finishes).
_C_BUILD_TIMED_OUT: set[str] = set()

# S28-225 (the S28-217 follow-up): the harness's build verdicts carry no
# OUTPUT on the row — the fmt-0003 slice contradiction (one slice's
# runner build PASS on the tree, the next slice's 0.6s FAIL) cannot be
# attributed without the failing TU / cmake state. The last failing
# build's output head lands here per case and rides the row.
_LAST_C_BUILD_DIAG: dict[str, str] = {}

# S28-239.2/240.1 (queue item 2): KEEP-THE-BEST promotion (pilot-gated,
# default OFF; CAPYBASE_KEEP_BEST=1). At the scoring tail, a kept row
# whose best repeat beats it (by verdict rank, or sim within the rank)
# copies that repeat's outcome fields over its own — the repeats are
# already paid, so the promotion is zero-request, and the harvest's
# per-case rows stop understating the system (trial15: 4/15 rows kept a
# worse verdict than an existing repeat). The demoted record rides the
# row's repeat_flips field.
_KEEP_BEST_REPEATS = os.environ.get("CAPYBASE_KEEP_BEST", "") == "1"

# S28-244 (queue item 10): the UNVERIFIED relabel (pilot-gated, default
# OFF; CAPYBASE_UNVERIFIED_RELABEL=1) — escalated + oracle-undecidable
# (None) + sim >= 0.80 reads UNVERIFIED, completing S28-192.
_UNVERIFIED_RELABEL = os.environ.get(
    "CAPYBASE_UNVERIFIED_RELABEL", "") == "1"

# S28-253: build-what-you-ship's fresh-gate read (same pilot flag as the
# session-side probe) — escalated + the HARNESS build passes + marker-
# free + sim >= PASS reads PASS.
_SHIP_GATE_READ = os.environ.get("CAPYBASE_SHIP_GATE_PROBE", "") == "1"

# S28-243.2 (queue item 6): the era-header pre-screen (pilot-gated,
# default OFF; CAPYBASE_ERA_PRESCREEN=1) — setup-time GU for cases whose
# oracle references tree-absent APIs (the duckdb near-oracle class).
_ERA_PRESCREEN = os.environ.get("CAPYBASE_ERA_PRESCREEN", "") == "1"


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


#: Provenance prefixes marking an LLM-involved accepted candidate:
#: authored by the model (``plain_llm``/``history_augmented_llm`` —
#: hybrid suffixes like ``plain_llm+import_union`` count: deterministic
#: closure finished an LLM candidate), DECIDED by the model
#: (``block_capture``: keep/delete is an LLM call even though the splice
#: is mechanical), or model-repaired from build feedback
#: (``micro_patch_repair`` — CEGIS-shaped by construction).
_LLM_PROV_PREFIXES = (
    "plain_llm", "history_augmented_llm", "block_capture",
    "micro_patch_repair",
)


def classify_resolution_bucket(outcomes) -> tuple[str, dict]:
    """Who produced the accepted candidates (the results histogram).

    Bucket rule (case level, total order — every accepted case lands in
    exactly one bucket):

    - ``deterministic`` — accepted units exist and NONE is LLM-authored
      (zero model calls for the case).
    - ``llm_one_shot``  — some accepted unit is LLM-authored and no unit
      needed more than one LLM attempt.
    - ``llm_cegis``     — some unit accepted an LLM candidate only after
      >=2 LLM attempts (the model saw a validated failure and re-solved
      — the CEGIS loop). Best-of-N sampling within one round appends a
      single attempt, so it stays one-shot.

    Unresolved/escalated units are ignored (escalations are the verdict
    columns' job). Returns ``(bucket, provenance_mix)``; the mix counts
    per-unit accepted-provenance strings (hybrids included) for the
    histogram's finer rows. ``""`` when nothing was accepted.
    """
    mix: dict = {}
    llm_units = 0
    cegis_units = 0
    for o in outcomes or []:
        if getattr(o, "superseded", False):
            continue  # s27-63: a whole-file swap replaced this resolution
        acc = getattr(o, "accepted", None)
        if acc is None:
            continue
        prov = (getattr(acc, "provenance", "") or "")
        mix[prov] = mix.get(prov, 0) + 1
        if not prov.startswith(_LLM_PROV_PREFIXES):
            continue
        llm_units += 1
        llm_attempts = sum(
            1 for c in (getattr(o, "attempts", None) or [])
            if (getattr(c, "provenance", "") or "").startswith(
                _LLM_PROV_PREFIXES))
        if llm_attempts > 1:
            cegis_units += 1
    if llm_units == 0:
        return ("deterministic" if mix else ""), mix
    return ("llm_cegis" if cegis_units else "llm_one_shot"), mix


class _CallCountingClient:
    """Wraps the LLM client and counts every model call — the README llm
    column's whole-process measure (CaseResult.model_involved). The three
    surfaces cover every call site: candidate generation and decision
    prompts (complete/raw_complete), batch draws (complete_many).
    Everything else delegates to the wrapped client."""

    def __init__(self, inner):
        self._inner = inner
        self.calls = 0

    def complete(self, *args, **kwargs):
        self.calls += 1
        return self._inner.complete(*args, **kwargs)

    def complete_many(self, *args, **kwargs):
        self.calls += 1
        return self._inner.complete_many(*args, **kwargs)

    def raw_complete(self, *args, **kwargs):
        self.calls += 1
        return self._inner.raw_complete(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@dataclass
class CaseResult:
    id: str
    language: str
    dataset: str
    escalated: bool = False
    # S28-161: None = the check never ran (exception paths, harness
    # crashes). The verdict chain MUST distinguish that from a failed
    # check — 17 s28 rows at sim >= 0.95 (8 at 1.000) were labeled
    # ORACLE_DIVERGENT because unchecked content read as failed checks.
    marker_free: bool | None = None
    compiles: bool | None = None
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
    # S28-167: order-sensitive secondary metrics vs the oracle (EVAL ONLY
    # — never a gate). The token-Jaccard sim is order-blind — identical
    # token multisets score 1.0 in ANY line order — so a scrambled merge
    # of the oracle's lines would PASS identically to a real one. 14 s28
    # PASS rows at sim >= 0.99 carry oracle line-presence 0.017-0.889
    # (legitimate regenerations, but the PASS class is unauditable for
    # order defects without these). oracle_line_presence: multiset share
    # of the oracle's lines the output contains; oracle_order_score:
    # difflib matching-block coverage of the longer line sequence (an
    # order-sensitive LCS-grade ratio; difflib under-approximates the
    # true LCS, which only deepens a reorder signal). None = not
    # measurable (empty text, or past the monster-file guard — S28-164).
    oracle_line_presence: float | None = None
    oracle_order_score: float | None = None
    # S28-146: the runner's TEXTUAL checks (brace balance / markers /
    # python compile) are inapplicable when the ORACLE fails the same
    # check on the same file — the verdict then follows sim instead of
    # reading oracle-class content as ORACLE_DIVERGENT (17 s28 rows at
    # sim >= 0.95, 8 at 1.000, were the victims).
    oracle_check_inapplicable: bool = False
    # S28-144(2): oracle-equivalence (eval-only flag): marker-free
    # content at sim >= 0.99 with oracle_builds False — the oracle cannot
    # build in this environment, so identity with it is PASS-equivalent
    # even without a working gate. The verdict vocabulary stays
    # GATE_UNAVAILABLE; the recount counts the flag in the dual view.
    oracle_equivalent: bool = False
    # S28-170: cross-session stability (the repeat protocol's evidence).
    # stability: "stable" | "unstable" across a repeated case's runs
    # ("single-run" when no repeats ran); best_repeat_verdict/sim: the
    # BEST outcome any repeat achieved — rows whose best beats the kept
    # verdict form the repeat-flip queue, the corpus's cheapest re-score
    # population (the passing candidates already sit in the flights).
    stability: str = ""
    best_repeat_verdict: str = ""
    best_repeat_sim: float | None = None
    # S28-239.2/240.1 (queue item 2): keep-the-best's demoted record —
    # when the promotion promoted an already-paid best repeat over the
    # kept row, the kept row's (verdict, sim, reason, session_id) rides
    # here so the demotion stays auditable in the results JSON.
    repeat_flips: list = None
    # S28-170(1): the engine's acceptance_trust proposed FOR REVIEW on
    # "compile evidence missing" AND a gate build timed out — the
    # escalation is an environment artifact (the tree could not be
    # judged), the S28-161 UNVERIFIED class at the runner level.
    compile_evidence_missing: bool = False
    # S28-173(3): the engine's post-splice check found accepted units
    # preserving under the bar of the loser side's changes (eval-only;
    # sub-bands the NEAR_MATCH class — 0.875-with-0.31-loser-pres is a
    # different story from 0.875-with-0.9).
    splice_loser_dropped: bool = False
    # S28-176(a): terminal gcc diagnostics, attributed (eval-only). The
    # batch-19 finding: 11 of the 19 near-oracle REPAIR_FAILUREs died on
    # file-level errors in the include/type-visibility HEAD region the
    # conflict units occupy — per-unit repair cannot fix what the unit
    # itself damaged. terminal_error_line is the last gcc diagnostic's
    # line; failure_head_region flags the head shape.
    terminal_error_line: int | None = None
    failure_head_region: bool = False
    # S28-171(1): the harness's OWN builds, site-tagged — the session
    # journal cannot see them. Entries {site, outcome, duration_s};
    # sites: toolchain_probe, runner_c_build, oracle_probe.
    harness_builds: list = None
    # S28-194: the engine's own acceptance trust on the row — the
    # libuv-0089 finding: a PASS can be a tier-B PROPOSE_FOR_REVIEW
    # over a FAILED gate, and the harvest could not see the
    # distribution. Last acceptance_trust event wins.
    acceptance_tier: str | None = None
    acceptance_decision: str | None = None
    # S28-232 (the S28-228 ship-gate census): escalated C/CPP rows where
    # the session NEVER recorded a passing build probe after the last
    # candidate_accepted — the acceptance shipped content whose
    # buildability nothing in-session proved. Pure journal computation.
    ship_gate_unproven: bool = False
    # FR2a flight recorder: the orchestrator's session_id (the per-case artifact
    # root under .rebase-agent/sessions/<session_id>/). Populated when
    # --preserve-flights copies the session dir out; None otherwise. The flight
    # manifest maps case_id → session_id → artifacts for replay.
    session_id: str = ""
    # Mechanism reporting (the results histogram): the case-level bucket
    # for WHO produced the accepted candidates (deterministic |
    # llm_one_shot | llm_cegis; "" when nothing was accepted), plus the
    # raw per-unit accepted-provenance counter for the histogram's finer
    # rows (hybrids like plain_llm+import_union included).
    resolution_bucket: str = ""
    # The README llm column's source of truth (2026-09-18): True when ANY
    # model call happened during THIS run's whole resolution process —
    # candidates, repairs, adjudication ballots, comment reconciliation.
    # The complement (False) is provably solvable without model access.
    # Distinct from resolution_bucket (landed-LLM-text), which undercounts
    # 147 vs 958 on the s28 corpus. None = the row predates the flag
    # (the recount tool then falls back to the flight journals).
    model_involved: bool | None = None
    provenance_mix: dict = field(default_factory=dict)
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
    # S28-110: cross-revision API drift (attribution-only). True when an
    # escalated merge references members absent from the CURRENT tree but
    # present in the REPLAYED tree — the conflict is under-scoped and no
    # in-case mechanism can fix it. None = not probed / no drift.
    api_drift: bool | None = None
    api_drift_evidence: str | None = None
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
    # S28-243.2 (queue item 6): the era-header pre-screen's door — the
    # conflict file's TU needs APIs absent from the tree AND the oracle's
    # own text uses them, so the pass criterion is unachievable in-place;
    # classified at setup, before any model budget.
    era_header_dead: bool = False
    # S28-267: the gate-divergence suspect (eval-only) — the session's
    # LAST in-session build_probe FAILED the content while the HARNESS's
    # own build PASSED it (fmt-0003/libuv-0089's S28-217 shapes). Sizes
    # the session-vs-harness divergence per class; feeds the future
    # suspicion scoring.
    gate_divergence_suspect: bool = False


def _engine_session_completed(flights_dir, case_id, live_root=None) -> bool:
    """S28-137: has this case's newest session ACCEPTED a candidate?

    Checks two locations (True on either): the flight copy under
    ``flights_dir`` (written after orch.run() returns — the post-run shape)
    and, when ``live_root`` is given, the LIVE session journal under the
    case's temp repo (``<repo>/.rebase-agent/sessions/<sid>/journal.jsonl``,
    incrementally appended during the run — the mid-run shape). The live
    journal is what makes the grace fire on the dominant measured shape:
    acceptance lands early (0062: 48s into the session) and the wall dies
    during the engine's own build-probe/test phases, minutes before
    session_completed. A session still mid-CEGIS has no acceptance and
    returns False (legacy timeout, unchanged). Best-effort: any missing or
    unreadable artifact is skipped.
    """
    journal_paths = []
    if flights_dir is not None:
        # Newest flight journal ONLY: --preserve-flights accumulates session
        # dirs across repeats, and an older session's acceptance must not
        # vouch for a current run that is still looping.
        flights = sorted(
            Path(flights_dir).glob(f"flights/{case_id}/*/journal.jsonl"),
            key=lambda p: p.stat().st_mtime)
        if flights:
            journal_paths.append(flights[-1])
    if live_root is not None:
        # Live journals are per-attempt (each attempt gets its own temp repo),
        # so scanning them all is safe; first acceptance wins.
        journal_paths.extend(
            sorted(Path(live_root).glob(
                "*/.rebase-agent/sessions/*/journal.jsonl"),
                key=lambda p: p.stat().st_mtime))
    for jp in journal_paths:
        try:
            events = [json.loads(line)
                      for line in jp.read_text().splitlines()
                      if line.strip()]
        except (OSError, json.JSONDecodeError):
            continue
        if any(e.get("event_type") == "candidate_accepted" for e in events):
            return True
    return False


def _classify_terminal_reason(
    reason: str, *, elapsed_s: float | None = None,
    budget_s: float | None = None,
) -> str:
    """Classify an escalation reason into a disjoint terminal category.

    S28-202: TIMEOUT_* classes require TIMEOUT EVIDENCE — a
    timeout/wall substring in the reason, or elapsed within ~10% of the
    case budget. The zenodo class carried 69-428s against a 1200s
    budget: ordinary escalations wearing a timeout label; without the
    evidence requirement the class lies about what stopped the run.
    Evidence-less TIMEOUT_* matches fall through to the generic
    escalation classes.

    Returns one of:
      SAFE_STOP           — safety guard caught a real danger (resurrection)
      SAFE_SKIP           — no real conflict (git resolved cleanly)
      OVERSIZED           — oversized guard fired (file too large for model)
      MODEL_EMPTY         — model returned empty (not oversized)
      MODEL_NEEDS_HUMAN   — model self-reported needs_human
      TIMEOUT_CONVERGENCE — CEGIS loop failed to converge (no-progress / wall-time)
      VALIDATION_EXHAUSTED — unit unresolved: candidates repeatedly failed
        validation (capability/repair, not budget)
      TIMEOUT_THROUGHPUT  — per-case timeout on a many-region file (>20 units)
      TIMEOUT_CAPABILITY  — per-case timeout on a small file (model can't solve it)
      TIMEOUT_AFTER_ACCEPT — per-case timeout AFTER the engine accepted and
        completed its session (scoring builds blew the wall; S28-137)
      REPAIR_FAILURE      — whole-file repair couldn't resolve a unit
      TOOLCHAIN_ERA       — preflight: sides+oracle all fail the gate identically
      SETUP_FAILED        — infrastructure/setup failure (not a resolver outcome)
      OTHER               — uncategorized
    """
    r = (reason or "").lower()
    # S28-202: the timeout-evidence gate for every TIMEOUT_* decision
    # below — explicit wall language, or the run actually approached
    # the budget. Computed once; None (no elapsed recorded) keeps the
    # reason-substring path as the only evidence source.
    _timeout_evident = (
        "timeout" in r or "timed out" in r or "wall" in r
        or (elapsed_s is not None and budget_s
            and elapsed_s >= 0.9 * float(budget_s)))
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
    # s27-extend-27: infrastructure/setup failures (git lock errors,
    # materializer exceptions) are NOT resolver outcomes — clickhouse-0003
    # sat in the ESCALATE column for weeks on a git-lock write failure.
    # Classified distinctly so summaries stop counting infra as capability.
    if r.startswith("setup failed"):
        return "SETUP_FAILED"
    # s27-72 (sixth pass): harness-level crashes ("orch raised: ...",
    # "harness error: ...") are infrastructure, not resolver capability —
    # they were falling to OTHER and polluting the real-conflict
    # denominator (the clickhouse-0003 SETUP_FAILED doctrine).
    if r.startswith("orch raised") or r.startswith("harness error"):
        return "SETUP_FAILED"
    if "too large" in r or "oversized" in r:
        return "OVERSIZED"
    # S28-137: the engine ACCEPTED and completed its session — the wall died
    # in post-resolution scoring (cold c/oracle builds), not in the CEGIS
    # loop. A completed resolution mislabeled as a capability timeout
    # overstated regressions (duckdb-0062/0106 vs s28).
    if "post-resolution scoring exceeded the wall" in r and _timeout_evident:
        return "TIMEOUT_AFTER_ACCEPT"
    if "case timeout" in r:
        return "TIMEOUT_CASE"
    if ("wall-time" in r or "wall_time" in r) and _timeout_evident:
        return "TIMEOUT_CONVERGENCE"
    if "no hard-failure progress" in r and _timeout_evident:
        return "TIMEOUT_CONVERGENCE"
    if "needs_human" in r:
        return "MODEL_NEEDS_HUMAN"
    if "empty resolution" in r or "empty res" in r:
        return "MODEL_EMPTY"
    if "whole-file" in r or "whole_file" in r:
        return "REPAIR_FAILURE"
    # S28-202: an evidence-less convergence/no-progress exit is the
    # CEGIS loop exhausting its ROUNDS on failing validation — the
    # generic capability/repair class, not a timeout that never
    # happened (the zenodo class: 69-428s against a 1200s budget).
    if ("convergence" in r or "no hard-failure progress" in r) \
            and not _timeout_evident:
        return "VALIDATION_EXHAUSTED"
    if "convergence" in r and _timeout_evident:
        return "TIMEOUT_CONVERGENCE"
    if "could not resolve" in r:
        if "error:" in r or "syntax" in r or "delimiter" in r:
            return "VALIDATION_EXHAUSTED"
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
        # Skip pathologically huge conflicts (>1M chars). S28-99 re-baseline,
        # measured on the s28 full corpus (guard bypassed): the 48K-1M range
        # runs at corpus-parity (48-128K: 90.6% PASS, 128-256K: 88.7%,
        # 256-512K: 86.2%, 512K-1M: 80.0%) — context trimming + the in-window
        # essential-token guard handle those. The collapse is >1M (25% PASS:
        # both harness MemoryErrors and the in-window OVERSIZED class), which
        # is this guard's proper territory. CAPYBASE_SKIP_SIZE_GUARD=1 still
        # lifts it entirely for exploratory runs.
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
                and len(c.marker_original) > 1024 * 1024):
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
    # s27-74: hermetic against global git config (diff3 marker styles,
    # rebase.backend, gpgsign) — same rationale as corpus/_gitshim.
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
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

    # Sprint-26 A5: rust era recovery — patch the yanked/broken deps and
    # vendor them so the cargo gate runs offline (the tokio registry class).
    if case.language == "rust" and crate_source is not None:
        _vendor_rust_deps(repo, case.dataset)

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
        prepare, build_cmd = resolve_c_build(repo, case.dataset, default_prepare)
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

        # Sprint-26 A5-fmt (era recovery): old fmt's core.h lacks
        # <cstdint> (uint64_t does not name a type under libstdc++ 15 —
        # the 'types_' errors were downstream). One include addition;
        # VERIFIED: fmt-0004's full cmake build (incl. tests) rc=0.
        _fmt_core = repo / "include" / "fmt" / "core.h"
        if _fmt_core.exists():
            _fc = _fmt_core.read_text()
            if "#include <cstdint>" not in _fc and "uint64_t" in _fc:
                _fc = _fc.replace("#include <cstdio>", "#include <cstdio>\n#include <cstdint>", 1)
                if "#include <cstdint>" in _fc:
                    _fmt_core.write_text(_fc)
        # Sprint-26 A1-redis (era recovery): bundled hiredis carries
        # va_arg(ap, void) — invalid C, gcc 15 hard-errors it. Replace with
        # the (void)ap discard. VERIFIED: with the full redis stack the
        # tree builds redis-server/cli/benchmark.
        _hiredis = repo / "deps" / "hiredis" / "hiredis.c"
        if _hiredis.exists():
            _hc = _hiredis.read_text()
            if "va_arg(ap,void)" in _hc or "va_arg(ap, void)" in _hc:
                _hiredis.write_text(
                    _hc.replace("va_arg(ap,void);", "(void)ap;")
                       .replace("va_arg(ap, void);", "(void)ap;"))
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
                # Sprint-26 A1: keep the era patches applied — reverting
                # tool/lemon.c resurrects the K&R declarations the patcher
                # fixed (the harvest's 90 sqlite failures: the probe's make
                # rebuilt lemon from the REVERTED source).
                and ln.strip() != "tool/lemon.c"
                and ln.strip() != "deps/hiredis/hiredis.c"
                and ln.strip() != "include/fmt/core.h"
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


#: Build-target narrowing: for sqlite and redis, compile only the conflict
#: file's translation unit instead of the full project. sqlite's Makefile
#: has per-object rules (delete.lo:, update.lo:, etc.) and redis has a %.o
#: pattern rule. This cuts in-session build verification from ~54s (full
#: make) to ~2-5s (single object). Falls back to the full build when no
#: target rule exists. json-c uses cmake (awkward per-object targets).
#: Hoisted to module scope for _oracle_builds (D10): the GATE_UNAVAILABLE
#: doctrine compares the oracle against the gate the resolver FACED —
#: the targeted build, not the full tree build.
_C_BUILD_TARGETS = {
    "sqlite-history": "make {stem}.lo",
    "redis-history": "make {stem}.o",
}


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
    cfg, _profile_knobs, _cal_report = apply_to_config(cfg, resolved)
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
    # S28-180 pilot gate: the side-consistent repair feedback is default
    # OFF; the screening rerun opts in via env (feedback-only — the
    # request-count pin must hold with it on).
    if os.environ.get("CAPYBASE_SIDE_FEEDBACK", "") == "1":
        cfg.future.enable_side_consistent_feedback = True
    # S28-189/S28-204 pilot gate: the seam-aware repair feedback is
    # default OFF; the screening rerun opts in via env.
    if os.environ.get("CAPYBASE_SEAM_FEEDBACK", "") == "1":
        cfg.future.enable_seam_aware_feedback = True
    # S28-247.2 pilot gate: the anti-reroll line is default OFF; the
    # next armed rerun opts in via env.
    if os.environ.get("CAPYBASE_ANTI_REROLL", "") == "1":
        cfg.future.enable_anti_reroll_feedback = True
    # S28-239.1 pilot gate: build-what-you-ship (the pre-escalation
    # final gate probe) is default OFF; the next armed rerun opts in.
    if os.environ.get("CAPYBASE_SHIP_GATE_PROBE", "") == "1":
        cfg.future.enable_ship_gate_final_probe = True
    # S28-203 pilot gate: the sides-check alignment on the validator
    # doubt is default OFF; the screening rerun opts in via env.
    if os.environ.get("CAPYBASE_SIDES_ALIGNMENT", "") == "1":
        cfg.future.enable_sides_check_alignment = True
    # S28-233 pilot gate: the beam-substrate synthesis is default OFF.
    if os.environ.get("CAPYBASE_BEAM_SUBSTRATE", "") == "1":
        cfg.future.enable_beam_sides_substrate = True
    # S28-233/243 pilot gate (queue item 4): the terminal-path arms are
    # default OFF; the next armed rerun opts in via env.
    if os.environ.get("CAPYBASE_TERMINAL_ARMS", "") == "1":
        cfg.future.enable_terminal_path_arms = True
    # S28-268 pilot gate: the tree-absent-member deletion rung is
    # default OFF; the next armed rerun opts in via env.
    if os.environ.get("CAPYBASE_TREE_ABSENT_DELETION", "") == "1":
        cfg.future.enable_tree_absent_deletion = True
    # S28-183 pilot gate: the repair-edit delimiter guard is default OFF
    # (census-gated decline); the screening rerun opts in via env.
    if os.environ.get("CAPYBASE_REPAIR_GUARD", "") == "1":
        cfg.future.enable_repair_delimiter_guard = True
    # S28-197 pilot gate: the declaration-restoration mode (default OFF).
    if os.environ.get("CAPYBASE_DECL_RESTORE", "") == "1":
        cfg.future.enable_declaration_restoration = True
    # S28-206 pilot gate: the identical-block dedup rung (default OFF).
    if os.environ.get("CAPYBASE_BLOCK_DEDUP", "") == "1":
        cfg.future.enable_identical_block_dedup = True
    # B10 (sprint-26): the self-consistency A/B arm —
    # CAPYBASE_SELF_CONSISTENCY=N (N>1) enables consensus sampling with N
    # samples (samples_complex follows). The per-candidate consensus fields
    # (agreement/clusters/n_samples) are journaled on candidate_generated
    # when active, so the A/B's cost model reads straight from the flights.
    # Default off = the harvest baseline arm; no CLI surface change.
    _sc_n = os.environ.get("CAPYBASE_SELF_CONSISTENCY", "").strip()
    if _sc_n.isdigit() and int(_sc_n) > 1:
        cfg.model.enable_self_consistency = True
        cfg.model.samples = int(_sc_n)
        cfg.model.samples_complex = int(_sc_n)
    # Near-miss seeding A/B arm (S28-128): the treatment run flips the
    # default-off mechanism on without code edits; the journal's
    # near_miss_stashed/near_miss_used pair makes the arms exactly
    # attributable.
    if os.environ.get("CAPYBASE_NEAR_MISS_SEEDING", "").strip().lower() in (
            "1", "true", "yes"):
        cfg.future.enable_near_miss_seeding = True
    # Common-span factoring A/B arm (S28-136): the treatment run flips the
    # default-off mechanism on without code edits; llm_skipped_oversized
    # events disappearing (conversions) is the headline metric.
    if os.environ.get("CAPYBASE_COMMON_SPAN_FACTORING", "").strip().lower() in (
            "1", "true", "yes"):
        cfg.future.enable_common_span_factoring = True
    # Commit-intent context A/B arm (S28-138 Fix B): the treatment run flips
    # the default-off mechanism on without code edits; prompt-byte deltas and
    # sim movement on multi-hunk cases are the metrics.
    if os.environ.get("CAPYBASE_COMMIT_INTENT_CONTEXT", "").strip().lower() in (
            "1", "true", "yes"):
        cfg.future.enable_commit_intent_context = True
    # Ordered-splice A/B arm (S28-139): the treatment run flips the
    # default-off mechanism on without code edits; conversions of
    # ambiguity-band SBCR declines into validated splices are the headline.
    if os.environ.get("CAPYBASE_ORDERED_SPLICE", "").strip().lower() in (
            "1", "true", "yes"):
        cfg.future.enable_ordered_splice = True
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
    if case.language in ("c", "cpp", "c++"):
        _target = _C_BUILD_TARGETS.get(case.dataset, "")
        if _target:
            cfg.validation.cc_build_target_template = _target
    cfg.features.structural_resolution = True
    # Sprint-23 mechanisms: F1 is always-on (smart conditions in the
    # orchestrator); R3 best-of-N is config-gated (default False,
    # enabled here for the specimen/full runs)
    cfg.future.enable_best_of_n = True
    cfg.features.combination_search = True
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
    cfg.features.rag = False
    if os.environ.get("CAPYBASE_GOLDEN_PATH", "") == "1":
        cfg.features.rag = True
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
        cfg.features.llm_critic = False
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
        cfg.future.jury_mode = "shadow"
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


def _c_builds(repo: Path, case: Case, timeout_s: float = 300) -> bool | None:
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
    # via resolve_c_build), which matches whatever prepare actually ran. Falls
    # back to the static per-dataset default.
    cmd = _DETECTED_BUILD_CMD.get(case.id) or C_BUILD_COMMANDS.get(case.dataset, "")
    if not cmd or cmd == "true":
        return None
    # S28-191(3): this case's tree build already burned the 300s cap
    # once (a cold environment — content-independent). Repeats (and the
    # oracle probe's second cold build) buy nothing from another burn.
    if case.id in _C_BUILD_TIMED_OUT:
        return None
    try:
        proc = _run_shell_tree(cmd, cwd=str(repo), timeout=timeout_s)
        if proc.returncode == 0:
            return True
        stderr = (proc.stderr or "") + (proc.stdout or "")
        # S28-250.3 (queue item 9, S28-242.1's harvest note): noisy
        # builds bury the diagnostic — redis's `#warning` storm from the
        # system include chain filled the 400-char head before the
        # error appeared. Store the gcc `error:` lines FIRST (bounded),
        # falling back to the head for failures no compiler diagnostic
        # describes (non-compile tool failures).
        _error_lines = [
            ln for ln in stderr.splitlines()
            if " error:" in ln or ln.startswith("error:")]
        _diag = "\n".join(_error_lines[:6]) if _error_lines else stderr
        _LAST_C_BUILD_DIAG[case.id] = _diag[:400]
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
            # Sprint-27: promotion excuse aligned with the in-session gate
            # (D13's doctrine, the curated categories) — without this, a
            # promoted warning in the conflict file failed the EVAL's
            # build while the orchestrator's gate correctly accepted the
            # merge: eval-stricter-than-session would misgrade a passing
            # resolution as not-compiling.
            if _is_promotion_tag(ln):
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
    except subprocess.TimeoutExpired:
        # S28-192(c): a TIMEOUT is not "no gate". The None contract here
        # means "no build command registered" (the degraded route), and
        # conflating the two sent the oracle probe down the standalone
        # fallback on a gate that merely could not FINISH. Memoize the
        # case and re-raise so callers discriminate.
        _C_BUILD_TIMED_OUT.add(case.id)
        raise
    except Exception:  # noqa: BLE001 — best-effort; treat as "couldn't check"
        return None


_CC_ERROR_LINE_RE = re.compile(r"^\S+?:\d+:\d+:\s*(error:.*)$")

#: Era-probe honesty (redis-0038/0048): the tag gcc appends to promoted
#: warnings. Two renderings exist: `[-Werror=cat]` (explicit -Werror= flag)
#: and `[-Wcat]` (observed under plain -Werror in gcc 15 — redis-0048's
#: recorded sig carries `[-Wincompatible-pointer-types]`). A -W tag names
#: a WARNING option; gcc puts it only on warning diagnostics, so an
#: ``error:`` line ending in ANY -W tag is a promotion. The verdict build
#: excuses promotions and sibling-file errors as infrastructure; the era
#: probe must excuse the SAME classes before comparing signatures —
#: otherwise a case whose only failures are infra-class can PASS the
#: eval's localized build check yet never stop being era-dead (0038: the
#: va_arg sed homogenized the last heterogeneous signature member).
_CC_WERROR_TAG_RE = re.compile(r"\[-W(error[=+])?([^\]]+)\]\s*$")


def _is_promotion_tag(ln: str) -> bool:
    """Aligned with verification's curated rule (s27 day-12): explicit
    -Werror= forms are promotions by construction; plain -W tags only in
    the KNOWN warning categories. Structural tags (-Wtemplate-body — real
    syntax errors) must stay IN the era signature: excluding them here
    weakened era detection (a false era-negative lets an un-passable case
    burn its full budget)."""
    m = _CC_WERROR_TAG_RE.search(ln)
    if m is None:
        return False
    if m.group(1):
        return True
    from capybase.verification import _PROMOTION_W_CATEGORIES
    return m.group(2) in _PROMOTION_W_CATEGORIES

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


def _compile_error_signature(
    output: str, language: str, conflict_path: str | None = None,
) -> list[str]:
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
                # Era-probe honesty (redis-0038): excuse exactly what the
                # verdict build excuses — -Werror promotions and (when the
                # conflict path is known) sibling-file errors — before
                # signature comparison. Infra-class errors are identical
                # across sides BY CONSTRUCTION; letting them into the
                # signature makes era-dead the default for any era tree
                # built with strict flags, regardless of content.
                if _is_promotion_tag(ln):
                    continue
                if conflict_path:
                    _loc = re.match(r"^(\S+?):\d+:\d+:", ln)
                    if _loc and (Path(_loc.group(1)).stem
                                 != Path(conflict_path).stem):
                        continue
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
                    "sig": _compile_error_signature(out, case.language, conflict_path=case.path),
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
                        out, case.language,
                        conflict_path=case.path)[:5]
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
        # Template-file guard (sqlite-0039 lesson): a conflict file that
        # fails its target with "expected expression before '%'" is a
        # GENERATOR TEMPLATE (lemon's lempar.c) — never compiled directly
        # by the real build (the full gate proved rc 0). The target rule
        # exists but is not on the default build path; classifying it
        # toolchain-dead steals a passable case. Computed over ALL three
        # target signatures — the equivalence block below needs the same
        # guard: 0039's template errors are identical across sides, which
        # re-flagged it dead through THAT path in the s26 pool.
        _template_sig = any(
            "before '%' token" in s or 'before ‘%’' in s
            for _sigs in _target_sigs.values()
            for s in (_sigs or [])
        ) or any(
            "before '%' token" in s or 'before ‘%’' in s
            for s in (target_probe.get("sig") or [])
        )
        if not _template_sig:
            dead = True
    # Signature equivalence (sprint-25 item 2): when ALL THREE texts fail
    # the conflict-target build with IDENTICAL signatures, the errors are
    # era/content-intrinsic (redis-0049: the era code lives in the sides
    # and the oracle alike) — the resolver cannot distinguish its output
    # from the human resolution. Semantically honest: identical signatures
    # across all three is content evidence, not denominator trimming.
    # Template guard applies here too: a generator template's identical
    # '%'-token errors are not content evidence.
    _all3 = (_target_sigs.get("oracle") and _target_sigs.get("current")
             and _target_sigs.get("replayed"))
    if (not dead and _all3
            and not _template_sig
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


#: S28-254.3/S28-243.2 (queue item 6): the gcc error shapes that name a
#: symbol the conflict file's TU needs. Mirrors the S28-245 side-note set.
_ERA_SYMBOL_PATTERNS = (
    re.compile(r"no member named\s+'?([A-Za-z_]\w*)"),
    re.compile(r"no matching function for call to\s+'([\w:]+)"),
    re.compile(r"no declaration matches\s+'?([\w:]+)"),
    re.compile(r"'([A-Za-z_]\w*)' does not name a type"),
    re.compile(r"unknown type name\s+'([A-Za-z_]\w*)"),
    re.compile(r"use of undeclared identifier\s+'([A-Za-z_]\w*)"),
)


def _tree_defines_symbol(repo: Path, symbol: str) -> bool:
    """True when ``symbol`` appears anywhere in the materialized tree
    (fixed-string git grep — presence anywhere counts, so a miss is a
    real miss; conservative against the S28-243.2 macro/default
    false-positive risk)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "grep", "-I", "-l", "-F", symbol,
             "--", ".",],
            capture_output=True, text=True, timeout=60)
        return bool((out.stdout or "").strip())
    except Exception:  # noqa: BLE001 — the grep is best-effort
        return True  # inconclusive counts as present (never a false GU)


#: S28-268: the era memo — persisted per-case era classifications so
#: run N+1 classifies at setup without re-running the probe builds.
#: Keyed (case_id) with a spec content hash + the armed-flag
#: fingerprint for validity.
_ERA_MEMO_PATH = Path.home() / ".cache" / "capybase" / "era_memo.json"
_ERA_NO_MEMBER_RE = re.compile(r"no member named\s+[‘']?([A-Za-z_]\w*)")
_ERA_NO_DECL_RE = re.compile(
    r"no declaration matches\s+[‘']?[\w:<> ]*?([A-Za-z_]\w*)\s*\(")
_ERA_FLAGS_FINGERPRINT = "|".join(sorted(
    name for name, env in (("ERA_PRESCREEN", "CAPYBASE_ERA_PRESCREEN"),
                           ("SHIP_GATE_PROBE", "CAPYBASE_SHIP_GATE_PROBE"),
                           ("TERMINAL_ARMS", "CAPYBASE_TERMINAL_ARMS"),
                           ("TREE_ABSENT_DELETION",
                            "CAPYBASE_TREE_ABSENT_DELETION"),
                           ("ANTI_REROLL", "CAPYBASE_ANTI_REROLL"))
    if os.environ.get(env, "") == "1"))


def _era_spec_sha(case: "Case") -> str:
    import hashlib as _hl
    return _hl.sha1(
        ((case.current or "") + "\x00" + (case.replayed or "") + "\x00"
         + (case.expected_resolved or "")).encode("utf-8", "replace")
    ).hexdigest()[:16]


def _era_memo_load() -> dict:
    try:
        return json.loads(_ERA_MEMO_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a missing/corrupt memo is a miss
        return {}


def _era_memo_store(case_id: str, entry: dict) -> None:
    memo = _era_memo_load()
    memo[case_id] = entry
    try:
        _ERA_MEMO_PATH.parent.mkdir(parents=True, exist_ok=True)
        _ERA_MEMO_PATH.write_text(json.dumps(memo, indent=1),
                                  encoding="utf-8")
    except Exception:  # noqa: BLE001 — the memo is best-effort
        pass


def _era_side_symbols(probe: dict) -> list[str]:
    """The member symbols the probe's SIDE builds named (the era
    screen's extraction, hoisted for the memo's subset check)."""
    probes = probe.get("probes") or {}
    syms: list[str] = []
    for side in ("current", "replayed"):
        for ln in (probes.get(side) or {}).get("sig") or []:
            m = _ERA_NO_MEMBER_RE.search(ln or "") or _ERA_NO_DECL_RE.search(ln or "")
            if m:
                sym = m.group(1).split("::")[-1]
                if len(sym) >= 4 and sym not in syms:
                    syms.append(sym)
    return syms


def _era_header_screen(repo: Path, case: "Case",
                       probe: dict) -> dict | None:
    """S28-243.2 (queue item 6): the era-header pre-screen.

    The toolchain probe already built BOTH pristine sides and holds
    their failures. Extract the symbols those conflict-TU errors name;
    a symbol absent from the ENTIRE tree is era-lost — and if the
    ORACLE's own text uses it, the human resolution cannot compile in
    this tree either: the case's pass criterion is unachievable
    in-place (the S28-144 GU doctrine, evaluated at setup instead of
    after the model budget). Returns the screen dict or None when
    nothing is decidable."""
    if not probe or probe.get("toolchain_dead"):
        return None
    probes = probe.get("probes") or {}
    symbols: list[str] = []
    oracle_repeats: list[str] = []
    _no_member = re.compile(r"no member named\s+[‘']?([A-Za-z_]\w*)")
    _no_decl = re.compile(
        r"no declaration matches\s+[‘']?[\w:<> ]*?([A-Za-z_]\w*)\s*\(")
    oracle_text = case.expected_resolved or ""
    for side in ("current", "replayed"):
        for ln in (probes.get(side) or {}).get("sig") or []:
            ln = ln or ""
            m = _no_member.search(ln) or _no_decl.search(ln)
            if not m:
                continue
            sym = m.group(1).split("::")[-1]
            if len(sym) < 4:
                continue
            if sym not in symbols:
                symbols.append(sym)
            # S28-261: the v2 discriminator — the compiler proved this
            # use invalid on its type; when the ORACLE's own text
            # repeats the same use spelling, the oracle cannot compile
            # in this tree (the 0113 signature-level drift: the symbol
            # NAME exists elsewhere, so the v1 name-grep alone
            # declines). Census (trial15 cpp rows): fires on exactly
            # the six era rows, declines the three fixable classes.
            if (sym not in oracle_repeats
                    and re.search(rf"[.>\-]{{1,2}}{sym}\s*\(", oracle_text)):
                oracle_repeats.append(sym)
    missing = [s for s in symbols[:8]
               if not _tree_defines_symbol(repo, s)]
    oracle_uses = [s for s in missing
                   if s in oracle_text]
    return {"era_header_dead": bool(oracle_uses or oracle_repeats),
            "missing_symbols": missing,
            "oracle_uses_missing": oracle_uses,
            "oracle_repeats_invalid": oracle_repeats}


def _mark_era_header_dead(res: "CaseResult", screen: dict,
                          t0: float) -> "CaseResult":
    """S28-243.2's GU door: the case is era-mismatched at the conflict
    file BEFORE any model budget — an unpassable case is not a resolver
    outcome (the toolchain-probe doctrine)."""
    res.elapsed = time.time() - t0
    res.escalated = True
    res.era_header_dead = True
    _uses = screen.get("oracle_uses_missing") or []
    _repeats = screen.get("oracle_repeats_invalid") or []
    res.reason = (
        "era-header pre-screen: conflict-file APIs absent from the tree "
        f"({', '.join(_uses)}) — the oracle itself references them; "
        f"invalid uses the oracle repeats ({', '.join(_repeats)}); no "
        "in-file resolution can pass this gate (S28-243.2)"
    ) if _uses else (
        "era-header pre-screen: the oracle repeats API uses the compiler "
        f"proved invalid on the sides ({', '.join(_repeats)}) — the "
        "oracle cannot compile in this tree; no in-file resolution can "
        "pass this gate (S28-243.2)"
    )
    return res


def _api_drift_probe(
    clone: Path, merge_sha: str, path: str,
    expected_current: str, expected_replayed: str,
    escalated_reason: str,
) -> str | None:
    """S28-110: is the escalation cross-revision API drift?

    S28-95's validated 2-grep probe: for member references in the
    escalation reason (the build errors name the undeclared identifiers /
    missing members), count tree-wide occurrences in each merge parent.
    A member present in the REPLAYED tree and absent from the CURRENT
    tree is drift — the fragment called the API as the replayed side's
    headers define it, and the gate compiles against current's. Returns
    the evidence string ("member X: current 0 / replayed N hits") or
    None (no drift shape, or the greps are inconclusive).
    """
    import re as _re
    try:
        parents = subprocess.run(
            ["git", "-C", str(clone), "rev-parse",
             f"{merge_sha}^1", f"{merge_sha}^2"],
            capture_output=True, text=True, check=True,
        ).stdout.split()
    except Exception:
        return None
    if len(parents) != 2:
        return None
    # identify which parent carries the current side's version of the file
    try:
        cur_blob = subprocess.run(
            ["git", "-C", str(clone), "show", f"{parents[0]}:{path}"],
            capture_output=True, text=True, check=True).stdout
        cur_parent, rep_parent = parents[0], parents[1]
        if cur_blob != expected_current:
            cur_parent, rep_parent = parents[1], parents[0]
    except Exception:
        return None

    members: list[str] = []
    for m in _re.finditer(
            r"[‘\']([A-Za-z_]\w*)[’\'] was not declared", escalated_reason):
        if m.group(1) not in members:
            members.append(m.group(1))
    if not members:
        return None
    for member in members[:2]:
        counts = []
        for parent in (cur_parent, rep_parent):
            r = subprocess.run(
                ["git", "-C", str(clone), "grep", member, parent],
                capture_output=True, text=True)
            counts.append(len(r.stdout.splitlines()))
        if counts[0] == 0 and counts[1] > 0:
            return (f"member {member!r}: 0 occurrences in the current tree, "
                    f"{counts[1]} in the replayed tree — cross-revision API "
                    f"drift; the correct merge must carry the API-defining "
                    f"changes")
        if counts[1] == 0 and counts[0] > 0:
            return (f"member {member!r}: present only in the current tree "
                    f"({counts[0]} hits) — reverse drift")
    return None


def _oracle_include_roots(repo: Path, case: "Case") -> list[str]:
    """S28-192(b): the standalone probe's include paths — repo + the
    file's dir (the S28-105 baseline) plus the tree's REAL roots: the
    conventional include dirs that exist on disk, and any -I flags in
    the adaptively-detected build command."""
    roots = [str(repo), str((repo / case.path).parent)]
    for sub in ("src/include", "include", "src"):
        cand = repo / sub
        if cand.is_dir():
            roots.append(str(cand))
    for m in re.finditer(r"-I\s*(\S+)",
                         _DETECTED_BUILD_CMD.get(case.id, "")):
        roots.append(m.group(1))
    return roots


def _oracle_builds(repo: Path, case: Case, crate_source: Path | None,
                   runner_build_passed: bool | None = None) -> bool | None:
    """S28-191(1): per-case memo around the probe — repeats read the
    cache (the oracle text is iteration-invariant; the toolchain-probe
    precedent). ``runner_build_passed`` feeds S28-241.2's cap choice."""
    if case.id in _ORACLE_PROBE_CACHE:
        return _ORACLE_PROBE_CACHE[case.id]
    result = _oracle_builds_uncached(repo, case, crate_source,
                                     runner_build_passed=runner_build_passed)
    _ORACLE_PROBE_CACHE[case.id] = result
    return result


def _oracle_builds_uncached(repo: Path, case: Case, crate_source: Path | None,
                            runner_build_passed: bool | None = None) -> bool | None:
    """Does the ORACLE (expected_resolved) pass the same gate the merge faced?

    Writes expected_resolved into the materialized tree and runs the gate the
    resolver itself faced: the C/C++ tree build for c/cpp cases, the cargo
    new-error delta (vs the one-side-blanked baseline — the orchestrator's own
    baseline construction) for rust-with-crate cases. Returns None when no
    gate applies or the probe is undecidable. Used by the GATE_UNAVAILABLE
    classification: a sim >= 0.95 merge the gate rejected is a sandbox
    artifact, not a resolver failure, when the human resolution fails the
    same gate.

    S28-241.2 (queue item 8): the probe's build cap is 300s only when the
    RUNNER build passed (the tree demonstrably builds; the oracle probe
    gets the full envelope); otherwise 120s — an na outcome is
    undecidable at any cap, and the trial's 7 x 300s na-burns all sat on
    content-failed trees (1.5-5s runner failures), so 120s carries the
    same information at 40% of the wall.
    """
    target = repo / case.path
    saved = target.read_bytes() if target.exists() else None
    try:
        target.write_text(case.expected_resolved)
        if case.language in ("c", "cpp", "c++"):
            # S28-191(2)/S28-192(c): a runner build that TIMED OUT marks
            # this cold tree's gate undecidable — the same command on the
            # oracle text cannot finish either (pilot4: 300.0s ×3), and
            # a build that never completes cannot discriminate content.
            # Skip the second cold burn AND refuse the standalone route
            # (designed for ABSENT gates, not unfinished ones).
            if case.id in _C_BUILD_TIMED_OUT:
                return None
            _full = _c_builds(
                repo, case,
                timeout_s=(300 if runner_build_passed else 120))
            if _full is False:
                return False
            if _full is None:
                # S28-105 (the D10 extension): the tree gate is DEGRADED
                # ("true" — no prepare/build entry, S28-78's honest
                # degrade), so the resolver's whole-file verdicts came from
                # the STANDALONE SYNTAX FALLBACK. Probe the oracle through
                # the SAME check, mirroring the verifier's own invocation.
                # S28-192(b): the probe runs with the tree's REAL include
                # roots — with them, a failure is the environment's true
                # state (the php arginfo band's generated headers are
                # ABSENT, not misplaced), and False is the sound
                # discriminator S28-199 dispositioned. S28-192(a)'s
                # missing-include→None rule is deliberately NOT applied
                # here: the unsound population (timeout-routed probes) is
                # handled above by the S28-192(c) split, and applying (a)
                # to the degraded route would strip the sound php band of
                # its honest GU (the D10 pin caught exactly that).
                from capybase.verification import _compile_ccs
                _cpp = case.language in ("cpp", "c++")
                _ok, _ = _compile_ccs(
                    case.expected_resolved,
                    cc_path="g++" if _cpp else "gcc",
                    std="c++17" if _cpp else "c11",
                    suffix=".cpp" if _cpp else ".c",
                    include_paths=_oracle_include_roots(repo, case),
                )
                return bool(_ok)
            # D10 (s27): the resolver's IN-SESSION gate is the TARGETED
            # per-file build for sqlite/redis, not the full tree build.
            # The oracle passing the full gate while failing the targeted
            # one means the resolver faced a gate the human resolution
            # ALSO fails (sqlite-0108/0111: pristine sides pass full make
            # rc=0 yet fail make select.lo on era-stale generated headers
            # — every variant unverifiable in-session; a sim-1.0 merge
            # rejected by a sandbox artifact). That shape is
            # GATE_UNAVAILABLE, so probe the gate the doctrine names: the
            # SAME one.
            _target = _C_BUILD_TARGETS.get(case.dataset, "")
            if _target:
                from pathlib import PurePosixPath as _PP
                _stem = _PP(case.path).stem
                _tp = _run_shell_tree(
                    _target.format(stem=_stem),
                    cwd=str(repo), timeout=120)
                if _tp.returncode != 0:
                    return False
            return True
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
            if new:
                return False
            # D10-rust (s27): the diagnostic-delta probe says "no new
            # errors", but the resolver's ACTUAL gate is cargo check's
            # new-error delta. axum-0019: the tree's file at merge_sha
            # is byte-identical to the oracle; cargo check fails it
            # identically on all three texts — a sim-1.0 merge rejected
            # by a gate the oracle shares. Run the real gate on the
            # oracle text; errors in the conflict file mean False.
            from pathlib import Path as _P
            _tcl = repo / case.path
            _saved_tcl = _tcl.read_bytes() if _tcl.exists() else None
            try:
                _tcl.write_text(case.expected_resolved)
                _oc = _run_shell_tree("cargo check", cwd=str(repo), timeout=600)
                if _oc.returncode != 0:
                    _out = (_oc.stderr or "") + (_oc.stdout or "")
                    import re as _re_d10r
                    _stem = _P(case.path).stem
                    _hit = any(
                        _re_d10r.search(
                            rf"\b{_re_d10r.escape(_stem)}\.rs\b", ln)
                        and "error" in ln.lower()
                        for ln in _out.splitlines())
                    if _hit:
                        return False
                return True
            except Exception:  # noqa: BLE001 — probe is best-effort
                return None
            finally:
                if _saved_tcl is not None:
                    _tcl.write_bytes(_saved_tcl)
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
    """Delegate to the canonical capybase.merge_intent.side_preservation.

    The WORKING classification's numbers MUST match the orchestrator's
    wholesale-winner floor — one implementation (consistency contract
    documented there).
    """
    from capybase.merge_intent import side_preservation
    return side_preservation(base_text, side_text, output_text)


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


def _oracle_line_presence(content: str, oracle: str) -> float | None:
    """Multiset line presence: the share of the oracle's nonblank lines
    (counted with multiplicity) the output contains — the S28-167
    census's exact computation. Multiset counting so a line repeated in
    the output can never cover more oracle occurrences than the oracle
    itself has. Whitespace-normalized like every other line judge."""
    from collections import Counter as _Counter
    o = [ln.strip() for ln in oracle.splitlines() if ln.strip()]
    if not o:
        return None
    out = _Counter(ln.strip() for ln in content.splitlines() if ln.strip())
    want = _Counter(o)
    return sum(min(n, out[ln]) for ln, n in want.items()) / len(o)


def _oracle_order_score(content: str, oracle: str) -> float | None:
    """Normalized order score: the share of the LONGER line sequence the
    matching-block decomposition covers. Order-sensitive where the token
    sim is not — the oracle's lines returned in scrambled order score
    presence 1.0 but well below 1.0 here. difflib's block decomposition
    under-approximates the true LCS; that bias is conservative for the
    audit (it can only deepen a reorder signal, never mask one)."""
    a = [ln.strip() for ln in content.splitlines() if ln.strip()]
    b = [ln.strip() for ln in oracle.splitlines() if ln.strip()]
    if not a or not b:
        return None
    from capybase.merge_intent import _SIDE_CHURN_MULTISETH_LINES as _guard
    if max(len(a), len(b)) > _guard:
        return None  # S28-164: the quadratic matcher is monster-file-only
    import difflib as _dl
    m = sum(
        blk.size
        for blk in _dl.SequenceMatcher(
            None, a, b, autojunk=False).get_matching_blocks())
    return m / max(len(a), len(b))


def _oracle_order_fields(content: str, oracle: str) -> tuple[float | None, float | None]:
    """(oracle_line_presence, oracle_order_score) for a resolved output —
    the S28-167 pair, recorded beside matches_oracle on every row."""
    if not content:
        return None, None
    try:
        return (
            _oracle_line_presence(content, oracle),
            _oracle_order_score(content, oracle),
        )
    except Exception:
        return None, None


def _oracle_check_inapplicable(
        *, expected: str, language: str,
        marker_free: bool | None, compiles: bool | None,
        gate_applies: bool = True,
        compiles_from_build: bool = False) -> bool:
    """S28-146: did the ORACLE fail the same TEXTUAL check the candidate
    failed? When yes, the check is inapplicable for the file — the oracle
    defines correctness, so a verdict computed from that check would
    label oracle-class content a resolver failure (the 17-row class:
    sim >= 0.95, 8 at 1.000, all compiles=False).

    Only the checks the runner applies to the TEXT calibrate for free:
    markers, brace balance, python compile. A build-derived compiles
    verdict (``compiles_from_build``) is S28-144's oracle_builds
    business — the harness cannot re-run the tree build on the oracle
    text alone, so this guard declines there.
    """
    if not expected:
        return False
    if marker_free is False and _contains_markers(expected):
        return True
    if compiles is not False or not gate_applies or compiles_from_build:
        return False
    if language == "python":
        return _py_compiles(expected) is False
    return _brace_balanced(expected, language) is False


#: S28-170(2): outcome quality order for the repeat-flip census. A PASS
#: repeat beats an ESCALATE kept verdict; equal ranks compare sim.
_VERDICT_RANK = {"PASS": 6, "WORKING": 5, "NEAR_MATCH": 4,
                 "GATE_UNAVAILABLE": 3, "UNVERIFIED": 2, "ESCALATE": 2,
                 "ORACLE_DIVERGENT": 1}


def _verdict_rank(v: str) -> int:
    return _VERDICT_RANK.get(v or "", 0)


def _is_repeat_flip(r: "CaseResult") -> bool:
    """S28-170(2): True when the row's best repeat beats the kept
    verdict — by outcome rank, or by sim within the same rank. Such rows
    are the repeat-flip queue: the cheapest re-score targets in the
    corpus (a repeat already proved the better outcome)."""
    if not r.best_repeat_verdict:
        return False
    kept, best = _verdict_rank(r.verdict), _verdict_rank(r.best_repeat_verdict)
    if best != kept:
        return best > kept
    return (r.best_repeat_sim or 0.0) > (r.matches_oracle or 0.0) + 0.005


def _promote_best_repeat(r: "CaseResult", kept_verdict: str,
                         records: list, best_i: int) -> str:
    """S28-239.2/240.1 (queue item 2): copy the best repeat's outcome
    fields (verdict, sim, reason, session_id) over the kept row's,
    recording the demoted record on ``r.repeat_flips``. Zero new model
    requests — the repeats are already paid; the harvest's per-case rows
    stop understating the system (trial15: 4/15 rows kept a worse
    verdict than an existing repeat). Returns the promoted verdict."""
    best = records[best_i]
    prev = list(r.repeat_flips or [])
    prev.append({
        "verdict": kept_verdict,
        "matches_oracle": r.matches_oracle,
        "reason": r.reason,
        "session_id": r.session_id,
    })
    r.repeat_flips = prev
    r.matches_oracle = best.matches_oracle
    r.reason = best.reason
    r.session_id = best.session_id
    # S28-265 (0126/0130's case studies): the VERDICT-EVIDENCE fields
    # ride too — a promoted row carrying the demoted row's
    # harness_builds (0126: runner_c_build FAIL under a PASS verdict)
    # or missing them entirely (0130: the cold/warm divergence
    # invisible) is an auditor trap. The row must be self-consistent.
    for _f in ("compiles", "marker_free", "oracle_builds",
               "harness_builds", "ship_gate_unproven",
               "oracle_equivalent"):
        setattr(r, _f, getattr(best, _f, None))
    r.verdict = r.best_repeat_verdict
    return r.best_repeat_verdict


def _compile_evidence_missing(events) -> bool:
    """S28-170(1): the escalation is a compile-evidence artifact.

    True when the session's own acceptance_trust proposed FOR REVIEW
    because compile evidence was missing AND a gate build timed out (the
    SYNTAX_ONLY degrade, a timed-out pre-continue gate, or a timeout
    probe). The cold-tree duckdb-0053 shape: identical candidates PASS
    on a warm tree — the escalation measured the environment, not the
    resolver. Requires BOTH signals: the engine's own confession and the
    missed deadline that explains it."""
    trust = timed_out = False
    for e in events or []:
        t = getattr(e, "event_type", None)
        p = getattr(e, "payload", None) or {}
        if t == "acceptance_trust" and p.get("decision") == "PROPOSE_FOR_REVIEW":
            reasons = p.get("reasons") or []
            if any("compile evidence missing" in str(x) for x in reasons):
                trust = True
        if (t == "build_state"
                or (t == "tests_finished" and p.get("timed_out"))
                or (t == "build_probe" and p.get("outcome") == "timeout")):
            timed_out = True
    return trust and timed_out


#: S28-176(a): the head-region bounds — a terminal gcc diagnostic at or
#: above the include/type-visibility territory (first ~50 lines) of a
#: file whose conflict units start at the head is the class that killed
#: the near-oracle REPAIR_FAILUREs (libuv-0089's uv_loop_t at line 3,
#: duckdb-0099's optional_ptr at 1:1).
_HEAD_REGION_LINE_LIMIT = 50
_HEAD_REGION_UNIT_START = 10


def _terminal_error_attribution(events) -> tuple[int | None, bool]:
    """S28-176(a): the LAST gcc diagnostic in the captured session events,
    attributed. Returns (error_line, head_region). Head region = the
    error sits at or above the include/type-visibility territory AND the
    conflict units start at the head (or no unit info survived — the
    error line alone still reads head-ish at <=50)."""
    import re as _re
    pat = _re.compile(r"[\w./\\+-]+:(\d+):\d+: error: ")
    unit_starts: list[int] = []
    last_line: int | None = None
    for e in events or []:
        t = getattr(e, "event_type", None)
        p = getattr(e, "payload", None) or {}
        if t == "conflict_unit_extracted":
            uid = str(p.get("unit_id") or "")
            parts = uid.split(":")
            if len(parts) >= 2:
                try:
                    unit_starts.append(int(parts[-2]))
                except ValueError:
                    pass
        blob = None
        for k in ("errors", "stderr_tail", "stdout_tail", "message",
                  "diagnostics"):
            v = p.get(k)
            if isinstance(v, str) and " error: " in v:
                blob = v
                break
        if blob:
            for m in pat.finditer(blob):
                try:
                    last_line = int(m.group(1))
                except ValueError:
                    pass
    if last_line is None:
        return None, False
    head = last_line <= _HEAD_REGION_LINE_LIMIT and (
        not unit_starts or min(unit_starts) <= _HEAD_REGION_UNIT_START)
    return last_line, head


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
    then the GATE_UNAVAILABLE override: a gate rejection where
    the ORACLE fails the same gate (oracle_builds probed on the live tree) is
    a sandbox artifact, not a resolver failure — sim >= 0.95 for any
    verdict, >= 0.80 for escalations (S28-144: the escalation cannot
    implicate a merge when the oracle fails the gate too). Above that,
    S28-146: a failed TEXTUAL check the oracle fails too (verified by
    running the check on the oracle text) makes the check inapplicable —
    the verdict follows sim. ESCALATE_TOOLCHAIN comes
    first: the preflight proved all three texts (both sides + oracle) fail
    the gate identically — the case is un-passable by construction."""
    if getattr(r, "toolchain_dead", False):
        return "ESCALATE_TOOLCHAIN"
    if getattr(r, "era_header_dead", False):
        # S28-243.2 (queue item 6): the pre-screen's GU door — the
        # conflict file needs tree-absent APIs and the ORACLE uses them
        # too, so the gate cannot judge any resolution here (the
        # environment's era gap, not a resolver failure).
        return "GATE_UNAVAILABLE"
    if r.escalated:
        # S28-253 (build-what-you-ship's fresh-gate read, same flag): an
        # escalation whose SHIPPED buffer the HARNESS's own build passes,
        # marker-free at sim >= PASS, reads PASS — the escalation reason
        # cited the session's stale/poisoned gate (fmt-0003: the session
        # probe failed in 0.6s on cmake state while the harness build of
        # the same content passed in 6.7s with oracle PASS). The
        # acceptance rerun named this: the session gate cannot be the
        # authority for its own poisoned environment; the harness build
        # can. Order: ahead of the UNVERIFIED doors — a passing harness
        # build is stronger evidence than either undecidable signal.
        if (_SHIP_GATE_READ
                and r.compiles and r.marker_free
                and (r.matches_oracle or 0.0) >= PASS_THRESHOLD):
            return "PASS"
        # S28-265 (case study: scikit-0005's sim-0.998 row): the
        # oracle-IDENTITY door, the relabel's top rung (same flag) —
        # the content IS the human resolution (sim >= 0.99,
        # marker-free) and the oracle probe is UNDECIDABLE (None): the
        # tree cannot compile the oracle itself, so the class is the
        # environment's (S28-144's identity doctrine extended to the
        # undecidable half). GU, not UNVERIFIED — "we couldn't judge"
        # is dishonest when the answer is the oracle's own content.
        # Ordered after the fresh-gate read (a passing harness build is
        # the stronger claim); the 0.80-0.99 band stays UNVERIFIED.
        if (_UNVERIFIED_RELABEL
                and r.marker_free and (r.matches_oracle or 0.0) >= 0.99
                and getattr(r, "oracle_builds", None) is None):
            return "GATE_UNAVAILABLE"
        if getattr(r, "compile_evidence_missing", False):
            # S28-170(1): the environment could not judge the merge (the
            # gate build timed out and the engine proposed FOR REVIEW on
            # missing compile evidence) — the S28-161 UNVERIFIED class at
            # the runner level, excluded from capability denominators.
            return "UNVERIFIED"
        # S28-244 (queue item 10, pilot-gated via CAPYBASE_UNVERIFIED_RELABEL):
        # escalated + the oracle probe UNDECIDABLE (None — post-S28-192(c)
        # a timeout reads None, not False) + sim >= the S28-144 floor
        # (0.80): the environment could not judge the merge, so the row
        # reads UNVERIFIED rather than ESCALATE — completing S28-192's
        # original intent; the harvest's failure counts stop absorbing
        # the environment's blind spot. The label history (GU -> ESCALATE
        # at constant sim across the hygiene fix) is the argument.
        if (_UNVERIFIED_RELABEL
                and getattr(r, "oracle_builds", None) is None
                and (r.matches_oracle or 0.0) >= 0.80):
            return "UNVERIFIED"
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
    elif getattr(r, "oracle_check_inapplicable", False):
        # S28-146: the ORACLE fails the same textual check on the same
        # file — the check cannot implicate the candidate, so the verdict
        # follows sim (the anomaly rides oracle_check_inapplicable).
        if r.matches_oracle >= PASS_THRESHOLD:
            verdict = "PASS"
        elif r.matches_oracle >= 0.80:
            verdict = "NEAR_MATCH"
        else:
            verdict = "ORACLE_DIVERGENT"
    elif r.marker_free is None or r.compiles is None:
        # S28-161: the checks never ran (exception path / harness crash) —
        # "unverified" must never read as the worst verdict (17 s28 rows at
        # sim >= 0.95, 8 at 1.000, were mislabeled ORACLE_DIVERGENT this
        # way). Excluded from denominators by the results tooling; the sim
        # stays on the row for post-hoc analysis.
        verdict = "UNVERIFIED"
    else:
        verdict = "ORACLE_DIVERGENT"
    if (verdict in ("ESCALATE", "ORACLE_DIVERGENT")
            and getattr(r, "oracle_builds", None) is False
            and r.matches_oracle >= 0.95):
        return "GATE_UNAVAILABLE"
    # S28-144(1): an ESCALATED session whose oracle ALSO fails the build
    # is un-passable in this environment — the escalation cannot implicate
    # the merge. The NEAR_MATCH bar (0.80) widens the old 0.95 door: the
    # census population (duckdb-0126/0127 at 0.899/0.916, php-0116 at
    # 0.894) sat just under it.
    if (verdict == "ESCALATE"
            and getattr(r, "oracle_builds", None) is False
            and r.matches_oracle >= 0.80):
        return "GATE_UNAVAILABLE"
    return verdict


def _recover_infra_lost(r: "CaseResult") -> bool:
    """S28-159: verdict-loss recovery for SETUP_FAILED rows with content.

    A setup failure that strikes AFTER the run already produced scoreable
    content (matches_oracle >= 0.80, the NEAR_MATCH bar) is INFRA_LOST —
    the resolution existed (php-0089's first run sat at sim 0.9998) and
    only the verdict was lost to infrastructure (the 0106 re-score
    incident class). Such rows are excluded from every capability
    denominator (the SETUP_FAILED doctrine) and queued for re-score
    instead of silently reading as failures. Returns True when
    reclassified. Pure on ``r`` apart from the two field writes.
    """
    if r.terminal_reason != "SETUP_FAILED":
        return False
    if r.matches_oracle < 0.80:
        return False
    r.terminal_reason = "INFRA_LOST"
    r.verdict = "INFRA_LOST"
    return True


class _OracleCalibratedVerification:
    """S28-140 part 1: the oracle-calibration gate (eval-harness side).

    The toolchain-era preflight already compiled the ORACLE in the
    materialized tree; when it fails with real compile errors, those
    error texts are the oracle's OWN defects — a candidate hard failure
    carrying the same text is the validator rejecting against an
    impossible bar (nlohmann-json-0038: three candidates rejected on
    "stray '@' in program" while the case scored sim 1.00). This wrapper
    downgrades matching hard failures to warnings and recomputes the
    verdict. Contamination guard: only the oracle's normalized error
    TEXTS cross into the gate — never the oracle source; the engine and
    its verification pipeline are unmodified.
    """

    def __init__(self, inner, exempt_errors, case_id: str = ""):
        self._inner = inner
        self._exempt = [e for e in (exempt_errors or []) if e]
        self._case_id = case_id
        self.exemptions_applied: list[dict] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def _calibrate(self, r):
        if r.passed or not self._exempt:
            return r
        from capybase.verification import VerificationWarning
        kept, moved = [], []
        for f in (r.hard_failures or []):
            msg = getattr(f, "message", "") or ""
            matched = next(
                (e for e in self._exempt if e and e in msg), None)
            if matched is not None:
                moved.append((f, matched))
            else:
                kept.append(f)
        if not moved:
            return r
        r.hard_failures = kept
        r.passed = not kept
        for f, matched in moved:
            r.warnings = list(getattr(r, "warnings", []) or []) + [
                VerificationWarning(
                    validator=getattr(f, "validator", "") or "oracle_calibrated",
                    message=f"[oracle-calibrated] {msg}")]
        self.exemptions_applied.append({
            "matched": moved[0][1],
            "downgraded": len(moved),
            "remaining_hard": len(kept),
        })
        return r

    def verify(self, *a, **kw):
        return self._calibrate(self._inner.verify(*a, **kw))

    def verify_file(self, *a, **kw):
        return self._calibrate(self._inner.verify_file(*a, **kw))


def _oracle_exempt_errors(probe: dict | None) -> list[str]:
    """The oracle's own compile-error texts from the preflight probe.

    Non-empty only when the oracle probe RAN and failed with a real
    error signature (rc != 0, non-empty sig) while the case was NOT
    toolchain-dead (the sides did not fail identically — the era class
    already owns that shape)."""
    if not probe or probe.get("toolchain_dead"):
        return []
    o = (probe.get("probes") or {}).get("oracle") or {}
    if o.get("rc") in (0, None):
        return []
    return [s for s in (o.get("sig") or []) if s]


# S28-140 part 1: opt-in via --oracle-calibrate (measurement-semantics
# change; the targeted rerun measures it before any default flip).
_ORACLE_CALIBRATE = False
# S28-162: the relaxed floor band A/B (default off; --relaxed-floor).
_RELAXED_FLOOR = False


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
    # Sprint-27: offline + lint-capped env scoped to THIS case when its
    # deps vendored (the 2019 rustdoc drift needs --cap-lints; the vendor
    # needs offline). Previously set process-wide on the first vendored
    # case — LEAKING into every later rust case: a cold-cache non-vendored
    # case would fail cargo resolution OFFLINE and era-exit falsely (the
    # 0008 chain's sibling hazard). Restored in the finally below.
    _prev_net = os.environ.get("CARGO_NET_OFFLINE")
    _prev_rustflags = os.environ.get("RUSTFLAGS")
    try:
        repo = Path(td) / "r"
        try:
            _materialize_conflict(case, repo, crate_source=crate_source)
            if (repo / "vendor").is_dir():
                os.environ["CARGO_NET_OFFLINE"] = "true"
                os.environ["RUSTFLAGS"] = "--cap-lints warn"
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
        # S28-171(1): the harness's own builds are instrumented (site-
        # tagged, durations) — the session journal cannot see them, so
        # the wall-time census reads them from the row.
        _harness_builds: list = []

        def _timed_harness_build(site: str, fn, *args, **kwargs):
            # S28-191(3): a build that RAISES (the timeout path —
            # _run_shell_tree re-raises TimeoutExpired) must still land
            # in the census: previously the entry vanished and the
            # caller's `_harness_builds[-1]` mislabelled the PREVIOUS
            # site's entry.
            _t0 = time.time()
            try:
                out = fn(*args, **kwargs)
            except Exception:
                _harness_builds.append({
                    "site": site, "duration_s": round(time.time() - _t0, 1),
                    "outcome": "timeout"})
                raise
            _harness_builds.append(
                {"site": site, "duration_s": round(time.time() - _t0, 1)})
            return out

        # S28-268: the era signature memo — when run N recorded that this
        # case's terminal gate failures were a symbol-subset of the
        # probe's side failures (the era errors known before the first
        # draw), run N+1 classifies at setup WITHOUT re-running the probe
        # builds. Validity: the spec content hash + the armed-flag
        # fingerprint (arms changes re-probe).
        if _ERA_PRESCREEN and _cached_probe is None:
            _memo_entry = _era_memo_load().get(case.id) or {}
            if (_memo_entry.get("spec_sha") == _era_spec_sha(case)
                    and _memo_entry.get("flags_fp") == _era_flags_fp):
                res.harness_builds = _harness_builds or None
                return _mark_era_header_dead(
                    res, _memo_entry.get("screen") or {}, t0)

        if _cached_probe is None:
            _cached_probe = _timed_harness_build(
                "toolchain_probe", _toolchain_era_probe,
                repo, case, has_crate=crate_source is not None)
            _harness_builds[-1]["outcome"] = (
                "dead" if (_cached_probe or {}).get("toolchain_dead")
                else ("declined" if _cached_probe is not None else "skipped"))
            _TOOLCHAIN_PROBE_CACHE[case.id] = _cached_probe
        if _cached_probe is not None and _cached_probe.get("toolchain_dead"):
            # S28-171(1) review: carry the probe's own instrumentation —
            # the early return precedes the scoring block's assignment.
            res.harness_builds = _harness_builds or None
            return _mark_toolchain_dead(res, _cached_probe, t0)
        if _cached_probe is not None:
            # Declined classification — still recorded for the audit trail
            # (the harvest census reads per-case probe outcomes).
            res.toolchain_probe = _cached_probe
            # S28-243.2 (queue item 6, CAPYBASE_ERA_PRESCREEN=1): the
            # era-header pre-screen — compose with the probe's held
            # side-build failures; a case whose ORACLE references
            # tree-absent APIs is unpassable in-place, classified BEFORE
            # any model budget.
            if _ERA_PRESCREEN:
                _screen = _era_header_screen(repo, case, _cached_probe)
                if _screen and _screen.get("era_header_dead"):
                    res.harness_builds = _harness_builds or None
                    res.toolchain_probe = {
                        **(_cached_probe or {}), "era_header_screen": _screen}
                    return _mark_era_header_dead(res, _screen, t0)
        cfg = _config_for(case, has_crate=crate_source is not None)
        if _RELAXED_FLOOR:
            cfg.future.enable_floor_relaxed_band = True
        engine = ResolutionEngine(cfg.model, client=client)
        orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                            out=lambda *_a, **_k: None)
        # S28-170(1): capture the session's events in memory — the
        # compile-evidence-missing guard reads them after the run.
        _session_events: list = []
        try:
            orch.journal.subscribe(_session_events.append)
        except Exception:  # noqa: BLE001 — capture is best-effort
            pass
        # S28-140 part 1: oracle-calibrated validation (opt-in). The
        # preflight's oracle probe ran above; its error texts become the
        # exemption set the gate downgrades to warnings.
        if _ORACLE_CALIBRATE:
            _ex = _oracle_exempt_errors(_cached_probe)
            if _ex:
                orch.verification = _OracleCalibratedVerification(
                    orch.verification, _ex, case_id=case.id)
        try:
            step = orch.run()
            res.escalated = bool(step.escalated)
            res.reason = step.reason or ""
            # Mechanism reporting: who produced the accepted candidates
            # (the results histogram). Single-step scenarios: the last
            # step's outcomes are the case's units.
            try:
                (res.resolution_bucket,
                 res.provenance_mix) = classify_resolution_bucket(
                    getattr(step, "outcomes", None))
            except Exception:  # noqa: BLE001 — reporting is advisory
                res.resolution_bucket, res.provenance_mix = "", {}
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
        # S28-140 part 1: journal the calibration outcomes for the census.
        _cal = getattr(orch.verification, "exemptions_applied", None)
        if _cal:
            try:
                orch.journal.emit(
                    "oracle_calibrated_exemption",
                    {"applied": len(_cal), "detail": _cal[:5]},
                    step_index=getattr(orch, "step", 0))
            except Exception:  # noqa: BLE001 — journaling is advisory
                pass
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
        # S28-170(1): the compile-evidence-missing guard — an escalated
        # session whose acceptance_trust proposed FOR REVIEW on missing
        # compile evidence AND whose gate build timed out measured the
        # environment, not the resolver.
        if res.escalated:
            res.compile_evidence_missing = _compile_evidence_missing(
                _session_events)
        # S28-173(3): the splice's preservation-net flag (eval-only).
        res.splice_loser_dropped = any(
            getattr(e, "event_type", None) == "splice_loser_dropped"
            for e in _session_events)
        # S28-194: carry the engine's acceptance trust onto the row
        # (last acceptance_trust event wins — the splice_loser_dropped
        # capture pattern).
        # S28-232: the ship-gate census — escalated compile-gated rows
        # with no passing probe after the last acceptance shipped
        # unproven content (the fmt-0003 pattern: acceptance at seq 29
        # on a red last probe, then strangers' probes dominated the
        # escalation reason).
        if (case.language in ("c", "cpp", "c++") and res.escalated):
            _last_accept_seq = max(
                (getattr(e, "seq", -1) for e in _session_events
                 if getattr(e, "event_type", None) == "candidate_accepted"),
                default=None)
            if _last_accept_seq is not None:
                _proved_after = any(
                    getattr(e, "event_type", None) == "build_probe"
                    and (getattr(e, "payload", None) or {}).get("outcome") == "pass"
                    and getattr(e, "seq", -1) > _last_accept_seq
                    for e in _session_events)
                res.ship_gate_unproven = not _proved_after
        for _e in _session_events:
            if getattr(_e, "event_type", None) == "acceptance_trust":
                _p = getattr(_e, "payload", None) or {}
                if _p.get("tier"):
                    res.acceptance_tier = str(_p["tier"])
                if _p.get("decision"):
                    res.acceptance_decision = str(_p["decision"])
        # S28-176(a): the terminal gcc diagnostic, attributed (eval-only).
        res.terminal_error_line, res.failure_head_region = (
            _terminal_error_attribution(_session_events))
        # C post-hoc compile check must run WHILE the repo tree is on disk (the
        # finally below removes it). python/rust checks operate on the content
        # string alone, so they run after cleanup; the C build needs the tree.
        c_builds_result: bool | None = None
        if case.language in ("c", "cpp", "c++") and content:
            # S28-192(c): a timed-out runner build RAISES (the memo is
            # set inside _c_builds first) — catch so the case still
            # scores; the census entry already reads outcome=timeout.
            try:
                c_builds_result = _timed_harness_build(
                    "runner_c_build", _c_builds, repo, case)
            except Exception:  # noqa: BLE001 — scoring is best-effort
                c_builds_result = None
            _harness_builds[-1]["outcome"] = (
                "pass" if c_builds_result is True
                else "fail" if c_builds_result is False
                else _harness_builds[-1].get("outcome", "na"))
            if c_builds_result is False and case.id in _LAST_C_BUILD_DIAG:
                _harness_builds[-1]["diag"] = _LAST_C_BUILD_DIAG[case.id]
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
                oracle_builds_result = _timed_harness_build(
                    "oracle_probe", _oracle_builds, repo, case, crate_source,
                    runner_build_passed=(c_builds_result is True))
                _harness_builds[-1]["outcome"] = (
                    "pass" if oracle_builds_result is True
                    else "fail" if oracle_builds_result is False else "na")
            except Exception:  # noqa: BLE001 — classification is best-effort
                oracle_builds_result = None
        # S28-110: the API-drift probe (attribution-only, zero model cost).
        # When the merge escalated with undeclared-member failures, check
        # whether the named members exist in the REPLAYED tree but not the
        # CURRENT tree — cross-revision API drift (S28-95's validated
        # 2-grep probe): the conflict is under-scoped, the correct merge
        # must carry the API-defining files, and no in-case mechanism can
        # fix it. Stamps api_drift + evidence onto the row so the next
        # failure analysis reads the class directly.
        api_drift_result: bool | None = None
        api_drift_evidence: str | None = None
        if (case.language in ("c", "cpp", "c++")
                and res.escalated and case.merge_sha and content
                and crate_source is not None):
            try:
                drift = _api_drift_probe(
                    clone=crate_source, merge_sha=case.merge_sha,
                    path=case.path, expected_current=case.current,
                    expected_replayed=case.replayed,
                    escalated_reason=res.reason or "",
                )
                if drift is not None:
                    api_drift_result = True
                    api_drift_evidence = drift
            except Exception:  # noqa: BLE001 — attribution is best-effort
                pass
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
        # Sprint-27: restore the pre-case env (offline/lints were scoped).
        for _k, _v in (("CARGO_NET_OFFLINE", _prev_net),
                       ("RUSTFLAGS", _prev_rustflags)):
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v
        # D3: when the main thread owns the temp dir, it cleans up after the
        # worker returns or times out. When we own it, clean up here.
        if owns_td:
            import shutil
            shutil.rmtree(td, ignore_errors=True)
    res.elapsed = time.time() - t0
    res.oracle_builds = oracle_builds_result
    res.api_drift = api_drift_result
    res.api_drift_evidence = api_drift_evidence
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
    # S28-267: the gate-divergence suspect — the session's LAST
    # in-session build failed the content the harness's build passes.
    if (c_builds_result is True and _session_events):
        _sess_bp = [e for e in _session_events
                    if getattr(e, "event_type", None) == "build_probe"]
        res.gate_divergence_suspect = bool(
            _sess_bp
            and (getattr(_sess_bp[-1], "payload", None) or {}).get("outcome")
            == "fail")
    res.matches_oracle = _token_jaccard(content, case.expected_resolved) if content else 0.0
    # Sprint-20 S20.11: skeleton intent similarity (EVAL ONLY — never a
    # gate). Recorded on every result; the harvest cross-tabs it against
    # matches_oracle to surface idiomatic-rewrite candidates (low jaccard,
    # high skeleton).
    res.skeleton_similarity = (
        _skeleton_similarity(content, case.expected_resolved)
        if content else 0.0)
    res.loser_preservation, res.winner_preservation = _preservation_fields(case, content)
    # S28-167: order-sensitive secondary metrics (EVAL ONLY — never a
    # gate). Recorded on every row; the harvest cross-tabs sim-high /
    # order-low PASS rows as the reorder-audit population and reads the
    # presence field as the oracle-equivalence doctrine's basis.
    res.oracle_line_presence, res.oracle_order_score = _oracle_order_fields(
        content, case.expected_resolved)
    # S28-146: calibrate the runner's textual checks against the oracle.
    # REVIEW (2026-09-23): guarded on non-empty content — an EMPTY
    # resolution fails both checks vacuously, and marking the check
    # inapplicable would misdescribe a nothing-output as oracle-class.
    res.oracle_check_inapplicable = bool(content) and _oracle_check_inapplicable(
        expected=case.expected_resolved, language=case.language,
        marker_free=res.marker_free, compiles=res.compiles,
        gate_applies=bool(_sga(case.path)),
        compiles_from_build=(
            case.language in ("c", "cpp", "c++")
            and c_builds_result is not None))
    # S28-144(2): the oracle-equivalence flag (eval-only).
    res.oracle_equivalent = (
        res.oracle_builds is False
        and res.marker_free is True
        and res.matches_oracle >= 0.99)
    # S28-171(1): the harness's own builds ride the row.
    res.harness_builds = _harness_builds or None
    # S28-268: the era memo WRITE — when the terminal gate failure's
    # symbols are a subset of the probe's side symbols (the era errors
    # were known at setup), persist so run N+1 classifies without
    # re-running the probe builds.
    if (_ERA_PRESCREEN and res.escalated
            and isinstance(res.toolchain_probe, dict)
            and res.toolchain_probe.get("probes")):
        _probe_syms = _era_side_symbols(res.toolchain_probe)
        if _probe_syms:
            _final_syms = [s for s in _era_side_symbols(
                {"probes": {"current": {"sig": [res.reason or ""]}}})
                if s in _probe_syms]
            if _final_syms:
                _era_memo_store(case.id, {
                    "spec_sha": _era_spec_sha(case),
                    "flags_fp": _ERA_FLAGS_FINGERPRINT,
                    "screen": {
                        "era_header_dead": True,
                        "missing_symbols": _probe_syms[:8],
                        "oracle_uses_missing": _final_syms[:8],
                        "oracle_repeats_invalid": [],
                    },
                })
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
    # D9 (s27): eagerly extract the tcl dev tree (headers + lib +
    # tclConfig.sh) and expose the config path for sqlite's output-test
    # command (CAPYBASE_TCL_CONFIG_SH; the command is assembled at import
    # from the env, so it must be set before cases run).
    _ensure_dataset_includes()
    _tcl_cfg = _tcl_config_sh()
    if _tcl_cfg:
        os.environ.setdefault("CAPYBASE_TCL_CONFIG_SH", _tcl_cfg)
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
    ap.add_argument("--repeat-all", type=int, default=1,
                    help="Like --repeat-nonpass but repeats EVERY case (a first-try "
                         "PASS can itself be variance). The majority-of-3 yardstick "
                         "for calibration A/Bs (B9/B10): the WITHOUT arm is the "
                         "harvest itself; the WITH arm needs honest repeats on "
                         "coin-flip cases that pass first try 50-70%% of the time. "
                         "When both flags are set, --repeat-all wins.")
    ap.add_argument("--preserve-flights", default=None,
                    help="Directory to copy per-case orchestrator session artifacts into "
                         "(FR2a flight recorder). Produces <dir>/flights/<case_id>/<session_id>/ "
                         "and <dir>/manifest.json. Required for shadow-jury replay.")
    ap.add_argument("--census", default=None,
                    help="Print a failure census report from an existing results JSON and exit. "
                         "Classifies each escalated case by root diagnostic category. Example: "
                         "--census /tmp/capybase-live/c-live-full-corpus.json")
    ap.add_argument("--relaxed-floor", action="store_true",
                    help="S28-162 A/B: enable the wholesale-winner floor's "
                         "relaxed band (ratio 0.81, shrinkage dominance "
                         "0.35; every firing stays output-gated).")
    ap.add_argument("--oracle-calibrate", action="store_true",
                    help="S28-140 part 1: when the toolchain-era preflight's oracle probe "
                         "fails with real compile errors, downgrade candidate hard failures "
                         "carrying the same error text to warnings (the oracle's own defects "
                         "cannot implicate the resolution). Eval-only; changes measurement "
                         "semantics.")
    args = ap.parse_args()
    globals()["_ORACLE_CALIBRATE"] = bool(args.oracle_calibrate)
    globals()["_RELAXED_FLOOR"] = bool(args.relaxed_floor)

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
    # DEF-3: the calibration audit trail — which profile, which sections,
    # which prompt layout is in force for this run (harvest attribution).
    _, _, _cal_report = apply_to_config(Config(), _PROVIDER)
    print(
        f"calibration: {_cal_report['profile_model']} @ "
        f"{_cal_report['profile_path']} — sections "
        f"{','.join(_cal_report['sections'])}, prompt "
        f"{_cal_report['prompt_layout']}")
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
            _case_t0 = time.time()
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

            # The llm column's source of truth: count every model call
            # this CASE's whole resolution makes (candidates, repairs,
            # ballots, comment reconciliation all funnel through the
            # client's three call surfaces). Per-run counter — each
            # repeat gets a fresh wrapper, so the kept row's flag
            # reflects its own run.
            _counting = _CallCountingClient(client)

            def _worker():
                try:
                    _res = run_case(case, _counting, flights_dir=flights_dir,
                                    td=_td, crate_source=_crate)
                    _res.model_involved = _counting.calls > 0
                    _holder.append(_res)
                except Exception as exc:
                    _res = CaseResult(
                        id=case.id, language=case.language, dataset=case.dataset,
                        escalated=True,
                        conflict_region_count=case.marker_original.count("<<<<<<<"),
                        reason=f"harness error: {type(exc).__name__}: {str(exc)[:250]}")
                    _res.model_involved = _counting.calls > 0
                    _holder.append(_res)

            _th = threading.Thread(target=_worker, daemon=True)
            _th.start()
            _th.join(timeout=args.case_timeout or None)
            # D3: clean up the temp dir from the main thread. BUT only when the
            # worker has actually finished — if the thread is still alive
            # (timeout), destroying its temp dir causes a race: the daemon
            # thread tries to access .rebase-agent/sessions/ or run git, and
            # crashes with GitError/FileNotFoundError because the directory is
            # gone. Defer cleanup to the end of the run for timed-out cases.
            if _th.is_alive() and _engine_session_completed(
                    flights_dir, case.id, live_root=_td):
                # S28-137: the engine has ACCEPTED a candidate — the thread is
                # in post-resolution work (its own build probes/tests, then the
                # scoring builds; cold cmake runs ~300s each), which is
                # deterministic, not an endless CEGIS loop. Extend the wall
                # once by a scoring grace (half the case budget) so a finished
                # resolution gets scored instead of being abandoned and
                # mislabeled. The LIVE journal under _td is the decisive
                # source here: the flight copy only lands after orch.run()
                # returns, which is exactly the phase the wall dies in.
                _th.join(timeout=max(300, (args.case_timeout or 0) // 2))
            if _th.is_alive():
                # The worker is still running — abandon it (daemon) and record
                # an escalate. The next case starts fresh. DON'T destroy the
                # temp dir yet — the daemon thread may still write.
                deferred_cleanup.append(_td)
                print(f"\n      [TIMEOUT after {args.case_timeout}s — moving on]", end="")
                _timeout_reason = "case timeout after " \
                    f"{args.case_timeout}s (endless CEGIS retries)"
                if _engine_session_completed(flights_dir, case.id, live_root=_td):
                    _timeout_reason = (
                        f"case timeout after {args.case_timeout}s "
                        "(engine accepted; post-resolution scoring exceeded "
                        "the wall)")
                _trow = CaseResult(
                    id=case.id, language=case.language, dataset=case.dataset,
                    escalated=True,
                    conflict_region_count=case.marker_original.count("<<<<<<<"),
                    reason=_timeout_reason)
                # S28-169: the timeout path returned a fresh result with
                # elapsed=0.0 — 8 s28 TIMEOUT_CAPABILITY rows carry no wall
                # time, blinding the throughput analysis to exactly the
                # slowest cases. Record the real wall clock.
                _trow.elapsed = time.time() - _case_t0
                return _trow
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
        # --repeat-all (B9/B10 majority yardstick) subsumes --repeat-nonpass:
        # repeat regardless of the first verdict.
        _repeat_n = max(args.repeat_all, args.repeat_nonpass)
        _repeat_only_nonpass = args.repeat_all <= 1
        if _repeat_n > 1 and (not _repeat_only_nonpass or verdict != "PASS"):
            # Variance-aware majority: rerun non-PASS cases, keep the modal
            # verdict (the first run exhibiting it) so a single lucky or
            # unlucky draw doesn't label the case.
            from collections import Counter as _VC
            _verdicts = [verdict]
            _records = [r]
            for _k in range(_repeat_n - 1):
                print(f"\n      [repeat {_k + 2}/{_repeat_n}] ...", end=" ")
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
            # S28-170(2): stability + best-repeat evidence on the kept row.
            _best_i = max(range(len(_verdicts)),
                          key=lambda i: (_verdict_rank(_verdicts[i]),
                                         _records[i].matches_oracle or 0.0))
            _kept.stability = (
                "stable" if len(set(_verdicts)) == 1 else "unstable")
            _kept.best_repeat_verdict = _verdicts[_best_i]
            _kept.best_repeat_sim = _records[_best_i].matches_oracle
            if _kept is not r:
                print(f"      [majority: {_maj} (verdicts: {','.join(_verdicts)})]",
                      end=" ")
                r, verdict = _kept, _maj
            # The kept row exhibits the majority verdict — assigned BEFORE
            # any predicate reads it (the S28-264 trial catch: the
            # promotion's _is_repeat_flip read r.verdict while it was
            # still unassigned on the records[0] path, rank 0, and fired
            # a spurious promotion on every first-run-kept row).
            r.verdict = _maj
            # S28-239.2/240.1 (queue item 2, CAPYBASE_KEEP_BEST=1):
            # keep-the-best — the kept row flips to its best repeat's
            # outcome; the demoted record rides the row.
            if _KEEP_BEST_REPEATS and _is_repeat_flip(r):
                _kept_sim = r.matches_oracle
                _new_verdict = _promote_best_repeat(r, _maj, _records, _best_i)
                verdict = _new_verdict
                print(f"\n      [keep-the-best: promoted repeat {_new_verdict} "
                      f"sim={r.matches_oracle:.3f} over kept {_maj} "
                      f"sim={_kept_sim:.3f}]", end=" ")
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
        elif verdict == "ESCALATE_TOOLCHAIN":
            # s27-72 (sixth pass): the ladder had no arm — toolchain-era
            # cases landed in wrong_ct, inflating ORACLE_DIVERGENT and
            # disagreeing with the by-dataset tables in the same summary.
            escalate_ct += 1
        else:
            wrong_ct += 1
        r.verdict = verdict
        if not r.stability:
            r.stability = "single-run"
        r.terminal_reason = (
            _classify_terminal_reason(
                r.reason, elapsed_s=r.elapsed,
                budget_s=float(getattr(args, "case_timeout", 1200) or 1200))
            if r.escalated else "")
        # Subclassify timeouts: throughput (many regions overwhelm the budget)
        # vs capability (few regions but the model can't solve them).
        if r.terminal_reason == "TIMEOUT_CASE":
            if r.conflict_region_count > 20:
                r.terminal_reason = "TIMEOUT_THROUGHPUT"
            else:
                r.terminal_reason = "TIMEOUT_CAPABILITY"
        if _recover_infra_lost(r):
            verdict = r.verdict  # INFRA_LOST: excluded + queued, not ESCALATE
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
    print(f"GATE_UNAVAILABLE: {gate_ct}  (gate rejection the oracle shares — "
          f"sim >= 0.95 any verdict, >= 0.80 for escalations (S28-144) — "
          f"sandbox artifact, not a resolver failure)")
    oeq_ct = sum(1 for r in results if getattr(r, "oracle_equivalent", False))
    if oeq_ct:
        print(f"  oracle-equivalent: {oeq_ct} of the GATE_UNAVAILABLE rows are "
              f"marker-free at sim >= 0.99 — PASS-equivalent by identity with "
              f"an oracle that cannot build here (S28-144, eval-only flag)")
    ochk_ct = sum(1 for r in results
                  if getattr(r, "oracle_check_inapplicable", False))
    if ochk_ct:
        print(f"  check-inapplicable: {ochk_ct} rows failed a textual check the "
              f"ORACLE fails too — verdict follows sim (S28-146)")
    _unstable = sum(1 for r in results if r.stability == "unstable")
    if _unstable:
        print(f"  unstable verdicts: {_unstable} repeated rows disagreed across "
              f"runs (S28-170 — cold-start flips and model variance)")
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
    # Real-conflict pass rate: excludes SAFE_SKIP (no real conflict),
    # SETUP_FAILED (infrastructure failure — not a resolver outcome) and
    # INFRA_LOST (S28-159: setup failure AFTER scoreable content existed —
    # the verdict is lost, not the resolution) from the denominator.
    _excluded = ("SAFE_SKIP", "SETUP_FAILED", "INFRA_LOST")
    real_conflicts = [
        r for r in results if r.terminal_reason not in _excluded]
    real_pass = sum(1 for r in real_conflicts if r.verdict == "PASS")
    real_work = sum(1 for r in real_conflicts if r.verdict == "WORKING")
    safe_skip_ct = sum(1 for r in results if r.terminal_reason == "SAFE_SKIP")
    setup_fail_ct = sum(
        1 for r in results if r.terminal_reason == "SETUP_FAILED")
    infra_lost = [r for r in results if r.terminal_reason == "INFRA_LOST"]
    # Explicit denominator breakdown so pass-rate comparisons are meaningful
    # across runs (Sprint 8: 64/76 vs Sprint 9: 52/75 — the denominator
    # changed by 1 with no explanation).
    print(f"total: {len(results)} | SAFE_SKIP: {safe_skip_ct} | "
          f"SETUP_FAILED: {setup_fail_ct} | INFRA_LOST: {len(infra_lost)} | "
          f"real conflicts: {len(real_conflicts)}")
    if infra_lost:
        lost_ids = [r.id for r in infra_lost]
        print(f"INFRA_LOST re-score queue ({len(lost_ids)}): "
              f"{', '.join(lost_ids)}")
        try:
            (out.parent / "re-score-queue.json").write_text(json.dumps({
                "reason": "INFRA_LOST: first-run content (sim >= 0.80) lost "
                          "to a setup failure; re-score these cases",
                "cases": lost_ids,
            }, indent=2))
        except Exception:  # noqa: BLE001 — queue is best-effort
            pass
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
    # S28-170(2): the repeat-flip queue — rows whose best repeat beat the
    # kept verdict (by outcome rank, or sim within the rank). The corpus's
    # cheapest re-score population: a repeat already proved the better
    # outcome and its candidate sits in the preserved flights.
    _flips = [r for r in results if _is_repeat_flip(r)]
    if _flips:
        (out.parent / "repeat-flip-queue.json").write_text(json.dumps(
            [{"id": r.id, "kept_verdict": r.verdict,
              "kept_sim": r.matches_oracle,
              "best_repeat_verdict": r.best_repeat_verdict,
              "best_repeat_sim": r.best_repeat_sim,
              "repeat_verdicts": r.repeat_verdicts,
              "stability": r.stability} for r in _flips], indent=2))
        print(f"repeat-flip queue: {len(_flips)} rows whose best repeat beat "
              f"the kept verdict -> {out.parent / 'repeat-flip-queue.json'}")
    if dropped:
        print(f"\n!! SUBSET RUN — size guard dropped {len(dropped)} corpus cases;"
              " these results are NOT a full shard")
    print(f"\nfull results: {out}")


if __name__ == "__main__":
    main()
