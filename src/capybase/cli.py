"""Command-line interface for capybase.

Usage::

    capybase inspect              # M1: detect + journal + review bundle, no mutation
    capybase manual               # M2: interactive manual resolver, stage (no continue)
    capybase run [--resume ID]    # M3: full auto loop with tests + continue
    capybase --version

All commands honor --config PATH and --dry-run.
"""

from __future__ import annotations

import argparse
import sys

from capybase import __version__
from capybase.config import Config
from capybase.orchestrator import Orchestrator


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="capybase",
        description="A rebase-conflict resolution agent with research-grade seams.",
    )
    p.add_argument("--version", action="version", version=f"capybase {__version__}")
    p.add_argument(
        "--config", "-c", default=None, help="path to capybase.toml (default: ./capybase.toml)"
    )
    p.add_argument("--repo", default=".", help="path to the git repository (default: .)")
    p.add_argument("--session", default=None, help="explicit session id (default: generated)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("inspect", help="detect conflicts and write a review bundle; no mutation")
    sub.add_parser("manual", help="interactive manual resolver; stage files, do not continue")

    run_p = sub.add_parser("run", help="full auto loop: resolve, test, continue")
    run_p.add_argument("--resume", default=None, help="resume an existing session id")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = Config.load(args.config)

    try:
        session = getattr(args, "session", None) or getattr(args, "resume", None)
        orch = Orchestrator(config, repo=args.repo, session_id=session)
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
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
