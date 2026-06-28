"""End-to-end tests for the ``capybase rebase`` command — the entry point that
owns the entire rebase process.

Unlike ``run`` (which assumes the user already started the rebase and stopped on
a conflict), ``rebase`` starts the rebase itself, drives the existing
resolve → test → continue loop, and (by default) aborts on escalation so the
repo returns to its original HEAD. These tests exercise that start→run→finish
(or start→escalate→abort) lifecycle against real temp git repos with a fake LLM.

The fixtures (``py_repo_before_rebase``, ``py_repo_clean_rebase``) leave the
repo on the feature branch with a clean worktree, BEFORE any rebase — so capybase
owns the start. The Rust test builds its own before-rebase crate inline.
"""

from __future__ import annotations

import json
import shutil

import pytest

from capybase.adapters.llm_openai import LLMResponse
from capybase.config import Config
from capybase.git_backend import GitError
from capybase.orchestrator import Orchestrator
from capybase.resolution_engine import ResolutionEngine

from tests.conftest import git

rustc = shutil.which("rustc")
skip_no_rustc = pytest.mark.skipif(rustc is None, reason="rustc not installed")


# ---------------------------------------------------------------------------
# Test helpers (mirror test_orchestrator.py / test_rust_end_to_end.py).
# ---------------------------------------------------------------------------


