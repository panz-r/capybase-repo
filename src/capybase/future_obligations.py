"""Future obligations — what later source commits expect of this resolution.

Side obligations (#3, :mod:`obligations`) capture what the two conflicting sides
demand of the resolution. *Future* obligations capture what the REST of the
source branch demands: a later replayed commit may rename a symbol the resolution
defines, edit a config key, or import a helper the region provides. A resolution
that's locally valid (satisfies both sides) but deletes ``parse_config`` while a
later commit still calls ``parse_config`` would break that later commit's replay.

These are derived structurally from future source-commit patches — never from the
LLM — so they're a trustworthy guardrail the model can't talk itself out of.

Three obligation kinds:

- **symbol survival**: a future commit references a name the resolution defines
  (call/import/reference) → the resolution must keep it. "later commit X expects
  ``parse_config`` to still exist."
- **key edits**: a future commit edits a config key inside the region → the
  resolution should keep the key. "later commit X modifies key ``strict_mode``."
- **imports**: a future commit imports a name the region defines → "later commit
  X imports ``normalize_path``."

The module is pure: :func:`extract_future_obligations` takes a RegionKey, the
future ReplayCommits touching the region, their patches (bytes), and the
resolution's defined symbols; returns a :class:`FutureObligations`. The
validation (:func:`obligations_satisfied`) checks a candidate's resolved text
against them and rejects a candidate that drops a survival/required obligation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from capybase.history import ReplayCommit

#: Cap the number of obligations derived per conflict (keeps the prompt/report
#: small; the LLM doesn't need 20 future obligations, the top few carry the
#: signal). Match the obligations.py summary truncation philosophy.
_MAX_OBLIGATIONS = 8


@dataclass(frozen=True)
class FutureObligation:
    """One thing a later source commit expects of the resolution.

    ``kind`` is ``symbol_survival`` / ``key_edit`` / ``import``. ``symbol`` is
    the name the later commit depends on (empty for key edits, which carry
    ``key`` instead). ``commit_subject`` is the depending commit's subject for
    the report; ``required`` marks obligations a candidate MUST satisfy (symbol
    survival/imports are required; key edits are advisory — a key edit might be
    superseded, but a deleted-then-referenced symbol is a hard break).
    """

    kind: str
    symbol: str = ""
    key: str = ""
    commit_subject: str = ""
    required: bool = True

    def render(self) -> str:
        """One-line human rendering for prompts/reports."""
        s = self.commit_subject or "a later commit"
        if self.kind == "symbol_survival":
            return f"later commit \"{s}\" expects `{self.symbol}` to still exist"
        if self.kind == "import":
            return f"later commit \"{s}\" imports `{self.symbol}`"
        if self.kind == "key_edit":
            return f"later commit \"{s}\" modifies key `{self.key}`"
        return f"later commit \"{s}\" depends on `{self.symbol or self.key}`"


@dataclass(frozen=True)
class FutureObligations:
    """The set of future obligations for one conflict's resolution."""

    obligations: list[FutureObligation] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.obligations

    @property
    def required_symbols(self) -> set[str]:
        """The symbol names a candidate MUST keep (survival + imports)."""
        return {
            o.symbol for o in self.obligations
            if o.required and o.kind in ("symbol_survival", "import") and o.symbol
        }

    @property
    def expected_keys(self) -> set[str]:
        """Config keys the later commits edit (advisory)."""
        return {o.key for o in self.obligations if o.kind == "key_edit" and o.key}

    def render_block(self) -> str:
        """A prompt block. Empty when no obligations (caller omits)."""
        if self.empty:
            return ""
        lines = ["Future obligations (later source commits expect these — preserve them):"]
        for o in self.obligations[:_MAX_OBLIGATIONS]:
            lines.append(f"  - {o.render()}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# symbol extraction (lightweight; no full AST needed for the survival check)
# ---------------------------------------------------------------------------

# Names a future patch REFERENCES (calls/uses). We scan added lines (``+``) for
# identifier uses — a definition in the future commit itself doesn't count as a
# dependency on our resolution. This is intentionally recall-biased: it's better
# to over-derive a survival obligation (and require the candidate keep a symbol)
# than to miss one and break a later commit. The candidate re-validation is the
# backstop; an over-derived obligation just rejects a deletion that *might* be
# fine, falling through to the LLM (safe).
_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"

# Definitions introduced BY the future commit (so we can exclude self-definitions
# from the "expects to exist" set — a future commit defining its own helper
# doesn't depend on our resolution for it).
_DEF_RE = re.compile(
    r"^\s*(?:pub\s+|async\s+)*(?:fn|def|struct|enum|trait|class|const|static|let)\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)"
)
# Imports a future commit adds: ``from x import y`` / ``import x.y as z`` /
# ``use crate::y`` / ``use self::y``. The imported name may be one we define.
_PY_IMPORT_RE = re.compile(
    r"^\s*from\s+\S+\s+import\s+(.+)|^\s*import\s+([A-Za-z_][\w.]*(?:\s+as\s+(\w+))?)"
)
_RS_USE_RE = re.compile(r"^\s*use\s+(.+?);")


def _defined_symbols(resolved_text: str) -> set[str]:
    """Callable/type names the resolution DEFINES (functions/classes/structs/etc.).

    Uses the shared signature extractor so Python and Rust definitions are both
    caught with the codebase's existing patterns. Deliberately EXCLUDES plain
    ``NAME =`` assignments: those are config values, not symbols a later commit
    "depends on surviving" — a future commit reassigning ``strict_mode`` is a key
    edit (advisory), not a survival obligation. Only callables/types qualify,
    because deleting/renaming a function a later commit calls is a hard break.
    """
    try:
        from capybase.resolution_engine import _extract_signatures

        sigs = _extract_signatures(resolved_text)
    except Exception:  # noqa: BLE001 - defensive
        return set()
    out: set[str] = set()
    for s in sigs:
        # Signatures look like "fn: name", "def: name", "test: name", etc.
        parts = s.split(":", 1)
        name = parts[1].strip() if len(parts) == 2 else s.strip()
        if name:
            out.add(name)
    return out


def _patch_added_text(patch: bytes) -> str:
    """The added (``+``) lines of a unified diff, as text (no ``+`` prefix)."""
    if not patch:
        return ""
    text = patch.decode("utf-8", errors="replace")
    out: list[str] = []
    for line in text.split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            out.append(line[1:])
    return "\n".join(out)


def _references_in_added(added: str, defined: set[str]) -> set[str]:
    """Names from ``defined`` that appear as references in the added lines.

    Excludes lines that DEFINE the name themselves (a future commit adding its
    own ``def foo`` doesn't depend on our resolution for ``foo``).
    """
    if not defined or not added:
        return set()
    self_defined: set[str] = set()
    for line in added.split("\n"):
        m = _DEF_RE.match(line)
        if m:
            self_defined.add(m.group(1))
    candidates = defined - self_defined
    if not candidates:
        return set()
    # Find all identifiers in the added text, intersect with candidates.
    found = set(re.findall(_IDENT, added))
    return candidates & found


def _imports_from_added(added: str, defined: set[str]) -> set[str]:
    """Names from ``defined`` that a future commit IMPORTS."""
    if not defined or not added:
        return set()
    imported: set[str] = set()
    for line in added.split("\n"):
        pym = _PY_IMPORT_RE.match(line)
        if pym:
            # ``from x import a, b`` or ``import x.y as z``.
            names = pym.group(1) or pym.group(2) or ""
            for n in re.split(r"[,\s]+", names):
                n = n.strip().split(".")[-1]
                if n and n in defined:
                    imported.add(n)
        rsm = _RS_USE_RE.match(line)
        if rsm:
            tail = rsm.group(1).split("::")[-1].strip()
            if tail in defined:
                imported.add(tail)
    return imported


_KEY_RE = re.compile(rf"^\s*({_IDENT})\s*[:=]")


def _keys_in_added(added: str) -> set[str]:
    """Config keys the future patch edits (``key:`` / ``key =`` assignments)."""
    keys: set[str] = set()
    for line in added.split("\n"):
        m = _KEY_RE.match(line)
        if m:
            keys.add(m.group(1))
    return keys


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def extract_future_obligations(
    *,
    resolved_text: str,
    future_commits: "list[ReplayCommit]",
    patches: "dict[str, bytes]",
) -> FutureObligations:
    """Derive future obligations from the future source commits' patches.

    Args:
        resolved_text: the candidate resolution text (to learn what symbols it
            defines — only names the resolution PROVIDES can be "expected to
            survive" by a later commit).
        future_commits: the region-touching future commits (oldest-first).
        patches: a mapping from commit OID → its path-filtered patch bytes. The
            caller fetches these via ``git.commit_patch``; passing them in keeps
            this function pure and testable without a repo.

    Returns a :class:`FutureObligations`, possibly empty when no future commit
    depends on anything the resolution defines. Never raises.
    """
    if not future_commits or not resolved_text:
        return FutureObligations()
    try:
        defined = _defined_symbols(resolved_text)
    except Exception:  # noqa: BLE001
        return FutureObligations()
    if not defined:
        # The region defines no named symbols (e.g. a config block); we can
        # still derive key-edit obligations, but no symbol-survival ones.
        defined = set()

    obligations: list[FutureObligation] = []
    seen: set[tuple[str, str]] = set()  # (kind, name) dedup
    for commit in future_commits:
        patch = patches.get(commit.oid) or b""
        added = _patch_added_text(patch)
        if not added:
            continue
        subject = commit.subject
        # Symbol survival: the future commit references a name we define.
        for sym in sorted(_references_in_added(added, defined)):
            key = ("symbol_survival", sym)
            if key in seen:
                continue
            seen.add(key)
            obligations.append(FutureObligation(
                kind="symbol_survival", symbol=sym,
                commit_subject=subject, required=True,
            ))
        # Imports: the future commit imports a name we define.
        for sym in sorted(_imports_from_added(added, defined)):
            key = ("import", sym)
            if key in seen:
                continue
            seen.add(key)
            obligations.append(FutureObligation(
                kind="import", symbol=sym,
                commit_subject=subject, required=True,
            ))
        # Key edits: the future commit edits a config key (advisory).
        for key_name in sorted(_keys_in_added(added)):
            ko = ("key_edit", key_name)
            if ko in seen:
                continue
            seen.add(ko)
            obligations.append(FutureObligation(
                kind="key_edit", key=key_name,
                commit_subject=subject, required=False,
            ))
    return FutureObligations(obligations=obligations[:_MAX_OBLIGATIONS])


def obligations_satisfied(
    obligations: FutureObligations, resolved_text: str
) -> tuple[bool, list[str]]:
    """Check a candidate's resolved text against the future obligations.

    Returns ``(satisfied, dropped)``. ``dropped`` lists the required symbol names
    the candidate no longer defines (a survival/import obligation violated). Key
    edits are advisory and never flag a drop. Symbol survival is checked by
    re-deriving the candidate's defined names: if a required symbol is missing,
    the candidate deleted/renamed it and would break the later commit.
    """
    required = obligations.required_symbols
    if not required:
        return True, []
    try:
        candidate_defs = _defined_symbols(resolved_text)
    except Exception:  # noqa: BLE001
        return True, []  # can't tell → don't block (advisory)
    dropped = sorted(required - candidate_defs)
    return (not dropped, dropped)
