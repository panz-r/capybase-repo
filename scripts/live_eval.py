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
    # Correct merge preserves BOTH intents. 'hi' and 'howdy' both present.
    return Scenario("py_simple", "python", repo, "app.py",
                    expect_substrings=["def greet():", "'hi'", "'howdy'"],
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

def _config_for(scenario: Scenario) -> Config:
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
    # Tests gate: real pytest/cargo. For Rust scenarios the cargo check/test
    # IS part of correctness (port must be 9090 for the test to pass).
    # NOTE: use `python3` (not `python`) — the eval host only has python3 on PATH.
    cfg.tests.pre_continue = "cargo test" if scenario.cargo else "python3 -m py_compile"
    cfg.tests.final = cfg.tests.pre_continue
    cfg.tests.required = True
    cfg.tests.timeout_seconds = 300
    # Structural resolver + combination search: keep ON (production defaults).
    cfg.future.enable_structural_resolver = True
    cfg.future.enable_combination_search = True
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


def run_scenario(builder, out_dir: Path) -> Result:
    scenario = builder()
    print(f"\n=== {scenario.name} ({scenario.language}) ===", flush=True)
    t0 = time.time()
    cfg = _config_for(scenario)
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
    print(f"  -> {status}  ({elapsed:.1f}s)  escalated={escalated}", flush=True)
    if escalated:
        print(f"     reason: {reason}", flush=True)
    if not correct:
        print(f"     expect_ok={expect_ok} reject_ok={reject_ok}", flush=True)
        print(f"     preview: {preview}", flush=True)

    return Result(scenario.name, correct, escalated, reason, elapsed, preview, events)


def main() -> int:
    all_builders = [
        scenario_py_simple,
        scenario_py_multi_unit,
        scenario_rust_impl,
        scenario_rust_port_test,
    ]
    # --only <name[,name...]> filters which scenarios run (for re-running just
    # the slow Rust ones without re-paying the fast Python ones).
    only = os.environ.get("CAPYBASE_LIVE_ONLY", "").split(",")
    only = [o.strip() for o in only if o.strip()]
    builders = [b for b in all_builders if not only or b.__name__.replace("scenario_", "") in only]
    # Smoke-test reachability first.
    print("Probing model endpoint...", flush=True)
    try:
        import urllib.request
        resp = urllib.request.urlopen(
            os.environ.get("CAPYBASE_BASE_URL", "http://DESKTOP-NOVA.local:8085/v1") + "/models",
            timeout=15,
        )
        models = json.loads(resp.read())
        loaded = [m["id"] for m in models.get("data", []) if m.get("status", {}).get("value") == "loaded"]
        print(f"  reachable. loaded models: {loaded}", flush=True)
        if "chat" not in loaded:
            print("  WARNING: 'chat' model not loaded — eval will fail.", flush=True)
    except Exception as e:
        print(f"  UNREACHABLE: {e}", flush=True)
        return 2

    results: list[Result] = []
    for b in builders:
        try:
            results.append(run_scenario(b, Path("/tmp/capybase-live")))
        except Exception as e:
            print(f"  SCENARIO SETUP FAILED: {type(e).__name__}: {e}", flush=True)
            results.append(Result(getattr(b, "__name__", "?"), False, True,
                                  f"setup error: {e}", 0.0, "", []))

    # Summary.
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

    # Dump full results JSON for the report.
    out = Path("/tmp/capybase-live/results.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        [{k: v for k, v in r.__dict__.items() if k != "journal_events"} | {"journal_events": r.journal_events}
         for r in results], indent=2))
    print(f"\nfull results: {out}")
    return 0 if n_wrong == 0 else 1  # escalated is acceptable (safe failure); wrong-merge is a bug


if __name__ == "__main__":
    raise SystemExit(main())
