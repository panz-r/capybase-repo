#!/usr/bin/env python3
"""Live-model evaluation harness.

Drives the capybase Orchestrator with a REAL OpenAICompatibleClient against the
configured local model (VibeThinker-3B via llama-server), on genuine git rebase
conflicts built in temp repos. Reports per-scenario correctness, provenance,
escalation status, and timing.

NOT part of the hermetic test suite — this makes real network calls. Run:

    .venv/bin/python scripts/live_eval.py

Scenarios mirror tests/conftest.py fixtures so the model is judged on the same
conflicts the fake-client tests assert against. A scenario "passes" when the
final file content contains the expected merged text (both sides' intent
preserved).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Make the package importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from capybase.adapters.llm_openai import OpenAICompatibleClient  # noqa: E402
from capybase.config import Config  # noqa: E402
from capybase.orchestrator import Orchestrator  # noqa: E402
from capybase.resolution_engine import ResolutionEngine  # noqa: E402


# ---------------------------------------------------------------------------
# tiny git helper (mirrors tests/conftest.py but standalone)
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "tester"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "t@example.com"
    env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = "2000-01-01T00:00:00"
    env["GIT_PAGER"] = "cat"
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        env=env, capture_output=True, text=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {args} failed (rc={proc.returncode}): {proc.stderr.strip()}")
    return proc


# ---------------------------------------------------------------------------
# scenario builders — each leaves the repo mid-rebase at a UU conflict.
# ---------------------------------------------------------------------------

@dataclass
class Scenario:
    name: str
    language: str
    repo: Path
    path: str  # conflicted file (relative)
    expect_substrings: list[str]  # correctness check — all must appear in resolved file
    reject_substrings: list[str] = field(default_factory=list)  # must NOT appear
    # For value-resolution conflicts where EITHER side's value (or a combination)
    # is a correct merge: at least one of these must appear. Empty = not used.
    expect_any_substrings: list[str] = field(default_factory=list)
    cargo: bool = False  # whether the repo has a Cargo.toml (runs cargo gate)


def _mk_repo() -> Path:
    d = Path(os.environ.get("CAPYBASE_LIVE_TMP", "/tmp/capybase-live")) / f"repo-{os.getpid()}-{time.time_ns()}"
    d.mkdir(parents=True, exist_ok=True)
    _git(d, "init", "-q", "-b", "main")
    return d


def scenario_py_simple() -> Scenario:
    """Python single-hunk: both sides edit the same return string."""
    repo = _mk_repo()
    base = "def greet():\n    return 'hello'\n"
    upstream = "def greet():\n    return 'hi'\n"
    replayed = "def greet():\n    return 'howdy'\n"

    (repo / "app.py").write_text(base); _git(repo, "add", "app.py"); _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "branch", "feat"); _git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text(replayed); _git(repo, "add", "app.py"); _git(repo, "commit", "-q", "-m", "rep")
    _git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text(upstream); _git(repo, "add", "app.py"); _git(repo, "commit", "-q", "-m", "up")
    _git(repo, "checkout", "-q", "feat")
    r = _git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected conflict"
    # This is a VALUE-RESOLUTION conflict: both sides preserved the `return`
    # statement and only the returned value diverged ('hi' vs 'howdy'). A correct
    # merge picks one side's value OR writes a combining expression — either is
    # valid (the base operation is preserved). So either literal must appear.
    return Scenario("py_simple", "python", repo, "app.py",
                    expect_substrings=["def greet():"],
                    expect_any_substrings=["'hi'", "'howdy'"],
                    reject_substrings=["<<<<<<<", "=======", ">>>>>>>", "return 'hello'"])


def scenario_py_multi_unit() -> Scenario:
    """Python two-hunk: services list + feature flags, both sides changed."""
    repo = _mk_repo()
    base = (
        'ENABLED_SERVICES = ["core", "cli"]\n\n\n'
        'class ServiceConfig:\n    name = "capybase"\n\n\n'
        'FEATURE_FLAGS = {\n    "cache": "off",\n    "metrics": "off",\n}\n'
    )
    upstream = (
        'ENABLED_SERVICES = ["core", "cli", "scheduler"]\n\n\n'
        'class ServiceConfig:\n    name = "capybase"\n\n\n'
        'FEATURE_FLAGS = {\n    "cache": "off",\n    "metrics": "on",\n}\n'
    )
    replayed = (
        'ENABLED_SERVICES = ["core", "cli", "reloader"]\n\n\n'
        'class ServiceConfig:\n    name = "capybase"\n\n\n'
        'FEATURE_FLAGS = {\n    "cache": "on",\n    "metrics": "off",\n}\n'
    )

    (repo / "cfg.py").write_text(base); _git(repo, "add", "cfg.py"); _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "branch", "feat"); _git(repo, "checkout", "-q", "feat")
    (repo / "cfg.py").write_text(replayed); _git(repo, "add", "cfg.py"); _git(repo, "commit", "-q", "-m", "rep")
    _git(repo, "checkout", "-q", "main")
    (repo / "cfg.py").write_text(upstream); _git(repo, "add", "cfg.py"); _git(repo, "commit", "-q", "-m", "up")
    _git(repo, "checkout", "-q", "feat")
    r = _git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected conflict"
    # Correct merge: scheduler AND reloader both added; cache AND metrics both 'on'.
    return Scenario("py_multi_unit", "python", repo, "cfg.py",
                    expect_substrings=["scheduler", "reloader", '"cache": "on"', '"metrics": "on"'],
                    reject_substrings=["<<<<<<<", "=======", ">>>>>>>"])


def scenario_rust_impl() -> Scenario:
    """Rust: struct field + impl additions vs upstream constant change."""
    repo = _mk_repo()
    base = (
        "pub struct Config {\n    pub name: String,\n    pub max_retries: u32,\n}\n\n"
        "impl Config {\n    pub fn new() -> Self {\n        Config {\n"
        '            name: "capybase".to_string(),\n            max_retries: 3,\n        }\n    }\n\n'
        '    pub fn label(&self) -> String {\n'
        '        format!("{} (retries={})", self.name, self.max_retries)\n    }\n}\n'
    )
    upstream = base.replace("max_retries: 3,", "max_retries: 5,").replace(
        'format!("{} (retries={})"', 'format!("[{}] retries={}"')
    # replayed: add timeout_ms field
    replayed = (
        "pub struct Config {\n    pub name: String,\n    pub max_retries: u32,\n    pub timeout_ms: u32,\n}\n\n"
        "impl Config {\n    pub fn new() -> Self {\n        Config {\n"
        '            name: "capybase".to_string(),\n            max_retries: 3,\n            timeout_ms: 10000,\n        }\n    }\n\n'
        '    pub fn label(&self) -> String {\n'
        '        format!("{} (retries={}, timeout={})", self.name, self.max_retries, self.timeout_ms)\n    }\n}\n'
    )

    (repo / "Cargo.toml").write_text('[package]\nname = "cfg"\nversion = "0.1.0"\nedition = "2021"\n')
    (repo / "src").mkdir()
    # lib.rs declares the module so cargo has a valid crate target.
    (repo / "src" / "lib.rs").write_text("pub mod config;\n")
    (repo / "src" / "config.rs").write_text(base)
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "branch", "feat"); _git(repo, "checkout", "-q", "feat")
    (repo / "src" / "config.rs").write_text(replayed); _git(repo, "add", "src/config.rs"); _git(repo, "commit", "-q", "-m", "rep")
    _git(repo, "checkout", "-q", "main")
    (repo / "src" / "config.rs").write_text(upstream); _git(repo, "add", "src/config.rs"); _git(repo, "commit", "-q", "-m", "up")
    _git(repo, "checkout", "-q", "feat")
    r = _git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected conflict"
    # Correct: retries=5 (upstream) AND timeout_ms field present (replayed).
    return Scenario("rust_impl", "rust", repo, "src/config.rs",
                    expect_substrings=["max_retries: 5", "pub timeout_ms: u32", "timeout_ms: 10000", "retries={"],
                    reject_substrings=["<<<<<<<", "=======", ">>>>>>>", "max_retries: 3,"],
                    cargo=True)


def scenario_rust_port_test() -> Scenario:
    """Rust: default port conflict where a test asserts port == 9090 (upstream)."""
    repo = _mk_repo()
    base = (
        "pub struct Config {\n    pub port: u16,\n}\n"
        "impl Config {\n    pub fn new() -> Self { Config { port: 8080 } }\n}\n\n"
        "#[cfg(test)]\nmod tests {\n    use super::*;\n    #[test]\n    fn port_is_9090() {\n"
        "        let c = Config::new();\n        assert_eq!(c.port, 9090);\n    }\n}\n"
    )
    upstream = base.replace("port: 8080 }", "port: 9090 }")
    replayed = base.replace("port: 8080 }", "port: 7070 }")

    (repo / "Cargo.toml").write_text('[package]\nname = "testgated"\nversion = "0.1.0"\nedition = "2021"\n')
    (repo / "src").mkdir()
    (repo / "src" / "lib.rs").write_text(base)
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "branch", "feat"); _git(repo, "checkout", "-q", "feat")
    (repo / "src" / "lib.rs").write_text(replayed); _git(repo, "add", "src/lib.rs"); _git(repo, "commit", "-q", "-m", "rep")
    _git(repo, "checkout", "-q", "main")
    (repo / "src" / "lib.rs").write_text(upstream); _git(repo, "add", "src/lib.rs"); _git(repo, "commit", "-q", "-m", "up")
    _git(repo, "checkout", "-q", "feat")
    r = _git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected conflict"
    return Scenario("rust_port_test", "rust", repo, "src/lib.rs",
                    expect_substrings=["port: 9090 }"],
                    reject_substrings=["<<<<<<<", "=======", ">>>>>>>", "port: 7070 }", "port: 8080 }"],
                    cargo=True)


# ---------------------------------------------------------------------------
# run harness
# ---------------------------------------------------------------------------

def _config_for(scenario: Scenario, *, critic_enabled: bool = True) -> Config:
    cfg = Config()
    # Use the live model endpoint from capybase.toml defaults (DESKTOP-NOVA chat).
    cfg.model.base_url = os.environ.get("CAPYBASE_BASE_URL", "http://DESKTOP-NOVA.local:8085/v1")
    cfg.model.api_key = os.environ.get("CAPYBASE_API_KEY", "sk-local")
    cfg.model.model = os.environ.get("CAPYBASE_MODEL", "chat")
    cfg.model.temperature = 0.2
    cfg.model.max_tokens = 8192  # VibeThinker-3B needs headroom for its <think> chain
    cfg.model.json_mode = True
    cfg.model.request_timeout_seconds = 600
    cfg.model.generation_timeout_seconds = 240
    # Tests gate: real cargo test (Rust) or a per-file compile check (Python).
    # For Rust the cargo check/test IS part of correctness (port must be 9090 for
    # the test to pass). For Python the scenarios have no test suite, so compile
    # the resolved file as the syntax floor — py_compile needs a filename arg the
    # orchestrator doesn't append, so bake the (repo-relative) path in here.
    # NOTE: use `python3` (not `python`) — the eval host only has python3 on PATH.
    if scenario.cargo:
        cfg.tests.pre_continue = "cargo test"
    else:
        cfg.tests.pre_continue = f"python3 -m py_compile {scenario.path}"
    cfg.tests.final = cfg.tests.pre_continue
    cfg.tests.required = True
    cfg.tests.timeout_seconds = 300
    # Structural resolver + combination search: keep ON (production defaults).
    cfg.future.enable_structural_resolver = True
    cfg.future.enable_combination_search = True
    # Verifier-model critic A/B arm. Default ON (the production default); the
    # A/B harness toggles this to measure the critic's contribution.
    cfg.validation.enable_verifier_model = critic_enabled
    cfg.validation.verifier_severity = "warning"
    return cfg


@dataclass
class Result:
    name: str
    correct: bool
    escalated: bool
    reason: str
    elapsed: float
    final_content_preview: str
    journal_events: list[str]
    # Critic-utility stats (collected from stored validations + journal). These
    # measure whether the critic ADDS value beyond the deterministic validators.
    critic_arm: str = ""            # "on" | "off"
    critic_calls: int = 0           # verdicts the critic actually produced
    critic_flagged: int = 0         # verdicts where preserves_* was False (dropped intent)
    critic_useful_interventions: int = 0  # critic flagged a candidate the syntactic checks PASSED
    # Outline-prompt variant (small-model experiment): which outline framing was
    # active, or "" for the baseline prompt. Set from CAPYBASE_PROMPT_VARIANT.
    prompt_variant: str = ""


def _critic_stats(orch) -> tuple[int, int, int]:
    """Mine the orchestrator's stored validations + journal for critic-utility stats.

    Returns (calls, flagged, useful_interventions):
    - calls: candidates where verifier_checked=True (the critic produced a verdict)
    - flagged: candidates where the critic said a side's intent was dropped
    - useful_interventions: critic-flagged candidates that PASSED every OTHER
      validator (hard_failures empty) — i.e. the critic caught something the
      syntactic checks were blind to. This is the critic's discriminating value.
    """
    calls = flagged = useful = 0
    # Stored per-candidate validations (richer than the journal's hard_failures).
    try:
        vdir = orch.paths.validations if hasattr(orch.paths, "validations") else None
        if vdir and Path(vdir).is_dir():
            for vf in sorted(Path(vdir).glob("*.json")):
                if vf.name.endswith("-file.json"):
                    continue  # whole-file validation, not a critic-bearing candidate
                try:
                    d = json.loads(vf.read_text())
                except Exception:
                    continue
                feats = d.get("features", {}) or {}
                if feats.get("verifier_checked") is not True:
                    continue
                calls += 1
                dropped = (
                    feats.get("verifier_preserves_current") is False
                    or feats.get("verifier_preserves_replayed") is False
                )
                if not dropped:
                    continue
                flagged += 1
                # Useful iff the critic was the ONLY thing wrong: no hard failures
                # from any other validator on this candidate.
                if not d.get("hard_failures"):
                    useful += 1
    except Exception:
        pass
    return calls, flagged, useful


def run_scenario(builder, out_dir: Path, *, critic_enabled: bool = True) -> Result:
    scenario = builder()
    arm = "on" if critic_enabled else "off"
    # Outline-prompt variant (small-model experiment): select it on the
    # resolution_engine before building the engine, so every fresh-resolve
    # candidate uses the chosen framing. The variant tag is surfaced in results.
    variant = os.environ.get("CAPYBASE_PROMPT_VARIANT", "").strip()
    variant_n: int | None = None
    if variant:
        try:
            variant_n = int(variant)
        except ValueError:
            variant_n = None
    from capybase.resolution_engine import set_outline_variant
    set_outline_variant(variant_n)
    tag = f"v{variant_n}" if variant_n else "baseline"
    os.environ["CAPYBASE_PROMPT_VARIANT_TAG"] = tag
    print(f"\n=== {scenario.name} ({scenario.language}) [critic={arm}] [prompt={tag}] ===",
          flush=True)
    t0 = time.time()
    cfg = _config_for(scenario, critic_enabled=critic_enabled)
    engine = ResolutionEngine(cfg.model, client=OpenAICompatibleClient(cfg.model))
    # Suppress console color noise; route prints to /dev/null to keep timing clean.
    orch = Orchestrator(cfg, repo=str(scenario.repo),
                        resolution_engine=engine, out=lambda _m: None)

    escalated = False
    reason = ""
    try:
        res = orch.run()
        escalated = res.escalated
        reason = res.reason or ""
    except Exception as e:
        escalated = True
        reason = f"EXCEPTION: {type(e).__name__}: {e}"
    elapsed = time.time() - t0

    # Read the final file content (after rebase either continued or was aborted).
    final_path = scenario.repo / scenario.path
    if final_path.exists():
        content = final_path.read_text()
    else:
        content = ""

    expect_ok = all(s in content for s in scenario.expect_substrings)
    # For value-resolution conflicts: at least one of the divergent values (or a
    # combination) must appear. Empty list = not applicable.
    if scenario.expect_any_substrings:
        expect_ok = expect_ok and any(
            s in content for s in scenario.expect_any_substrings
        )
    reject_ok = not any(s in content for s in scenario.reject_substrings)
    correct = expect_ok and reject_ok and not escalated

    # If rebase was aborted (escalation), content may still hold markers from
    # the pre-abort working tree; re-check by reading the committed result on
    # the feature branch if a backup exists. Simplest: correctness = file has
    # no markers AND contains expected substrings, regardless of escalation.
    if not escalated:
        correct = expect_ok and reject_ok
    else:
        # Escalated → did NOT auto-resolve correctly (by definition).
        correct = False

    preview = content[:400].replace("\n", "\\n")

    # Collect journal event types for diagnostics.
    events = []
    try:
        jpath = orch.paths.journal if hasattr(orch.paths, "journal") else None
        if jpath and Path(jpath).exists():
            with open(jpath) as f:
                for line in f:
                    try:
                        ev = json.loads(line).get("event_type", "")
                        if ev:
                            events.append(ev)
                    except Exception:
                        pass
    except Exception:
        pass

    status = "PASS" if correct else ("ESCALATED" if escalated else "WRONG_MERGE")
    c_calls, c_flagged, c_useful = _critic_stats(orch) if critic_enabled else (0, 0, 0)
    print(f"  -> {status}  ({elapsed:.1f}s)  escalated={escalated}", flush=True)
    if escalated:
        print(f"     reason: {reason}", flush=True)
    if not correct:
        print(f"     expect_ok={expect_ok} reject_ok={reject_ok}", flush=True)
        print(f"     preview: {preview}", flush=True)
    if critic_enabled and c_calls:
        print(f"     critic: {c_calls} verdict(s), {c_flagged} flagged, "
              f"{c_useful} useful (beyond syntactic checks)", flush=True)

    return Result(scenario.name, correct, escalated, reason, elapsed, preview, events,
                  critic_arm=arm, critic_calls=c_calls, critic_flagged=c_flagged,
                  critic_useful_interventions=c_useful,
                  prompt_variant=os.environ.get("CAPYBASE_PROMPT_VARIANT_TAG", ""))


def _probe_endpoint() -> bool:
    print("Probing model endpoint...", flush=True)
    target_model = os.environ.get("CAPYBASE_MODEL", "chat")
    try:
        import urllib.request
        resp = urllib.request.urlopen(
            os.environ.get("CAPYBASE_BASE_URL", "http://DESKTOP-NOVA.local:8085/v1") + "/models",
            timeout=15,
        )
        models = json.loads(resp.read())
        all_ids = [m["id"] for m in models.get("data", [])]
        # Some servers (the DESKTOP-NOVA llama.cpp build) report a per-model
        # ``status.value == "loaded"``; others (LM Studio) just list available
        # models with no status field. When statuses are reported, require the
        # target to be loaded; otherwise settle for it being listed.
        with_status = [m for m in models.get("data", []) if m.get("status")]
        if with_status:
            loaded = [m["id"] for m in with_status if m.get("status", {}).get("value") == "loaded"]
            ok = target_model in loaded
            print(f"  reachable. loaded models: {loaded}", flush=True)
        else:
            ok = target_model in all_ids
            print(f"  reachable. available models: {all_ids}", flush=True)
        if not ok:
            print(
                f"  WARNING: target model '{target_model}' not available — eval "
                f"will fail.", flush=True
            )
        return ok
    except Exception as e:
        print(f"  UNREACHABLE: {e}", flush=True)
        return False


def _selected_builders() -> list:
    all_builders = [
        scenario_py_simple,
        scenario_py_multi_unit,
        scenario_rust_impl,
        scenario_rust_port_test,
    ]
    only = os.environ.get("CAPYBASE_LIVE_ONLY", "").split(",")
    only = [o.strip() for o in only if o.strip()]
    return [b for b in all_builders if not only or b.__name__.replace("scenario_", "") in only]


def main() -> int:
    builders = _selected_builders()
    if not _probe_endpoint():
        return 2

    # A/B mode: CAPYBASE_AB_RUNS=N runs each scenario N times with critic ON and
    # N times with critic OFF, then compares. Default (unset/0/1) is the plain
    # single-run-per-scenario path (critic ON — the production default).
    try:
        ab_runs = max(1, int(os.environ.get("CAPYBASE_AB_RUNS", "1")))
    except ValueError:
        ab_runs = 1
    ab_mode = ab_runs > 1

    results: list[Result] = []
    if not ab_mode:
        for b in builders:
            try:
                results.append(run_scenario(b, Path("/tmp/capybase-live")))
            except Exception as e:
                print(f"  SCENARIO SETUP FAILED: {type(e).__name__}: {e}", flush=True)
                results.append(Result(getattr(b, "__name__", "?"), False, True,
                                      f"setup error: {e}", 0.0, "", []))
        _print_single_summary(results)
        _dump_results(results)
        n_wrong = sum(1 for r in results if not r.correct and not r.escalated)
        return 0 if n_wrong == 0 else 1

    # --- A/B multi-run: critic ON vs OFF, N runs each, interleaved ---
    print(f"\n*** A/B MODE: {ab_runs} run(s) per scenario per arm "
          f"(critic ON vs OFF) ***", flush=True)
    for b in builders:
        name = b.__name__.replace("scenario_", "")
        for i in range(ab_runs):
            for arm in (True, False):  # alternate arms to spread load/time variance
                tag = "on" if arm else "off"
                print(f"\n##### {name} run {i+1}/{ab_runs} critic={tag} #####", flush=True)
                try:
                    results.append(run_scenario(b, Path("/tmp/capybase-live"), critic_enabled=arm))
                except Exception as e:
                    print(f"  RUN FAILED: {type(e).__name__}: {e}", flush=True)
                    results.append(Result(name, False, True, f"run error: {e}",
                                          0.0, "", [], critic_arm=tag))
    _print_ab_summary(results, ab_runs)
    _dump_results(results)
    return 0


def _print_single_summary(results: list[Result]) -> None:
    print("\n" + "=" * 64, flush=True)
    print("LIVE EVAL SUMMARY", flush=True)
    print("=" * 64, flush=True)
    n_pass = sum(1 for r in results if r.correct)
    n_escal = sum(1 for r in results if r.escalated and not r.correct)
    n_wrong = sum(1 for r in results if not r.correct and not r.escalated)
    print(f"{'scenario':<20} {'result':<12} {'time':>6}  detail")
    print("-" * 64)
    for r in results:
        tag = "PASS" if r.correct else ("ESCALATED" if r.escalated else "WRONG")
        detail = r.reason[:40] if r.escalated else ""
        print(f"{r.name:<20} {tag:<12} {r.elapsed:>5.1f}s  {detail}")
    print("-" * 64)
    print(f"correct: {n_pass}/{len(results)}   escalated: {n_escal}   wrong-merge: {n_wrong}")


def _print_ab_summary(results: list[Result], runs: int) -> None:
    """Compare critic ON vs OFF across the multi-run results.

    The model is non-deterministic (temp 0.2), so per-run correctness varies;
    the A/B aggregates success RATE per arm to separate the critic's signal from
    variance. Also reports the critic-utility stats: how often the critic flagged
    a candidate the syntactic checks passed (its discriminating value), and
    whether those interventions converted a would-be-wrong-merge into a success.
    """
    print("\n" + "=" * 72, flush=True)
    print("A/B SUMMARY: critic ON vs OFF", flush=True)
    print("=" * 72, flush=True)
    # Group by (scenario, arm).
    from collections import defaultdict
    by: dict[tuple[str, str], list[Result]] = defaultdict(list)
    for r in results:
        by[(r.name, r.critic_arm or "on")].append(r)
    scenarios = sorted({r.name for r in results})
    print(f"{'scenario':<20} {'arm':<5} {'pass':>5} {'esc':>5} {'wrong':>6} {'avg_s':>6}  critic stats")
    print("-" * 72)
    for sc in scenarios:
        for arm in ("on", "off"):
            rs = by.get((sc, arm), [])
            if not rs:
                continue
            n_pass = sum(1 for r in rs if r.correct)
            n_esc = sum(1 for r in rs if r.escalated and not r.correct)
            n_wrong = sum(1 for r in rs if not r.correct and not r.escalated)
            avg = sum(r.elapsed for r in rs) / len(rs)
            calls = sum(r.critic_calls for r in rs)
            flagged = sum(r.critic_flagged for r in rs)
            useful = sum(r.critic_useful_interventions for r in rs)
            cstats = (f"{calls}v/{flagged}f/{useful}u" if arm == "on" else "(disabled)")
            print(f"{sc:<20} {arm:<5} {n_pass:>5} {n_esc:>5} {n_wrong:>6} {avg:>5.1f}s  {cstats}")
    # Totals per arm.
    print("-" * 72)
    for arm in ("on", "off"):
        rs = [r for r in results if (r.critic_arm or "on") == arm]
        if not rs:
            continue
        n_pass = sum(1 for r in rs if r.correct)
        n_wrong = sum(1 for r in rs if not r.correct and not r.escalated)
        rate = n_pass / len(rs) * 100 if rs else 0
        print(f"  arm={arm:<4}: {n_pass}/{len(rs)} correct ({rate:.0f}%), "
              f"{n_wrong} wrong-merge(s)")
    print("\nlegend: pass=success esc=escalated(safe) wrong=staged-bad-merge  "
          "critic: v=verdicts f=flagged u=useful(beyond syntactic checks)")


def _dump_results(results: list[Result]) -> None:
    out = Path("/tmp/capybase-live/results.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        [{k: v for k, v in r.__dict__.items() if k != "journal_events"} | {"journal_events": r.journal_events}
         for r in results], indent=2))
    print(f"\nfull results: {out}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
