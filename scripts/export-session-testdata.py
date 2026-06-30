#!/usr/bin/env python3
"""Export accepted conflict resolutions from capybase SESSIONS as test cases.

The companion to ``fetch_mergeconflict_datasets.py``. Where that script mines
*external* GitHub conflicts with a *human* merge (the real-world set), this one
mines capybase's OWN rebase sessions — the conflicts capybase actually faced and
the resolutions the model actually produced. The value is different and
complementary:

  - **External set** — human-merge oracle; "does capybase accept the human M?"
  - **Session set**  — model-merge regression; "does capybase still resolve what
    it once resolved?" + "do the verifier hooks still fire on this conflict?"

The journal is the data spine. For every conflict unit that reached an ACCEPTED
candidate, this script joins three on-disk artifacts:

  prompts/<unit>.attempt0.txt   the 3-way sides (CURRENT_UPSTREAM / REPLAYED / BASE)
                                 + surrounding context, verbatim
  candidates/<id>.json          the model's resolved_text + verdict metadata
  validations/<unit>-file.json  the whole-file (cargo check / py_compile) verdict

and emits one JSON per unit under ``extracted-testdata/sessions/`` in a shape
``tests/session_loader.py`` consumes (which mirrors ``realworld_loader.py``).

The 3-way sides come straight from the resolve prompt's literal section headers
(``... body (exact, including leading spaces):``); the original conflict-marked
file is NOT on disk (snapshots are post-resolution, marker-free), so
``marker_original`` is RECONSTRUCTED from current+replayed (+ a diff3 ``|||||||``
base section) — enough to satisfy ``parse_marker_blocks`` (≥1 block).

Idempotent: re-running overwrites case files by id. Never mutates the session.

Usage:
    python scripts/export-session-testdata.py --session /path/to/.rebase-agent/sessions/<id>
    python scripts/export-session-testdata.py --repo /path/to/repo      # all sessions
    python scripts/export-session-testdata.py --repo . --require-verified
    python scripts/export-session-testdata.py --session <id> --dry-run  # print, write nothing
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths. ``REPO_ROOT`` resolves relative to this script (scripts/), so it works
# from any CWD. The default output dir sits next to ``realworld/`` so the empty
# directory is tracked on a fresh clone (each dataset dir keeps its own
# ignore-everything .gitignore; tests skip cleanly when empty).
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "extracted-testdata" / "sessions"

# ---------------------------------------------------------------------------
# Prompt section grammar. The resolve/retry prompts emit these EXACT headers
# (resolution_engine._resolve_prompt_parts); a unit's ``attempt0.txt`` is a
# resolve prompt and reliably carries all three sides + surrounding context.
# Block-capture prompts (a different header family) lack the trio and are
# skipped — they are only emitted under a non-default future flag anyway.
# Each section body is ``{lines}\n\n`` then the next header, so the body is the
# text between one header line and the next recognized header.
# ---------------------------------------------------------------------------
_HEADER_CURRENT = "CURRENT_UPSTREAM_SIDE body (exact, including leading spaces):"
_HEADER_REPLAYED = "REPLAYED_COMMIT_SIDE body (exact, including leading spaces):"
_HEADER_BASE = "BASE (common ancestor) body, for context:"
_HEADER_CONTEXT = "Surrounding file context:"
# Header lines we recognize as section terminators (order in the prompt):
_SECTION_HEADERS = (_HEADER_CURRENT, _HEADER_REPLAYED, _HEADER_BASE, _HEADER_CONTEXT)

# The legacy prompt family (pre-v5; old fixtures only) uses different headers.
_LEGACY_HEADERS = {
    "CURRENT_UPSTREAM_SIDE": "The conflict block's CURRENT_UPSTREAM_SIDE body is exactly:",
    "REPLAYED_COMMIT_SIDE": "The conflict block's REPLAYED_COMMIT_SIDE body is exactly:",
    "BASE": "The BASE (common ancestor) body, for context, was:",
}

# The JSON contract's placeholder string. When the model emits this verbatim as
# its resolved_text, the "resolution" is not a real merge — the model returned
# the template stub. This happens on hard units (often a sub-block of a larger
# conflict that the whole-file repair path ultimately settled elsewhere). Such
# cases are kept (the conflict sides are still valid; the failure is a real
# model-behavior signal) but flagged so tests that depend on the resolution
# *content* can exclude them while conflict-shape/verifier tests keep them.
_PLACEHOLDER_RESOLUTION = "<merged replacement text>"


@dataclass(frozen=True)
class ParsedSides:
    """The 3-way sides + surrounding context parsed from one resolve prompt."""

    current: str
    replayed: str
    base: str
    surrounding_context: str
    legacy: bool = False  # True if parsed from the pre-v5 header family


@dataclass(frozen=True)
class SessionCase:
    """One accepted conflict resolution projected from a session."""

    id: str
    dataset: str
    session_id: str
    path: str
    language: str
    base: str
    current: str
    replayed: str
    marker_original: str
    accepted_resolution: str
    file_validation_passed: bool
    attempts: int
    needs_human: bool
    prompt_version: str
    model: str
    provenance: dict
    is_placeholder_resolution: bool = False


# ---------------------------------------------------------------------------
# Prompt parsing
# ---------------------------------------------------------------------------


def parse_resolve_prompt(text: str) -> ParsedSides | None:
    """Extract the 3-way sides + context from a stored resolve/retry prompt.

    Returns ``None`` if the per-side trio is absent (e.g. a block-capture prompt,
    or a prompt that never carried the sides). The parser is line-oriented and
    uses the literal section headers as delimiters: a section body runs from the
    line after its header up to (but not including) the next recognized header
    or the end of the sides block.
    """
    lines = text.splitlines()

    # Locate the four primary headers by line index.
    idx = {h: None for h in _SECTION_HEADERS}
    for i, ln in enumerate(lines):
        for h in _SECTION_HEADERS:
            if idx[h] is None and ln.rstrip("\r") == h:
                idx[h] = i
    # The trio (current/replayed/base) is mandatory; context is best-effort.
    if idx[_HEADER_CURRENT] is None or idx[_HEADER_REPLAYED] is None:
        return None
    if idx[_HEADER_BASE] is None:
        return None

    def section(header: str, next_headers: tuple[str, ...]) -> str:
        start = idx[header]
        # The body starts on the line after the header. Find the next recognized
        # header at or below the body start.
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if lines[j].rstrip("\r") in next_headers:
                end = j
                break
        body = lines[start + 1 : end]
        # Drop the single trailing blank line that precedes the next header
        # (the template emits ``{lines}\n\n``). Preserve all other blank lines
        # (empty sides, internal blanks) — they are significant.
        while body and body[-1].strip() == "":
            body.pop()
        return "\n".join(body)

    current = section(_HEADER_CURRENT, (_HEADER_REPLAYED, _HEADER_BASE, _HEADER_CONTEXT))
    replayed = section(_HEADER_REPLAYED, (_HEADER_BASE, _HEADER_CONTEXT))
    base = section(_HEADER_BASE, (_HEADER_CONTEXT,))
    context = ""
    if idx[_HEADER_CONTEXT] is not None:
        # Surrounding context runs to the end of the sides/data block; cap it so
        # we don't sweep up the contract/rules sections that follow. The contract
        # always begins with a JSON-schema fenced block or a "Your output" rule;
        # take a generous but bounded window.
        cstart = idx[_HEADER_CONTEXT]
        cend = len(lines)
        for j in range(cstart + 1, len(lines)):
            # The data block ends where the contract begins (heuristic: a line
            # starting a fenced ``` block or the literal contract preamble).
            if lines[j].startswith("```") or lines[j].startswith("Output the JSON"):
                cend = j
                break
        body = lines[cstart + 1 : cend]
        while body and body[-1].strip() == "":
            body.pop()
        context = "\n".join(body)

    return ParsedSides(
        current=current,
        replayed=replayed,
        base=base,
        surrounding_context=context,
    )


def build_marker_original(cur: str, rep: str, base: str) -> str:
    """Reconstruct a conflict-marked block from the three sides.

    Produces a diff3-style block (``<<<<<<<`` / ``|||||||`` / ``=======`` /
    ``>>>>>>>``) so ``parse_marker_blocks`` sees a well-formed, single-block
    conflict. The base section is included only when non-empty (diff3).
    """
    parts = ["<<<<<<< HEAD", cur if cur else ""]
    if base.strip():
        parts.append("||||||| merged-common-ancestor")
        parts.append(base)
    parts.append("=======")
    parts.append(rep if rep else "")
    parts.append(">>>>>>> REPLAYED")
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Session projection
# ---------------------------------------------------------------------------


def _safe_stem(unit_id: str) -> str:
    """Mirror journal._safe so artifact filenames round-trip (unit -> stem)."""
    return unit_id.replace("/", "__").replace(":", "-")


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def project_session(session_dir: Path, require_verified: bool = False) -> list[SessionCase]:
    """Project one session dir into a list of accepted-resolution cases.

    Walks the journal for units that reached a ``candidate_accepted``; for each,
    joins the prompt sides, the accepted candidate, and the file validation.
    Units whose prompt lacks the sides trio, or (under ``--require-verified``)
    whose file didn't validate, are skipped with a logged reason.
    """
    journal = session_dir / "journal.jsonl"
    if not journal.exists():
        return []

    events = _read_jsonl(journal)

    # Session-level provenance (target/start_oid/backup) from rebase_started.
    provenance: dict = {}
    for e in events:
        if e.get("event_type") == "rebase_started":
            provenance = {
                "target": e.get("payload", {}).get("target", ""),
                "start_oid": e.get("payload", {}).get("start_oid", ""),
                "backup_ref": e.get("payload", {}).get("backup_ref", ""),
            }
            break
    session_id = session_dir.name

    # Walk events in order; track per-unit state.
    #   accepted_unit_id -> last candidate_id that was accepted
    #   path_for_unit, lang_for_unit
    units: dict[str, dict] = {}  # unit_id -> meta
    accepted: dict[str, str] = {}  # unit_id -> candidate_id of latest accept
    rejected_attempts: dict[str, int] = {}  # unit_id -> count of rejections/retries
    file_validated: dict[str, dict] = {}  # path -> {passed, features}

    for e in events:
        et = e.get("event_type")
        uid = e.get("unit_id")
        path = e.get("path")
        pl = e.get("payload", {})
        if et == "conflict_unit_extracted" and uid and path:
            units[uid] = {
                "path": path,
                "language": pl.get("language", ""),
                "step": e.get("step_index"),
            }
        elif et == "risk_decision" and pl.get("action") in ("retry", "reject") and uid:
            rejected_attempts[uid] = rejected_attempts.get(uid, 0) + 1
        elif et == "candidate_accepted" and uid:
            cid = pl.get("candidate_id")
            if cid:
                accepted[uid] = cid
        elif et == "file_validated" and path:
            file_validated[path] = {
                "passed": bool(pl.get("passed")),
                "features": pl.get("features", {}) if isinstance(pl.get("features"), dict) else {},
            }

    cases: list[SessionCase] = []
    prompts_dir = session_dir / "prompts"
    candidates_dir = session_dir / "candidates"

    for uid, meta in units.items():
        path = meta["path"]
        lang = meta.get("language") or _infer_language(path)
        cid = accepted.get(uid)
        if not cid:
            print(
                f"  skip {session_id} {uid}: no accepted candidate",
                file=sys.stderr,
            )
            continue

        # Accepted candidate -> resolved_text + metadata.
        cand_path = candidates_dir / f"{_safe_stem(cid)}.json"
        if not cand_path.exists():
            print(
                f"  skip {session_id} {uid}: candidate file missing ({cid})",
                file=sys.stderr,
            )
            continue
        candidate = json.loads(cand_path.read_text(encoding="utf-8"))
        resolved = candidate.get("resolved_text", "")

        # File validation for this path.
        fv = file_validated.get(path, {})
        file_passed = fv.get("passed", False)
        if require_verified and not file_passed:
            print(
                f"  skip {session_id} {uid}: file not verified (--require-verified)",
                file=sys.stderr,
            )
            continue

        # Sides from the resolve prompt (attempt0).
        prompt_path = prompts_dir / f"{_safe_stem(uid)}.attempt0.txt"
        if not prompt_path.exists():
            print(
                f"  skip {session_id} {uid}: no attempt0 prompt on disk",
                file=sys.stderr,
            )
            continue
        prompt_text = prompt_path.read_text(encoding="utf-8")
        sides = parse_resolve_prompt(prompt_text)
        if sides is None:
            print(
                f"  skip {session_id} {uid}: prompt has no 3-way sides "
                "(likely block-capture)",
                file=sys.stderr,
            )
            continue

        case_id = f"session-{session_id}--{_safe_stem(uid)}"
        marker = build_marker_original(sides.current, sides.replayed, sides.base)
        is_placeholder = _PLACEHOLDER_RESOLUTION in resolved
        cases.append(
            SessionCase(
                id=case_id,
                dataset="capybase-session",
                session_id=session_id,
                path=path,
                language=lang,
                base=sides.base,
                current=sides.current,
                replayed=sides.replayed,
                marker_original=marker,
                accepted_resolution=resolved,
                file_validation_passed=file_passed,
                attempts=rejected_attempts.get(uid, 0),
                needs_human=bool(candidate.get("needs_human", False)),
                prompt_version=candidate.get("prompt_version", ""),
                model=candidate.get("model_name", ""),
                provenance=provenance,
                is_placeholder_resolution=is_placeholder,
            )
        )
    return cases


def _infer_language(path: str) -> str:
    if path.endswith(".py"):
        return "python"
    if path.endswith(".rs"):
        return "rust"
    return ""


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def case_to_jsonable(c: SessionCase) -> dict:
    return {
        "id": c.id,
        "dataset": c.dataset,
        "session_id": c.session_id,
        "path": c.path,
        "language": c.language,
        "base": c.base,
        "current": c.current,
        "replayed": c.replayed,
        "marker_original": c.marker_original,
        "accepted_resolution": c.accepted_resolution,
        "file_validation_passed": c.file_validation_passed,
        "attempts": c.attempts,
        "needs_human": c.needs_human,
        "prompt_version": c.prompt_version,
        "model": c.model,
        "provenance": c.provenance,
        "is_placeholder_resolution": c.is_placeholder_resolution,
    }


def write_cases(cases: list[SessionCase], out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for c in cases:
        target = out_dir / f"{c.id}.json"
        target.write_text(
            json.dumps(case_to_jsonable(c), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        written += 1
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _find_sessions(repo: Path) -> list[Path]:
    sessions = repo / ".rebase-agent" / "sessions"
    if not sessions.is_dir():
        return []
    return sorted(p for p in sessions.iterdir() if (p / "journal.jsonl").exists())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--session",
        type=Path,
        help="a single session dir (contains journal.jsonl)",
    )
    src.add_argument(
        "--repo",
        type=Path,
        help="repo root; projects every session under <repo>/.rebase-agent/sessions/",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"output dir (default: {DEFAULT_OUT.relative_to(REPO_ROOT)})",
    )
    ap.add_argument(
        "--require-verified",
        action="store_true",
        help="only emit units whose whole-file validation passed",
    )
    ap.add_argument(
        "--language",
        choices=("python", "rust"),
        help="filter to one language",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the projected cases, write nothing",
    )
    args = ap.parse_args()

    if args.session is not None:
        if not (args.session / "journal.jsonl").exists():
            ap.error(f"not a session dir (no journal.jsonl): {args.session}")
        sessions = [args.session.resolve()]
    else:
        sessions = _find_sessions(args.repo.resolve())
        if not sessions:
            print(f"no sessions found under {args.repo}/.rebase-agent/sessions/", file=sys.stderr)

    all_cases: list[SessionCase] = []
    for s in sessions:
        print(f"projecting {s.name} ...", file=sys.stderr)
        cases = project_session(s, require_verified=args.require_verified)
        if args.language:
            cases = [c for c in cases if c.language == args.language]
        all_cases.extend(cases)

    if args.dry_run:
        for c in all_cases:
            print(
                f"  {c.id}  lang={c.language} file_ok={c.file_validation_passed} "
                f"attempts={c.attempts} resolved={len(c.accepted_resolution)} chars"
            )
        print(f"\nDRY RUN: {len(all_cases)} case(s) (nothing written)", file=sys.stderr)
        return 0

    written = write_cases(all_cases, args.out)
    print(f"\nwrote {written} case(s) to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
