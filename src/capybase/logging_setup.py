"""Structured logging configuration for capybase.

The per-session JSONL journal (``.rebase-agent/sessions/<id>/journal.jsonl``) is
the authoritative audit of *one* run. This module provides the *cross-session*
operational trail: a rotating file under the XDG data dir (always on, INFO) that
records rebase starts, per-step LLM prompt/response sizes, accept/escalate
decisions, and final outcomes across every run and every repo — the thing you
read when debugging "why did yesterday's rebase on repo X fail?".

Console verbosity is user-controlled:

- default: no console log noise (user-facing output goes through the
  orchestrator's ``out`` callback, unchanged).
- ``-v/--verbose``: mirror DEBUG to stderr so a first-time user can watch the
  pipeline run.
- ``-q/--quiet``: suppress even the file handler side-effects' WARNINGs on the
  console (the file always logs).

Idempotent: calling :func:`configure_logging` more than once (e.g. in tests)
removes existing handlers before adding new ones, so log lines aren't duplicated.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from capybase.config import default_data_dir

LOGGER_NAME = "capybase"
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(
    *,
    verbose: bool = False,
    quiet: bool = False,
    data_dir: Path | None = None,
) -> logging.Logger:
    """Configure the ``capybase`` logger.

    Always installs a :class:`RotatingFileHandler` at INFO under
    ``<data_dir>/logs/capybase.log`` (creating the dir). Adds a stderr
    :class:`logging.StreamHandler` at DEBUG when ``verbose``. ``quiet`` mutes
    the console handler entirely (the file handler still runs). Returns the
    configured logger.

    The file handler is best-effort: if the data dir can't be created or written
    (read-only home, permissions), logging degrades to console-only rather than
    crashing — logging must never break a rebase.
    """
    log = logging.getLogger(LOGGER_NAME)
    # Remove handlers from a prior configure call (idempotent / test-safe).
    for h in list(log.handlers):
        log.removeHandler(h)

    formatter = logging.Formatter(_FORMAT)

    # File handler: always on, INFO. Best-effort.
    if data_dir is None:
        data_dir = default_data_dir()
    file_handler: logging.FileHandler | None = None
    try:
        log_dir = data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "capybase.log", maxBytes=2_000_000, backupCount=3
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        log.addHandler(file_handler)
    except OSError:
        # Read-only home, no disk, permissions — never let logging break a run.
        file_handler = None

    # Console handler: DEBUG when --verbose, absent otherwise (and when --quiet).
    if verbose and not quiet:
        console = logging.StreamHandler()
        console.setLevel(logging.DEBUG)
        console.setFormatter(formatter)
        log.addHandler(console)

    # Overall level: DEBUG so --verbose can see everything; the file handler's
    # own INFO level filters the file independently.
    log.setLevel(logging.DEBUG)
    log.propagate = False  # don't double-log through the root logger
    return log