class CyclingClient:
    """Returns canned responses in order, then repeats the last."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        if len(self.responses) > 1:
            return LLMResponse(text=self.responses.pop(0))
        return LLMResponse(text=self.responses[0])


class FailingClient:
    """Always returns a leaked-marker resolution → the verifier hard-rejects it,
    exhausting the retry budget and forcing an escalation."""

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        return LLMResponse(
            text=json.dumps({"resolved_text": "    x\n<<<<<<< still\n"})
        )


def _config(repo, *, tests_required: bool = True) -> Config:
    """A config with the test gate set to `true` (always exits 0)."""
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = tests_required
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    return cfg


def _payload(text: str) -> str:
    return json.dumps(
        {"resolved_text": text, "explanation": "merge", "self_reported_confidence": 0.8}
    )


def _journal_events(orch: Orchestrator) -> list[dict]:
    """Read the session journal as a list of {event_type, payload} dicts."""
    events = []
    for line in orch.paths.journal.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            d = json.loads(line)
            events.append({"event_type": d["event_type"], "payload": d.get("payload", {})})
    return events


# ---------------------------------------------------------------------------
# Happy path: rebase starts, conflict resolves, rebase finishes.
# ---------------------------------------------------------------------------


def test_rebase_resolves_and_finishes(py_repo_before_rebase):
    """``rebase`` starts the rebase, resolves the conflict, and finishes."""
    repo = py_repo_before_rebase["repo"]
    start_head = git(repo, "rev-parse", "HEAD").stdout.strip()
    merged_block = py_repo_before_rebase["merged_block"]
    engine = ResolutionEngine(
        _config(repo).model, client=CyclingClient([_payload(merged_block)])
    )
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.rebase("main")
    # Rebase completed cleanly — no escalation.
    assert not result.escalated, result.reason
    # No conflict markers in the resolved file.
    assert "<<<<<<<" not in (repo / "app.py").read_text()
    # Rebase is no longer in progress.
    assert not _rebase_in_progress(repo)
    # HEAD advanced (rebased onto main), not the original feat tip.
    assert git(repo, "rev-parse", "HEAD").stdout.strip() != start_head
    # The session completed event was journaled.
    types = [e["event_type"] for e in _journal_events(orch)]
    assert "rebase_requested" in types
    assert "rebase_started" in types
    assert "session_completed" in types
    # A user-visible backup branch was created at the pre-rebase HEAD, so the
    # result can be rolled back. It carries the pre-rebase OID. (The journal
    # stores the full refname; list_backup_refs returns short names.)
    backups = orch.git.list_backup_refs()
    assert backups, "expected a backup branch after rebase"
    started = next(e for e in _journal_events(orch) if e["event_type"] == "rebase_started")
    backup_short = started["payload"]["backup_ref"][len("refs/heads/"):]
    assert backup_short in backups
    assert git(repo, "rev-parse", backups[0]).stdout.strip() == start_head


def test_rebase_clean_no_conflict(py_repo_clean_rebase):
    """A rebase with no conflict starts and finishes immediately."""
    repo = py_repo_clean_rebase
    start_head = git(repo, "rev-parse", "HEAD").stdout.strip()
    # No client needed — the rebase never hits a conflict, so the LLM is never
    # called. A failing client would surface a "never called" only if a conflict
    # appeared, which it must not.
    engine = ResolutionEngine(_config(repo).model, client=FailingClient())
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.rebase("main")
    assert not result.escalated, result.reason
    assert not _rebase_in_progress(repo)
    # feat's commits are now rebased onto main: HEAD advanced.
    assert git(repo, "rev-parse", "HEAD").stdout.strip() != start_head
    log = git(repo, "log", "--oneline").stdout
    assert "add b" in log and "add c" in log


# ---------------------------------------------------------------------------
# Escalation + abort-on-escalation (the default).
# ---------------------------------------------------------------------------


def test_rebase_aborts_on_escalation(py_repo_before_rebase):
    """On escalation with the default ``abort_on_escalation``, the rebase is
    aborted and the repo returns to its original HEAD."""
    repo = py_repo_before_rebase["repo"]
    start_head = git(repo, "rev-parse", "HEAD").stdout.strip()
    engine = ResolutionEngine(_config(repo).model, client=FailingClient())
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.rebase("main")
    # The conflict couldn't be resolved → escalated.
    assert result.escalated
    # abort_on_escalation is the default: the rebase was rolled back.
    assert not _rebase_in_progress(repo)
    # The repo is back at its original HEAD.
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == start_head
    # The abort was journaled.
    types = [e["event_type"] for e in _journal_events(orch)]
    assert "rebase_aborted" in types
    # The backup branch still exists (not auto-deleted) and points at the
    # pre-rebase HEAD, so the user can reset to it.
    aborted = next(e for e in _journal_events(orch) if e["event_type"] == "rebase_aborted")
    backup_short = aborted["payload"]["backup_ref"][len("refs/heads/"):]
    assert backup_short in orch.git.list_backup_refs()
    assert git(repo, "rev-parse", backup_short).stdout.strip() == start_head


def test_rebase_no_abort_preserves_stop(py_repo_before_rebase):
    """With ``abort_on_escalation=False``, the rebase is left stopped."""
    repo = py_repo_before_rebase["repo"]
    start_head = git(repo, "rev-parse", "HEAD").stdout.strip()
    engine = ResolutionEngine(_config(repo).model, client=FailingClient())
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.rebase("main", abort_on_escalation=False)
    assert result.escalated
    # The rebase is still in progress (left stopped at the conflict).
    assert _rebase_in_progress(repo)
    # HEAD is NOT the original (the rebase moved it before stopping).
    assert git(repo, "rev-parse", "HEAD").stdout.strip() != start_head
    # Conflict markers are still in the worktree.
    assert "<<<<<<<" in (repo / "app.py").read_text()
    # A review bundle was written for manual inspection.
    assert (orch.paths.final / "review-bundle.md").exists()
    # No abort event.
    types = [e["event_type"] for e in _journal_events(orch)]
    assert "rebase_aborted" not in types
    # Clean up the stopped rebase so the tmp repo is tidy.
    git(repo, "rebase", "--abort", check=False)


# ---------------------------------------------------------------------------
# Interruption safety: a SIGTERM mid-rebase aborts cleanly (no orphaned rebase).
# ---------------------------------------------------------------------------


def test_rebase_interrupt_aborts_cleanly(py_repo_before_rebase):
    """An interruption during run() aborts the in-progress rebase and returns
    the repo to its start, rather than leaving a stopped rebase behind.

    The interrupt is a RuntimeError raised from run() itself (as the SIGTERM
    handler produces — it raises between bytecodes in the main thread, NOT from
    inside complete(), so the resolution engine's request-failure catch doesn't
    swallow it). We monkeypatch run() to raise, exercising the real
    abort-on-interrupt try/except.
    """
    repo = py_repo_before_rebase["repo"]
    start_head = git(repo, "rev-parse", "HEAD").stdout.strip()
    engine = ResolutionEngine(_config(repo).model, client=CyclingClient([]))
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    # Force run() to raise mid-rebase, as the SIGTERM handler would (Interrupted
    # is a BaseException so the LLM retry wrapper can't swallow it).
    def boom():
        from capybase.adapters.llm_openai import Interrupted
        raise Interrupted("capybase interrupted by signal 15")
    orch.run = boom  # type: ignore[method-assign]
    # The interrupt propagates; the rebase path catches it, aborts the
    # in-progress rebase, journals the abort, and re-raises. Interrupted is a
    # BaseException, so assert on BaseException.
    with pytest.raises(BaseException, match="interrupted by signal"):
        orch.rebase("main")
    # CRITICAL: the rebase was aborted, not left stopped.
    assert not _rebase_in_progress(repo), "rebase should have been aborted on interrupt"
    # The repo is back at its original HEAD (the abort rolled back).
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == start_head
    # The abort was journaled.
    types = [e["event_type"] for e in _journal_events(orch)]
    assert "rebase_aborted" in types
    aborted = next(e for e in _journal_events(orch) if e["event_type"] == "rebase_aborted")
    assert "interrupted" in aborted["payload"]["reason"]


def test_rebase_sigterm_handler_converts_signal(py_repo_before_rebase, monkeypatch):
    """The SIGTERM handler installed by rebase() converts the signal into a
    RuntimeError that flows to the abort path. Verified by capturing the handler
    registration rather than timing a real signal (racy with the fast fake)."""
    import signal as _sig

    repo = py_repo_before_rebase["repo"]
    installed: list[object] = []
    real_signal = _sig.signal

    def spy_signal(sig, handler):
        # Store the handler ARGUMENT (the callable we installed), not the
        # return value (Python 3.14 returns a Handlers enum, not callable).
        if int(sig) in (int(_sig.SIGTERM), int(getattr(_sig, "SIGHUP", _sig.SIGTERM))):
            installed.append(handler)
        return real_signal(sig, handler)

    monkeypatch.setattr(_sig, "signal", spy_signal)
    # run() raises so the handler is observed before rebase() restores defaults.
    engine = ResolutionEngine(_config(repo).model, client=CyclingClient([]))
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    orch.run = lambda: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        orch.rebase("main")
    assert installed, "rebase() must install a SIGTERM/SIGHUP handler"
    # The handler raises Interrupted (a BaseException) — converts the signal to
    # an exception that the LLM retry wrapper can't swallow.
    from capybase.adapters.llm_openai import Interrupted
    with pytest.raises(Interrupted, match="interrupted by signal"):
        installed[0](_sig.SIGTERM, None)


# ---------------------------------------------------------------------------
# Worktree preflight + autostash.
# ---------------------------------------------------------------------------


def test_rebase_refuses_dirty_worktree(py_repo_before_rebase):
    """A dirty worktree (no --autostash) raises GitError before any rebase."""
    repo = py_repo_before_rebase["repo"]
    # Introduce an uncommitted change.
    (repo / "app.py").write_text("def greet():\n    return 'dirty'\n")
    start_head = git(repo, "rev-parse", "HEAD").stdout.strip()
    engine = ResolutionEngine(_config(repo).model, client=CyclingClient([]))
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    with pytest.raises(GitError, match="working tree is dirty"):
        orch.rebase("main")
    # Nothing happened: no rebase started, HEAD unchanged.
    assert not _rebase_in_progress(repo)
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == start_head


def test_rebase_autostash_allows_dirty(py_repo_before_rebase):
    """--autostash stashes dirty changes, proceeds, and re-applies them."""
    repo = py_repo_before_rebase["repo"]
    merged_block = py_repo_before_rebase["merged_block"]
    # Dirty change: an untracked extra file (autostash handles tracked + untracked
    # via stash --include-untracked when git rebase --autostash runs). Use a
    # tracked modification so autostash definitely engages.
    (repo / "extra.txt").write_text("uncommitted\n")
    engine = ResolutionEngine(
        _config(repo).model, client=CyclingClient([_payload(merged_block)])
    )
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.rebase("main", autostash=True)
    # The rebase proceeded and resolved.
    assert not result.escalated, result.reason
    assert not _rebase_in_progress(repo)
    # The dirty change was re-applied after the rebase.
    assert (repo / "extra.txt").exists()


# ---------------------------------------------------------------------------
# Recovery ref + journal provenance.
# ---------------------------------------------------------------------------


def test_rebase_records_start_ref(py_repo_before_rebase):
    """The pre-rebase HEAD is recorded as a recovery ref before the rebase."""
    repo = py_repo_before_rebase["repo"]
    start_head = git(repo, "rev-parse", "HEAD").stdout.strip()
    merged_block = py_repo_before_rebase["merged_block"]
    engine = ResolutionEngine(
        _config(repo).model, client=CyclingClient([_payload(merged_block)])
    )
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    orch.rebase("main")
    ref = f"refs/rebase-agent/{orch.session_id}/start"
    ref_oid = git(repo, "rev-parse", "--verify", "--quiet", ref).stdout.strip()
    assert ref_oid == start_head, "start ref must point at the pre-rebase HEAD"


def test_rebase_start_failure_raises(py_repo_clean_rebase):
    """A bad rebase target raises GitError (not a silent escalation)."""
    repo = py_repo_clean_rebase
    engine = ResolutionEngine(_config(repo).model, client=FailingClient())
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    # A nonexistent branch: git rebase fails immediately (no conflict stop).
    with pytest.raises(GitError, match="rebase"):
        orch.rebase("does-not-exist")
    # No rebase was left in progress.
    assert not _rebase_in_progress(repo)


# ---------------------------------------------------------------------------
# Rust: the real-crate compile-checked path, owned start-to-finish by rebase.
# ---------------------------------------------------------------------------


@skip_no_rustc
def test_rebase_rust_resolves_and_compiles(repo):
    """A Rust crate rebase: capybase starts it, resolves, compiles, finishes.

    Builds a two-hunk ``impl Config`` conflict (mirrors ``rust_conflicted_repo``
    but stopped before the rebase) so capybase owns the start. The correct merge
    must compile under the Phase-B rustc floor to be accepted.
    """
    base = (
        "pub struct Config {\n"
        '    pub name: String,\n'
        "    pub max_retries: u32,\n"
        "}\n"
        "\n"
        "impl Config {\n"
        "    pub fn new() -> Self {\n"
        "        Config {\n"
        '            name: "capybase".to_string(),\n'
        "            max_retries: 3,\n"
        "        }\n"
        "    }\n"
        "\n"
        "    pub fn label(&self) -> String {\n"
        '        format!("{} (retries={})", self.name, self.max_retries)\n'
        "    }\n"
        "}\n"
    )
    # Upstream (CURRENT): bump retries to 5, bracket the name in label().
    upstream = base.replace("max_retries: 3,", "max_retries: 5,").replace(
        'format!("{} (retries={})"', 'format!("[{}] retries={}"'
    )
    # Replayed: add timeout_ms field (struct + init + label format).
    replayed = (
        "pub struct Config {\n"
        '    pub name: String,\n'
        "    pub max_retries: u32,\n"
        "    pub timeout_ms: u32,\n"
        "}\n"
        "\n"
        "impl Config {\n"
        "    pub fn new() -> Self {\n"
        "        Config {\n"
        '            name: "capybase".to_string(),\n'
        "            max_retries: 3,\n"
        "            timeout_ms: 10000,\n"
        "        }\n"
        "    }\n"
        "\n"
        "    pub fn label(&self) -> String {\n"
        '        format!("{} (retries={}, timeout={})", self.name, '
        "self.max_retries, self.timeout_ms)\n"
        "    }\n"
        "}\n"
    )

    (repo / "src").mkdir()
    (repo / "src" / "config.rs").write_text(base)
    git(repo, "add", "src/config.rs")
    git(repo, "commit", "-q", "-m", "base")

    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    (repo / "src" / "config.rs").write_text(replayed)
    git(repo, "add", "src/config.rs")
    git(repo, "commit", "-q", "-m", "replayed: add timeout_ms")

    git(repo, "checkout", "-q", "main")
    (repo / "src" / "config.rs").write_text(upstream)
    git(repo, "add", "src/config.rs")
    git(repo, "commit", "-q", "-m", "upstream: raise retries")

    git(repo, "checkout", "-q", "feat")

    # The two block-interior merges (hunk order: new() then label()).
    r_new = (
        "            max_retries: 5,\n"
        "            timeout_ms: 10000,"
    )
    r_label = (
        '        format!("[{}] (retries={}, timeout={})", self.name, '
        "self.max_retries, self.timeout_ms)"
    )
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = None
    cfg.tests.final = None
    engine = ResolutionEngine(
        cfg.model, client=CyclingClient([_payload(r_new), _payload(r_label)])
    )
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.rebase("main")
    assert not result.escalated, result.reason
    text = (repo / "src" / "config.rs").read_text()
    assert "<<<<<<<" not in text
    # Semantic correctness: both sides' intent preserved.
    assert "max_retries: 5" in text            # upstream's value
    assert "timeout_ms: 10000" in text         # replayed's field + init
    assert "[{}] (retries={}, timeout={})" in text  # combined format string
    assert not _rebase_in_progress(repo)


# ---------------------------------------------------------------------------
# Utility.
# ---------------------------------------------------------------------------


def _rebase_in_progress(repo) -> bool:
    """True if a rebase is currently in progress in ``repo``."""
    r = git(repo, "rev-parse", "--git-path", "rebase-merge")
    if r.returncode == 0 and (repo / r.stdout.strip()).exists():
        return True
    r = git(repo, "rev-parse", "--git-path", "rebase-apply")
    return r.returncode == 0 and (repo / r.stdout.strip()).exists()
