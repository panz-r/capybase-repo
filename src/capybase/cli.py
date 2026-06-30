"""Command-line interface for capybase.

Usage::

    # Safety-first workflow (recommended for a first real run):
    capybase check                    # git + LLM + tools ready? (no mutation)
    capybase rebase --dry-run <tgt>   # rehearse in a throwaway worktree
    capybase rebase <tgt>             # own the entire rebase, start → finish
                                     #   (on escalation with a terminal attached,
                                     #    drops into an interactive fallback:
                                     #    paste a resolution, edit the file, skip,
                                     #    or abort — then continues)
    capybase status                   # read-only: latest session + backups

    # Stepping through conflicts manually:
    capybase inspect                  # detect + journal + review bundle, no mutation
    capybase manual                   # interactive manual resolver, stage (no continue)
    capybase run [--resume ID]        # full auto loop with tests + continue
    capybase calibrate                # probe the model and store a tuned profile
    capybase recalibrate              # redo calibration, overwriting the stored profile
    capybase --version

Global flags (before the subcommand): --config DIR, --repo, --profile PATH,
-v/--verbose, -q/--quiet.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

from capybase import __version__
from capybase.config import Config, ModelConfig
from capybase.orchestrator import Orchestrator

# Default profile location (sibling of the risk calibration.json under memory/).
DEFAULT_PROFILE_PATH = ".rebase-agent/memory/model_profile.json"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="capybase",
        description="A rebase-conflict resolution agent with research-grade seams.",
    )
    p.add_argument("--version", action="version", version=f"capybase {__version__}")
    # Verbose mirrors the capybase logger to stderr at DEBUG so a first-time
    # user can watch the pipeline; quiet suppresses the console handler (the
    # rotating file log at <data_dir>/logs/capybase.log always runs).
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="verbose: mirror debug logs to stderr while running",
    )
    p.add_argument(
        "-q", "--quiet", action="store_true",
        help="quiet: suppress console log output (the file log still runs)",
    )
    p.add_argument(
        "--config", "-c", default=None,
        help="path to the capybase config directory (reads capybase.toml, "
             "model_profile.json, calibration.json from it; default: "
             "~/.config/capybase). A repo-local ./capybase.toml still takes "
             "precedence over the dir's.",
    )
    p.add_argument("--repo", default=".", help="path to the git repository (default: .)")
    p.add_argument("--session", default=None, help="explicit session id (default: generated)")
    p.add_argument(
        "--profile",
        default=None,
        help=(
            "path to the model profile to read at runtime / write on calibrate "
            f"(default: {DEFAULT_PROFILE_PATH}). Shared by all commands."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("inspect", help="detect conflicts and write a review bundle; no mutation")
    sub.add_parser("manual", help="interactive manual resolver; stage files, do not continue")

    run_p = sub.add_parser("run", help="full auto loop: resolve, test, continue")
    run_p.add_argument("--resume", default=None, help="resume an existing session id")

    sub.add_parser(
        "check",
        help="sanity-check config, git, tools, and the LLM endpoint before a rebase",
    )

    status_p = sub.add_parser(
        "status",
        help="show the current/latest session state and any backup branches",
    )
    status_p.add_argument(
        "--session", default=None, help="show a specific session id (default: latest)",
    )

    rb_p = sub.add_parser(
        "rebase",
        help="own the entire rebase: start it, resolve conflicts, finish",
    )
    rb_p.add_argument(
        "target",
        help="the upstream/branch to rebase onto (passed to `git rebase <target>`)",
    )
    rb_p.add_argument(
        "--autostash",
        action="store_true",
        help="autostash dirty changes before rebasing (like git rebase --autostash)",
    )
    rb_p.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="rehearse the entire rebase in a throwaway worktree; report whether "
             "it would succeed WITHOUT moving the branch pointer. Real LLM calls "
             "are made — this proves the real pipeline, it doesn't simulate it.",
    )
    inter_group = rb_p.add_mutually_exclusive_group()
    inter_group.add_argument(
        "-i", "--interactive",
        dest="interactive", action="store_true", default=True,
        help="on escalation, drop into an interactive fallback to let you resolve "
             "the conflict capybase couldn't (paste a resolution or edit the file "
             "directly), then continue. The default when a terminal is attached.",
    )
    inter_group.add_argument(
        "--no-interactive",
        dest="interactive", action="store_false",
        help="never prompt; on escalation just leave the rebase stopped (or abort, "
             "per --abort-on-escalation). For CI / scripted runs.",
    )
    abort_group = rb_p.add_mutually_exclusive_group()
    abort_group.add_argument(
        "--abort-on-escalation",
        dest="abort_on_escalation",
        action="store_true",
        help="abort the rebase if a conflict can't be auto-resolved (the default, "
             "since rebase owns the process)",
    )
    abort_group.add_argument(
        "--no-abort-on-escalation",
        dest="abort_on_escalation",
        action="store_false",
        help="leave the rebase stopped at an unresolvable conflict (inspect the "
             "review bundle and finish manually)",
    )
    rb_p.set_defaults(abort_on_escalation=True)
    color_group = rb_p.add_mutually_exclusive_group()
    color_group.add_argument(
        "--no-color",
        dest="color", action="store_false", default=None,
        help="disable ANSI color in the terminal output (the conflict sides, "
             "side-analysis, and status markers). Default: auto-detect — color "
             "when stdout is a TTY and NO_COLOR is unset; see also FORCE_COLOR.",
    )
    color_group.add_argument(
        "--color",
        dest="color", action="store_true",
        help="force ANSI color even when stdout is not a TTY (e.g. piped to a "
             "pager that renders escapes).",
    )

    cal_p = sub.add_parser(
        "calibrate",
        help="probe the model endpoint and store a tuned runtime profile",
    )
    cal_p.add_argument(
        "--json",
        action="store_true",
        help="emit the profile as JSON instead of a human-readable report",
    )
    cal_p.add_argument(
        "--dry-run",
        action="store_true",
        help="run the probes and print results, but do not write the profile",
    )

    sub.add_parser(
        "recalibrate",
        help="redo calibration: overwrites the stored profile (alias for calibrate)",
    )

    emb_p = sub.add_parser(
        "calibrate-embeddings",
        help="calibrate the embedding-retrieval similarity floor for this model",
    )
    emb_p.add_argument(
        "--json",
        action="store_true",
        help="emit the calibration envelope as JSON instead of a human-readable report",
    )
    emb_p.add_argument(
        "--dry-run",
        action="store_true",
        help="run the calibration and print results, but do not write the profile",
    )

    return p


def _format_report(report, profile_path: Path, *, written: bool = False) -> str:
    """Human-readable summary of a calibration run + a diff vs current config."""
    from capybase.calibration_profile import PROFILE_KNOBS

    lines = [f"capybase calibrate — model: {report.profile.model}"]
    status = "ok" if report.ok else "INCOMPLETE (some knobs not tuned)"
    lines.append(f"status: {status}")
    lines.append("")
    lines.append("probes:")
    for r in report.results:
        mark = "✓" if r.ok else "✗"
        lines.append(f"  {mark} {r.name:<14} {r.detail}")
    lines.append("")
    lines.append("tuned profile:")
    lines.append(f"  max_tokens                = {report.profile.max_tokens}")
    lines.append(f"  json_mode                 = {report.profile.json_mode}")
    lines.append(f"  capture_token_entropy     = {report.profile.capture_token_entropy}")
    lines.append(f"  generation_timeout_seconds= {report.profile.generation_timeout_seconds}")
    lines.append(f"  avg_latency_ms            = {report.profile.avg_latency_ms}")
    # Mechanisms: empirically A/B-selected against the blessed corpus. Show the
    # chosen sample count and which mechanisms are ON, so the user sees what
    # calibration decided (and why their resolution path may change).
    lines.append("")
    lines.append("mechanisms (corpus A/B):")
    p = report.profile
    lines.append(f"  samples                   = {p.samples}")
    lines.append(f"  two_pass                  = {'on' if p.two_pass else 'off'}")
    lines.append(f"  plan_search               = {'on' if p.plan_search else 'off'}")
    lines.append(f"  prompt_variants           = {'on' if p.prompt_variants else 'off'}")
    lines.append(f"  diverse_sampling          = {'on' if p.diverse_sampling else 'off'}")
    lines.append(f"  enable_self_consistency   = {'on' if p.enable_self_consistency else 'off'}")
    if report.profile.notes:
        lines.append("")
        lines.append("notes:")
        for n in report.profile.notes:
            lines.append(f"  - {n}")
    lines.append("")
    lines.append(f"profile knobs: {', '.join(PROFILE_KNOBS)}")
    if report.ok:
        if written:
            lines.append(f"wrote profile to: {profile_path}")
        else:
            lines.append(f"profile path: {profile_path} (not written)")
    return "\n".join(lines)


def _run_calibrate(
    config: Config,
    repo: str,
    profile_path: str,
    *,
    json_output: bool = False,
    dry_run: bool = False,
    out=sys.stdout,
    err=sys.stderr,
    client_factory: Callable[[ModelConfig], object] | None = None,
) -> int:
    """Run calibration against the model named in ``config.model``.

    ``client_factory`` lets tests inject a fake client; when None the real
    :class:`OpenAICompatibleClient` is built from ``config.model``. Writes the
    profile unless ``dry_run``. Exits non-zero if the endpoint was unreachable
    (so a transient outage doesn't silently overwrite a good profile).
    """
    from capybase.calibration_profile import resolve_profile_path
    from capybase.probes import run_calibration

    client = (
        client_factory(config.model)
        if client_factory is not None
        else _real_client(config.model)
    )
    # --dry-run skips the expensive mechanism A/B sweep (resolves the corpus
    # ~14×); it's a quick capability check (max_tokens/json_mode/logprobs) only.
    report = run_calibration(
        client,
        config.model,
        run_mechanisms=not dry_run,
        embeddings_model=config.memory.embeddings_model,
    )

    resolved = resolve_profile_path(repo, profile_path)
    written = False
    if report.ok and not dry_run:
        # Preserve the embeddings calibration across an LLM re-tune: the two
        # commands co-own this file, so a fresh ``calibrate`` must not silently
        # wipe the model-specific ``embedding_min_similarity`` + envelope that
        # ``calibrate-embeddings`` derived. Carry them over ONLY when the stored
        # profile is for the same model — a model swap correctly drops them (the
        # calibrated floor was fit for the old model and would be wrong now).
        from capybase.calibration_profile import ModelProfile

        prior = ModelProfile.load(resolved)
        if prior is not None and prior.model == report.profile.model:
            report.profile.embedding_min_similarity = prior.embedding_min_similarity
            report.profile.embedding_calibration = prior.embedding_calibration
            report.profile.fusion_method = prior.fusion_method
        report.profile.save(resolved)
        written = True

    if json_output:
        import json

        payload = report.profile.to_dict()
        payload["_written"] = written
        payload["_ok"] = report.ok
        print(json.dumps(payload, indent=2), file=out)
    else:
        text = _format_report(report, resolved, written=written)
        if dry_run:
            text += "\n(dry-run: profile not written)"
        elif not report.ok:
            text += "\nendpoint unreachable — profile NOT written"
        print(text, file=out)
    return 0 if report.ok else 1


def _real_client(model_cfg: ModelConfig):
    """Build the live OpenAI-compatible client. Lazily imported to keep the
    CLI importable when the adapter has optional dependencies missing."""
    from capybase.adapters.llm_openai import OpenAICompatibleClient

    return OpenAICompatibleClient(model_cfg)


def _real_embeddings_client(model_cfg: ModelConfig, embeddings_model: str):
    """Build the live embeddings client, using the embedding model name when set.

    On a multi-model llama-server the embedding slot has a distinct id; on a
    single-model server the completion model name is reused. Mirrors the
    orchestrator's retriever construction.
    """
    from capybase.memory.embeddings import OpenAIEmbeddingsClient

    emb_cfg = model_cfg
    if embeddings_model:
        emb_cfg = emb_cfg.model_copy(update={"model": embeddings_model})
    return OpenAIEmbeddingsClient(emb_cfg)


def _format_embeddings_report(
    cal, profile_path: Path, *, written: bool, prev_floor: float, drift=None
) -> str:
    """Human-readable summary of an embeddings-calibration run."""
    lines = [f"capybase calibrate-embeddings — model: {cal.model}"]
    lines.append(f"status: {'ok' if cal.ok else 'FAILED (endpoint unreachable)'}")
    lines.append("")
    lines.append("measured score distributions:")
    lines.append(
        f"  related   : n={cal.related.count}  "
        f"min={cal.related.minimum:.3f}  max={cal.related.maximum:.3f}  "
        f"mean={cal.related.mean:.3f}"
    )
    lines.append(
        f"  unrelated : n={cal.unrelated.count}  "
        f"min={cal.unrelated.minimum:.3f}  max={cal.unrelated.maximum:.3f}  "
        f"mean={cal.unrelated.mean:.3f}"
    )
    lines.append("")
    lines.append("threshold estimates:")
    lines.append(f"  quantile_gap (applied) = {cal.min_similarity:.3f}")
    lines.append(f"  related_p10            = {cal.related_p10:.3f}")
    lines.append(f"  unrelated_p90          = {cal.unrelated_p90:.3f}")
    if cal.has_isotonic_fit:
        # Score-calibration (survey §2.1): the isotonic transform maps raw cosines
        # onto a model-agnostic scale; the three zones (§3.2) are derived on it.
        lines.append("")
        lines.append(
            f"score calibration: isotonic transform ({len(cal.isotonic_points)} pts), "
            f"KS separation = {cal.ks_separation:.3f} on calibrated scale"
        )
        lines.append("  three-zone thresholds (calibrated scale):")
        lines.append(f"    green (high-confidence) = {cal.green_threshold:.3f}")
        lines.append(f"    amber (borderline)      = {cal.amber_threshold:.3f}")
        lines.append(f"    red   (hard floor)      = {cal.red_threshold:.3f}")
    lines.append("")
    lines.append(f"chosen min_similarity   = {cal.min_similarity:.3f}  (was {prev_floor:.3f})")
    if cal.notes:
        lines.append("")
        lines.append("notes:")
        for n in cal.notes:
            lines.append(f"  - {n}")
    if drift is not None:
        lines.append("")
        flag = "DRIFT DETECTED" if drift.drifted else "no drift vs last calibration"
        lines.append(f"drift vs last calibration: {flag}")
        lines.append(
            f"  related median shift   = {drift.related_median_shift:+.3f}  "
            f"(MAD ratio {drift.related_mad_ratio:.2f})"
        )
        lines.append(
            f"  unrelated median shift = {drift.unrelated_median_shift:+.3f}  "
            f"(MAD ratio {drift.unrelated_mad_ratio:.2f})"
        )
        lines.append(f"  class separation Δ     = {drift.ks_separation_delta:+.3f}")
        for r in drift.reasons:
            lines.append(f"    - {r}")
    lines.append("")
    if written:
        lines.append(f"wrote embedding calibration to: {profile_path}")
    else:
        lines.append(f"profile path: {profile_path} (not written)")
    return "\n".join(lines)


def _run_calibrate_embeddings(
    config: Config,
    repo: str,
    profile_path: str,
    *,
    json_output: bool = False,
    dry_run: bool = False,
    out=sys.stdout,
    err=sys.stderr,
    client_factory: Callable[[ModelConfig, str], object] | None = None,
) -> int:
    """Calibrate the embedding-retrieval similarity floor for the active model.

    Derives a model-specific ``min_similarity`` from the corpus score distribution
    and writes it into the model profile (alongside the LLM-calibration knobs),
    preserving all other profile fields. ``client_factory`` lets tests inject a
    fake embeddings client; when None the real client is built. Exits non-zero if
    the embeddings endpoint was unreachable.
    """
    import json

    from capybase.calibration_profile import ModelProfile, resolve_profile_path
    from capybase.embeddings_calibration import (
        EmbeddingCalibration,
        calibrate_thresholds,
        compare_calibration,
    )

    embeddings_model = config.memory.embeddings_model
    client = (
        client_factory(config.model, embeddings_model)
        if client_factory is not None
        else _real_embeddings_client(config.model, embeddings_model)
    )
    cal = calibrate_thresholds(client, embeddings_model=embeddings_model)

    resolved = resolve_profile_path(repo, profile_path)
    written = False
    prev_floor = 0.35
    drift = None  # advisory drift-vs-baseline report (survey 2 §7)
    if cal.ok:
        # Load the existing profile (preserving LLM-calibration knobs); a missing
        # profile is created fresh via from_dict (safe defaults for required
        # fields). Read the prior floor whenever the endpoint worked so the
        # "was X.XXX" delta in the report reflects the stored value — including
        # under --dry-run (the user runs it precisely to see what would change).
        profile = ModelProfile.load(resolved)
        if profile is None:
            # Construct a placeholder directly (not via from_dict, which now
            # validates load-bearing knobs and would reject a max_tokens of 0).
            # This placeholder only carries the model name + the embedding-floor
            # defaults the code below reads; the resolution knobs are inert here.
            profile = ModelProfile(
                model=config.model.model,
                max_tokens=1,
                json_mode=True,
                capture_token_entropy=False,
                generation_timeout_seconds=60,
            )
        if profile.model == config.model.model:
            prev_floor = profile.embedding_min_similarity
            # Offline drift detection: compare against the prior run's envelope
            # for the same model (advisory; never blocks the write).
            prior_env = profile.embedding_calibration or {}
            if prior_env:
                try:
                    baseline = EmbeddingCalibration.from_dict(prior_env)
                    drift = compare_calibration(cal, baseline)
                except Exception:  # noqa: BLE001 - best-effort; a bad envelope is no-drift
                    drift = None
        if not dry_run:
            profile.model = config.model.model  # keep the match key current
            profile.embedding_min_similarity = cal.min_similarity
            env = cal.to_dict()
            if drift is not None:
                env["drift"] = drift.to_dict()
            profile.embedding_calibration = env
            profile.save(resolved)
            written = True

    if json_output:
        payload = cal.to_dict()
        payload["_written"] = written
        payload["_ok"] = cal.ok
        if drift is not None:
            payload["drift"] = drift.to_dict()
        print(json.dumps(payload, indent=2), file=out)
    else:
        text = _format_embeddings_report(
            cal, resolved, written=written, prev_floor=prev_floor, drift=drift
        )
        if dry_run:
            text += "\n(dry-run: profile not written)"
        elif not cal.ok:
            text += "\nembeddings endpoint unreachable — profile NOT written"
        print(text, file=out)
    return 0 if cal.ok else 1


def _run_check(
    config: Config,
    repo: str,
    *,
    out=sys.stdout,
    client_factory: Callable[[ModelConfig], object] | None = None,
) -> int:
    """Pre-flight confidence check: is this repo ready to ``capybase rebase``?

    Runs the full rebase preflight (including an LLM ping — the highest-value
    check), plus calibration-presence and tooling availability. Prints a
    readable report and exits 0 ("ready to rebase") or non-zero (blocking
    failure, or warnings only with ``--strict``). ``client_factory`` lets tests
    inject a fake LLM client; when None the real client is built.
    """
    import shutil
    from pathlib import Path

    from capybase.git_backend import GitBackend
    from capybase.preflight import run_rebase_preflight

    lines: list[str] = ["capybase check", "=" * 40]

    # Config source (informational).
    src = getattr(config, "source_path", None) or "<built-in defaults>"
    lines.append(f"config source : {src}")
    lines.append(f"model         : {config.model.model!r} @ {config.model.base_url}")

    # Calibration presence (warn, not block).
    profile_p = Path(config.calibration.model_profile_path)
    if profile_p.exists():
        lines.append(f"profile       : {profile_p} (present)")
    else:
        lines.append(f"profile       : {profile_p} (absent — run `capybase calibrate` to tune; resolution still works with built-in defaults)")

    # Tools (informational; a missing optional tool never blocks).
    tools = []
    for name in ("pyright", "rust-analyzer", "cargo", "rustc", "pytest"):
        tools.append((name, shutil.which(name) is not None))
    tool_line = ", ".join(f"{n}: {'yes' if ok else 'no'}" for n, ok in tools)
    lines.append(f"tools         : {tool_line}")

    # Git-state preflight (no target, no LLM — those are handled separately
    # below). The target-dependent checks (target-resolves, not-self-rebase,
    # rebase-shape) are meaningless for `check` since there's no target yet, so
    # we run only the git-state checks.
    git = GitBackend(repo)
    report = run_rebase_preflight(git, config, "HEAD", autostash=False, llm_ping=False)
    lines.append("-" * 40)
    lines.append("preflight:")
    blocking_fail = False
    # Skip target-dependent checks for `check` (there's no target yet).
    skip_for_check = {"target-resolves", "not-self-rebase", "rebase-shape"}
    for c in report.checks:
        if c.name in skip_for_check:
            continue
        tag = "ok  " if c.ok else ("WARN " if not c.blocking else "FAIL ")
        lines.append(f"  [{tag}] {c.name}: {c.detail}")
        if c.blocking and not c.ok:
            blocking_fail = True

    # LLM ping — honor client_factory so tests can fake reachability without a
    # server. Done here (not in preflight) so we control the client exactly.
    from capybase.probes import probe_reachability
    try:
        client = (
            client_factory(config.model)
            if client_factory is not None
            else _real_client(config.model)
        )
        result = probe_reachability(client, config.model)
        if result.ok:
            lines.append(f"  [ok  ] llm-reachable: {result.detail}")
        else:
            lines.append(f"  [FAIL ] llm-reachable: {result.detail}")
            blocking_fail = True
    except Exception as exc:  # noqa: BLE001
        lines.append(f"  [FAIL ] llm-reachable: {exc}")
        blocking_fail = True

    lines.append("=" * 40)
    if blocking_fail:
        lines.append("NOT ready to rebase — fix the FAIL items above.")
    else:
        lines.append("ready to rebase (warnings are informational).")
    print("\n".join(lines), file=out)
    return 1 if blocking_fail else 0


def _run_status(
    config: Config,
    repo: str,
    session_id: str | None = None,
    *,
    out=sys.stdout,
) -> int:
    """Read-only report of the current repo's rebase state.

    Shows whether a git operation is in progress, the latest (or ``--session``)
    capybase session's outcome, and any leftover backup branches with delete
    hints. Never mutates anything.
    """
    from capybase.git_backend import GitBackend
    from capybase.session import SESSIONS_DIR, SessionPaths
    from capybase.journal import Journal

    git = GitBackend(repo)
    lines: list[str] = ["capybase status", "=" * 40]

    # Live repo state.
    op = git.operation_in_progress()
    branch = git.current_branch()
    lines.append(f"branch        : {branch or '<detached HEAD>'}")
    lines.append(f"op in progress: {op or 'none'}")
    lines.append(f"head          : {git.head_oid()[:8]}")

    # Session: explicit --session, else the latest under .rebase-agent/sessions.
    repo_root = git.repo
    sessions_dir = repo_root / SESSIONS_DIR
    sid = session_id
    if sid is None:
        sids = sorted(
            (p.name for p in sessions_dir.iterdir() if p.is_dir()),
            key=lambda s: (sessions_dir / s).stat().st_mtime,
        ) if sessions_dir.exists() else []
        sid = sids[-1] if sids else None
    if sid is None:
        lines.append("session       : <none> (no capybase sessions in this repo)")
    else:
        paths = SessionPaths(sid, repo_root)
        events = Journal(paths).read_events()
        lines.append(f"session       : {sid} ({len(events)} events)")
        if events:
            # Pull key facts from the journal events.
            target = ""
            backup_ref = ""
            escalated_reason = ""
            completed = False
            for ev in events:
                if ev.event_type == "rebase_started":
                    target = ev.payload.get("target", target)
                    backup_ref = ev.payload.get("backup_ref", backup_ref)
                elif ev.event_type == "session_completed":
                    completed = True
                elif ev.event_type == "escalated":
                    escalated_reason = ev.payload.get("reason", escalated_reason)
                elif ev.event_type == "rebase_aborted":
                    backup_ref = ev.payload.get("backup_ref", backup_ref or "")
            status = "completed" if completed else ("escalated" if escalated_reason else "stopped/unknown")
            lines.append(f"  target      : {target or '(n/a)'}")
            lines.append(f"  status      : {status}")
            if escalated_reason:
                lines.append(f"  reason      : {escalated_reason}")
            if backup_ref:
                lines.append(f"  backup      : {backup_ref}")
            last = events[-1]
            lines.append(f"  last event  : {last.event_type}")
            bundle = paths.final / "review-bundle.md"
            if bundle.exists():
                lines.append(f"  review bundle: {bundle}")

    # Backup branches.
    backups = git.list_backup_refs()
    if backups:
        lines.append("-" * 40)
        lines.append(f"backup branches ({len(backups)}):")
        for b in backups:
            lines.append(f"  {b}  (delete: git branch -D {b})")
    print("\n".join(lines), file=out)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Configure the cross-session operational logger early. Verbose mirrors
    # debug output to stderr; quiet mutes the console. The rotating file log
    # (always on) is best-effort and never breaks a run.
    from capybase.logging_setup import configure_logging
    configure_logging(verbose=args.verbose, quiet=args.quiet)
    # --config is a DIRECTORY (the shared config dir), not a file. capybase reads
    # capybase.toml + calibration artifacts from it, so the user repo need not
    # carry any capybase config. A repo-local ./capybase.toml still wins (per-repo
    # overrides); see Config.load for the full precedence.
    config = Config.load(config_dir=args.config)

    # The global --profile overrides the profile location for BOTH reading
    # (run/inspect/manual: the orchestrator overlay loads it from here) and
    # writing (calibrate writes it here). When unset, fall back to the config's
    # [calibration] model_profile_path (which itself defaults to the memory dir).
    profile_path = args.profile or config.calibration.model_profile_path
    if args.profile:
        config.calibration.model_profile_path = args.profile

    # calibrate / recalibrate don't need an orchestrator or a git repo session.
    if args.command in ("calibrate", "recalibrate"):
        return _run_calibrate(
            config,
            repo=args.repo,
            profile_path=profile_path,
            json_output=getattr(args, "json", False),
            dry_run=getattr(args, "dry_run", False),
        )

    if args.command == "calibrate-embeddings":
        return _run_calibrate_embeddings(
            config,
            repo=args.repo,
            profile_path=profile_path,
            json_output=getattr(args, "json", False),
            dry_run=getattr(args, "dry_run", False),
        )

    # check / status don't need an orchestrator (they use the git backend
    # directly and never drive a resolution loop).
    if args.command == "check":
        return _run_check(config, repo=args.repo)
    if args.command == "status":
        return _run_status(config, repo=args.repo, session_id=args.session)

    try:
        session = getattr(args, "session", None) or getattr(args, "resume", None)
        # Color: --color forces on, --no-color forces off, default auto-detects
        # via color_enabled(stdout) honoring NO_COLOR/FORCE_COLOR/isatty.
        from capybase.color import color_enabled

        color = getattr(args, "color", None)
        if color is None:
            color = color_enabled(sys.stdout)
        orch = Orchestrator(config, repo=args.repo, session_id=session, color=color)
    except Exception as exc:  # noqa: BLE001 - top-level CLI guard
        print(f"capybase: error: {exc}", file=sys.stderr)
        return 2

    if args.command == "inspect":
        result = orch.inspect()
        return 1 if result.escalated else 0
    if args.command == "manual":
        result = orch.manual()
        return 1 if result.escalated else 0
    if args.command == "run":
        result = orch.run()
        return 1 if result.escalated else 0
    if args.command == "rebase":
        if getattr(args, "dry_run", False):
            # Rehearse in a throwaway worktree: never moves the branch pointer.
            # Uses real LLM calls (the point of a rehearsal); no orchestrator
            # against the real repo is constructed.
            from capybase.dryrun import rehearse_rebase
            report = rehearse_rebase(
                config, repo=args.repo, target=args.target, autostash=args.autostash,
            )
            print(report.summary())
            return 0 if report.would_succeed else 1
        result = orch.rebase(
            args.target,
            autostash=args.autostash,
            abort_on_escalation=args.abort_on_escalation,
            interactive=args.interactive,
        )
        return 1 if result.escalated else 0
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
