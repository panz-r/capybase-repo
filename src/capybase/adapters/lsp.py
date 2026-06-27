"""External language-server / type-checker adapters.

Runs external tools (pyright, rust-analyzer/cargo check) on candidate-resolved
source and returns structured diagnostics. These are the deterministic
verification backends behind the ``LspDiagnosticsValidator``: an LLM is a
probabilistic guesser that must be paired with an unforgiving checker.

All tools are assumed installed system-wide (not bundled). Every function
degrades gracefully — if the tool is absent or the command fails, an empty
diagnostic list is returned so the validator can report "not checked" rather
than crash. This keeps capybase functional on minimal installs and in CI
without the toolchain.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class Diagnostic:
    """One language-server diagnostic.

    Line/column are 0-based to match capybase's convention. ``severity`` is
    the tool's level (``"error"``, ``"warning"``, ``"information"`` ...);
    callers gate on ``"error"``.
    """

    severity: str
    message: str
    line: int = 0
    column: int = 0
    code: str = ""
    source: str = ""  # which tool produced it


@dataclass
class Diagnostics:
    """The result of checking one source file."""

    checked: bool = False  # False if the tool was absent/failed to run
    tool: str = ""
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "error"]

    @property
    def error_count(self) -> int:
        return len(self.errors)


class LspRunner(Protocol):
    """Check source and return diagnostics. Pluggable for testing/languages."""

    def check(self, source: str, *, path: str, repo_root: str) -> Diagnostics: ...


# ---------------------------------------------------------------------------
# Python: pyright (falls back to py_compile message-only)
# ---------------------------------------------------------------------------


class PyrightRunner:
    """Run pyright on a temp file and parse its JSON output.

    Pyright reports type errors, undefined names, and similar issues that
    py_compile cannot. It is the primary Python backend when installed. The
    temp file is written next to nothing (a standalone temp path) so pyright
    doesn't pick up unrelated project config — we want diagnostics for THIS
    source only.
    """

    def __init__(self, pyright_path: str = "pyright", *, timeout: int = 60) -> None:
        self.pyright_path = pyright_path
        self.timeout = timeout

    def check(self, source: str, *, path: str, repo_root: str) -> Diagnostics:
        exe = _resolve(self.pyright_path)
        if exe is None:
            return Diagnostics(checked=False, tool="pyright")
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as tf:
            tf.write(source)
            tmp = tf.name
        try:
            proc = subprocess.run(
                [exe, "--outputjson", tmp],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=repo_root,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return Diagnostics(checked=False, tool="pyright")
        finally:
            Path(tmp).unlink(missing_ok=True)
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return Diagnostics(checked=False, tool="pyright")
        diags: list[Diagnostic] = []
        for d in data.get("generalDiagnostics", []):
            rng = d.get("range", {}).get("start", {})
            diags.append(
                Diagnostic(
                    severity=str(d.get("severity", "warning")).lower(),
                    message=str(d.get("message", "")),
                    line=int(rng.get("line", 0)),
                    column=int(rng.get("character", 0)),
                    code=str(d.get("rule", "")),
                    source="pyright",
                )
            )
        return Diagnostics(checked=True, tool="pyright", diagnostics=diags)


# ---------------------------------------------------------------------------
# Rust: rust-analyzer diagnostics (via CLI) or cargo check
# ---------------------------------------------------------------------------


class RustAnalyzerRunner:
    """Check Rust source with rust-analyzer or cargo check.

    For a standalone ``.rs`` file we try ``rust-analyzer diagnostics``. For a
    file inside a cargo project (``Cargo.toml`` present), ``cargo check`` is
    more reliable — it invokes rustc's analysis and reports compilation
    errors/warnings as structured JSON. The runner auto-detects which to use.
    """

    def __init__(
        self,
        rust_analyzer_path: str = "rust-analyzer",
        cargo_path: str = "cargo",
        *,
        timeout: int = 120,
    ) -> None:
        self.rust_analyzer_path = rust_analyzer_path
        self.cargo_path = cargo_path
        self.timeout = timeout

    def check(self, source: str, *, path: str, repo_root: str) -> Diagnostics:
        # Prefer cargo check if this is part of a cargo project.
        if _has_cargo_manifest(repo_root):
            return self._check_cargo(source, path, repo_root)
        return self._check_rust_analyzer(source, path, repo_root)

    def _check_cargo(
        self, source: str, path: str, repo_root: str
    ) -> Diagnostics:
        cargo = _resolve(self.cargo_path)
        if cargo is None:
            return Diagnostics(checked=False, tool="cargo")
        # Write the resolved source into the actual file path so cargo sees it.
        target_path = Path(repo_root) / path
        original: bytes | None = None
        if target_path.exists():
            original = target_path.read_bytes()
        try:
            target_path.write_text(source, encoding="utf-8")
            proc = subprocess.run(
                [cargo, "check", "--message-format=json", "--quiet"],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=repo_root,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return Diagnostics(checked=False, tool="cargo")
        finally:
            if original is not None:
                target_path.write_bytes(original)
        diags = _parse_cargo_messages(proc.stdout, path)
        return Diagnostics(checked=True, tool="cargo", diagnostics=diags)

    def _check_rust_analyzer(
        self, source: str, path: str, repo_root: str
    ) -> Diagnostics:
        exe = _resolve(self.rust_analyzer_path)
        if exe is None:
            return Diagnostics(checked=False, tool="rust-analyzer")
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".rs", delete=False, encoding="utf-8"
        ) as tf:
            tf.write(source)
            tmp = tf.name
        try:
            proc = subprocess.run(
                [exe, "diagnostics", tmp],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=repo_root,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return Diagnostics(checked=False, tool="rust-analyzer")
        finally:
            Path(tmp).unlink(missing_ok=True)
        diags = _parse_rust_analyzer_output(proc.stdout, tmp)
        return Diagnostics(checked=True, tool="rust-analyzer", diagnostics=diags)


def _parse_cargo_messages(stdout: str, path: str) -> list[Diagnostic]:
    """Parse cargo's ``--message-format=json`` lines into diagnostics.

    cargo emits one JSON object per line; compiler messages have
    ``reason: "compiler-message"`` with a nested ``message`` containing
    ``level``, ``message``, and ``spans`` (with file/line info).
    """
    diags: list[Diagnostic] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("reason") != "compiler-message":
            continue
        inner = msg.get("message", {})
        spans = inner.get("spans", [])
        line_no = 0
        col = 0
        # Prefer the primary span.
        for sp in spans:
            if sp.get("is_primary"):
                line_no = int(sp.get("line_start", 1)) - 1  # cargo is 1-based
                col = int(sp.get("column_start", 1)) - 1
                break
        diags.append(
            Diagnostic(
                severity=str(inner.get("level", "warning")).lower(),
                message=str(inner.get("message", "")),
                line=line_no,
                column=col,
                code=str((inner.get("code") or {}).get("code", "")),
                source="cargo",
            )
        )
    return diags


def _parse_rust_analyzer_output(stdout: str, tmp_path: str) -> list[Diagnostic]:
    """Parse ``rust-analyzer diagnostics`` text output.

    The CLI emits lines like ``<file>:<line>:<col> <severity>: <message>``.
    This is a best-effort parse; the cargo JSON path is preferred when
    available.
    """
    diags: list[Diagnostic] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or tmp_path not in line:
            continue
        # Strip the file prefix.
        rest = line.split(":", 3)
        if len(rest) < 4:
            continue
        try:
            ln = int(rest[1]) - 1 if rest[1].isdigit() else 0
            col = int(rest[2]) - 1 if rest[2].isdigit() else 0
        except (ValueError, IndexError):
            ln, col = 0, 0
        msg_part = rest[3].strip()
        severity = "warning"
        message = msg_part
        for sev in ("error", "warning"):
            idx = msg_part.lower().find(sev)
            if idx != -1:
                severity = sev
                message = msg_part[idx + len(sev) :].lstrip(": ").strip()
                break
        diags.append(
            Diagnostic(
                severity=severity,
                message=message,
                line=ln,
                column=col,
                source="rust-analyzer",
            )
        )
    return diags


# ---------------------------------------------------------------------------
# Dispatch + helpers
# ---------------------------------------------------------------------------


def runner_for(language: str | None, *, config: "LspConfig | None" = None) -> LspRunner | None:
    """Return an LspRunner for ``language`` or None if unsupported.

    ``config`` supplies tool paths; when None, defaults are used. The caller is
    responsible for deciding whether to actually invoke the runner (based on
    config flags). Returning a runner does not guarantee the tool is installed
    — the runner reports ``checked=False`` if the binary is missing at run time.
    """
    if language == "python":
        path = (config.pyright_path if config else "pyright")
        return PyrightRunner(path)
    if language == "rust":
        return RustAnalyzerRunner(
            rust_analyzer_path=config.rust_analyzer_path if config else "rust-analyzer",
            cargo_path=config.cargo_path if config else "cargo",
        )
    return None


def _resolve(cmd: str) -> str | None:
    """Return the executable path if ``cmd`` is runnable, else None."""
    # Allow absolute paths and PATH lookups. Do NOT run the binary — just check
    # it exists, so a missing tool is cheap to detect.
    if os.path.isabs(cmd):
        return cmd if os.path.isfile(cmd) and os.access(cmd, os.X_OK) else None
    from shutil import which

    return which(cmd)


def _has_cargo_manifest(repo_root: str) -> bool:
    return (Path(repo_root) / "Cargo.toml").exists()


def run_clippy(
    repo_root: str, *, cargo_path: str = "cargo", deny_warnings: bool = True,
    timeout: int = 180,
) -> Diagnostics:
    """Run ``cargo clippy`` on the crate and return its diagnostics.

    Clippy emits the SAME ``--message-format=json`` format as ``cargo check``
    (``reason: compiler-message`` with ``message.level``/``message.message``),
    so diagnostics parse identically via ``_parse_cargo_messages``. By default
    ``-D warnings`` is passed so lint findings surface as errors (clippy
    otherwise exits 0 even with warnings); pass ``deny_warnings=False`` to keep
    them as warnings. Runs against the CURRENT worktree state — the caller
    (Phase B) has already written every resolved file, so the whole crate is
    checked. Returns ``checked=False`` (never raises) when cargo is absent,
    there's no Cargo.toml, or the invocation fails.
    """
    cargo = _resolve(cargo_path)
    if cargo is None or not _has_cargo_manifest(repo_root):
        return Diagnostics(checked=False, tool="clippy")
    argv = [cargo, "clippy", "--message-format=json", "--quiet"]
    if deny_warnings:
        argv += ["--", "-D", "warnings"]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, cwd=repo_root,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return Diagnostics(checked=False, tool="clippy")
    # Clippy always produces JSON regardless of exit code; parse the messages.
    diags = _parse_cargo_messages(proc.stdout, "")
    return Diagnostics(checked=True, tool="clippy", diagnostics=diags)


@dataclass
class LspConfig:
    """Paths and toggles for the LSP runners."""

    pyright_path: str = "pyright"
    rust_analyzer_path: str = "rust-analyzer"
    cargo_path: str = "cargo"
    enable_lsp_diagnostics: bool = False
    # Reject only NEW diagnostics (not present in the pre-conflict baseline).
    lsp_baseline_strict: bool = True
    enable_shadow_tests: bool = False
