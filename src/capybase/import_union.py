"""Deterministic Rust import-union editor.

A *post-model-candidate* deterministic pass: when the LLM has resolved a
conflict by copying one side verbatim and thereby dropped an additive Rust
``use`` import from the other side, this module inserts the missing import
leaf mechanically — without a second model call.

This is the first Tier-A primitive of an obligation-driven structural merge
layer. It is:

  - **deterministic:** identical inputs produce identical edits;
  - **idempotent:** a leaf already present is not re-added (re-entry is a no-op);
  - **transactional:** a local-validity failure (brace imbalance, failed leaf
    round-trip) rolls back the entire edit — the original candidate is returned
    untouched;
  - **conservative:** on ANY doubt it returns ``AMBIGUOUS`` rather than a
    guessed merge, and the existing cargo/rustc gauntlet remains the
    authoritative check after the edit.

The editor is *pure of I/O*. It does not call ``rustc`` or ``cargo`` — those
run later in the verification gauntlet. The local pre-commit check is cheap
(brace/paren/bracket balance + leaf round-trip), which is sufficient for the
realistic failure modes of single-group brace editing (the convergence pattern
this targets).

Design references: ``docs/jury-enforcement-deliverable.md`` for the broader
change-accounting system; this module consumes ``BranchObligation`` records
from ``change_accounting.derive_missing_obligations``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Result vocabulary (the primitive contract)
# ---------------------------------------------------------------------------

#: A lossless mechanical operation with no semantic choice. The editor may
#: close the obligation automatically; the existing validation gauntlet
#: (cargo/rustc) remains authoritative for acceptance.
RISK_TIER_A = "A"

#: The primitive's applicability + outcome status.
#:
#:   APPLIED         — the edit was produced; the candidate's resolved_text
#:                      now contains the missing leaf, and the obligation is
#:                      closed (subject to the downstream gauntlet).
#:   NOT_APPLICABLE  — no applicable missing import obligation (e.g. no
#:                      missing imports, or none map to a destination use in
#:                      the candidate). The candidate is untouched; the normal
#:                      preservation → repair flow proceeds unchanged.
#:   BLOCKED         — a local-validity check failed on the edit (brace
#:                      imbalance or failed leaf round-trip). The candidate is
#:                      untouched; the obligation is NOT closed (left for the
#:                      model via the existing delta-completion feedback).
#:   AMBIGUOUS       — applicability could not be established confidently
#:                      (collision, glob interaction, alias rewrite, nested
#:                      group, visibility/cfg mismatch, comment inside a
#:                      group). The candidate is untouched; we do not guess.
STATUS_APPLIED = "APPLIED"
STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"
STATUS_BLOCKED = "BLOCKED"
STATUS_AMBIGUOUS = "AMBIGUOUS"


# ---------------------------------------------------------------------------
# The import leaf record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportLeaf:
    """One canonical import leaf, independent of presentation.

    A ``use`` tree is decomposed into leaves so that ``use util::{A, B}`` and
    ``use util::A; use util::B;`` are recognized as carrying the same two
    leaves. The leaf is the unit of obligation: a missing ``BoxCloneService``
    from ``util::{MapErrLayer, Oneshot}`` is the leaf
    ``util::BoxCloneService`` (binding ``BoxCloneService``).

    Fields:
        path: the full canonical source path as a tuple of segments
              (``("util", "BoxCloneService")``). ``("util",)`` for ``self``.
        binding: the name introduced into local scope. For a rename
                 (``Foo as Bar``) this is ``Bar``. For ``self`` it's the last
                 path segment. For a glob (``*``) it's "" (no single binding).
                 For ``Trait as _`` it's ``"_"`` (a special non-binding alias).
        visibility: ``""`` | ``"pub"`` | ``"pub(crate)"`` (only the ``pub``
                    prefix of the ``use`` statement; we don't model finer
                    visibility than crate).
        cfg: the raw ``#[cfg(...)]`` / ``#[cfg_attr(...)]`` attribute text
             (``""`` when none). Two imports must agree on cfg domain to union.
        kind: ``"name"`` | ``"self"`` | ``"rename"`` | ``"glob"``.
        alias: the ``as`` target (``"Bar"`` for ``Foo as Bar``), else ``""``.
        raw_path_text: the raw path string from the source line, for display
                       and for locating the destination in the candidate. Not
                       canonical (may include whitespace) — use ``path`` for
                       equality.
    """
    path: tuple[str, ...]
    binding: str
    visibility: str
    cfg: str
    kind: str
    alias: str = ""
    raw_path_text: str = ""


# ---------------------------------------------------------------------------
# Use-tree parsing (recursive descent over the brace tree)
# ---------------------------------------------------------------------------

#: Leading visibility prefix. ``pub use``, ``pub(crate) use``.
_VIS_RE = re.compile(r"^\s*(pub(?:\s*\([^)]*\))?\s+)?use\s+")

#: An ``#[...]`` attribute that may precede a ``use`` (``#[cfg(feature="x")]``).
#: We capture the FULL attribute text to compare cfg domains between source
#: and destination. This is intentionally greedy on one attribute; a ``use``
#: with multiple attributes or attributes we can't cleanly pair is rejected
#: (returns None → AMBIGUOUS) rather than risk a mismatched splice.
_ATTR_RE = re.compile(r"^\s*(#[^\n]+?\])\s*")


def _parse_visibility_and_attrs(line: str) -> tuple[str, str, str, str] | None:
    """Split a ``use`` line into (visibility, cfg_attr, body, trailing).

    ``body`` is everything after ``use `` up to the trailing ``;``. The cfg
    attribute is captured so we can compare cfg domains. Returns None when the
    line is not a recognizable single-line ``use`` statement (multi-line use
    trees, malformed). Conservative: any doubt → None → AMBIGUOUS.

    ``trailing`` is the text after the body (the ``;`` plus any whitespace),
    preserved so an edit can reattach it.
    """
    # Capture leading attributes first (they precede visibility).
    cfg = ""
    rest = line
    m = _ATTR_RE.match(rest)
    attr_seen: list[str] = []
    while m:
        attr_seen.append(m.group(1))
        rest = rest[m.end():]
        m = _ATTR_RE.match(rest)
    if attr_seen:
        # We only model a single cfg-family attribute. Multiple attributes or
        # a non-cfg attribute → we can't safely pair cfg domains → AMBIGUOUS.
        if len(attr_seen) != 1:
            return None
        a = attr_seen[0]
        if a.lstrip().startswith("#[cfg") or a.lstrip().startswith("#[cfg_attr"):
            cfg = a.strip()
        else:
            # A non-cfg attribute (e.g. #[macro_use]) — we don't model its
            # semantics; refuse to union rather than risk dropping it.
            return None
    vm = _VIS_RE.match(rest)
    if not vm:
        return None
    vis = (vm.group(1) or "").strip()
    vis = "pub(crate)" if vis.startswith("pub(crate)") else (vis or "").replace("(", "").replace(")", "")
    # Normalize: "pub(crate)" may come through as "pub(crate)"; keep it clean.
    vis_norm = re.sub(r"\s+", "", vis).replace("pub(crate)", "pub(crate)")
    after = rest[vm.end():]
    # Must end with a semicolon (single-line use). A use tree spanning
    # multiple lines won't be a single ``line`` here, but guard anyway.
    semi = after.rfind(";")
    if semi == -1:
        return None
    body = after[:semi].rstrip()
    trailing = after[semi:]
    if "\n" in body:
        return None  # multi-line body inside one logical line — bail
    return vis_norm, cfg, body, trailing


def _split_top_level(s: str, sep: str) -> list[str]:
    """Split ``s`` on ``sep`` at bracket depth 0.

    Used to split the comma-separated items inside a ``use`` brace group
    without splitting on commas inside nested groups (``a::{B::{C, D}, E}``).
    """
    out: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in s:
        if ch in "{([":
            depth += 1
            cur.append(ch)
        elif ch in "})]":
            depth -= 1
            cur.append(ch)
        elif ch == sep and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def _parse_use_tree(
    body: str, prefix: tuple[str, ...], vis: str, cfg: str, raw_prefix: str,
) -> list[ImportLeaf] | None:
    """Recursively parse a use-tree body into leaves.

    ``body`` is the path/tree text (no ``use``, no ``;``). ``prefix`` is the
    accumulated path segments from outer groups. Returns the flat list of
    leaves, or None if the tree can't be fully classified (AMBIGUOUS).

    Recognized forms:
      ``a::b::c``              → name leaf a::b::c
      ``a::b::{X, Y}``         → recurse into the group with prefix (a, b)
      ``a::b::{self, X}``      → self leaf (a, b) + name leaf a::b::X
      ``a::b::self``           → self leaf (a, b)
      ``a::b::c as D``         → rename leaf a::b::c binding D
      ``a::*``                 → glob leaf (a,) binding ""
      ``a::b::{X as Y, Z}``    → rename a::b::X→Y + name a::b::Z
    """
    body = body.strip()
    if not body:
        return None

    # Glob: only at the very end of a path, not inside a rename.
    if body.endswith("*"):
        path_segs = _path_segments(body[:-1])
        if path_segs is None:
            return None
        full = prefix + tuple(path_segs)
        if not full:
            return None
        return [ImportLeaf(
            path=full, binding="", visibility=vis, cfg=cfg,
            kind="glob", raw_path_text=raw_prefix + "::".join(path_segs) + "::*",
        )]

    # Brace group: path::{...}
    # Find the FIRST ``::{`` at depth 0 — the group introducer.
    gidx = _find_group_introducer(body)
    if gidx is not None:
        pre, group_body = body[:gidx], body[gidx:]
        pre_segs = _path_segments(pre)
        if pre_segs is None or not pre_segs:
            return None
        # group_body looks like ``::{A, B}`` or ``:: {A, B}``.
        gm = re.match(r"::\s*\{(.*)\}\s*$", group_body, re.DOTALL)
        if not gm:
            return None
        inner = gm.group(1)
        if "\n" in inner:
            return None  # multi-line group — bail (AMBIGUOUS); v1 is single-line
        items = _split_top_level(inner, ",")
        leaves: list[ImportLeaf] = []
        new_prefix = prefix + tuple(pre_segs)
        new_raw = raw_prefix + "::".join(pre_segs) if raw_prefix else "::".join(pre_segs)
        for item in items:
            sub = _parse_use_tree(item.strip(), new_prefix, vis, cfg, new_raw)
            if sub is None:
                return None
            leaves.extend(sub)
        if not leaves:
            return None
        return leaves

    # Single path, possibly with ``as`` rename.
    # Split off a trailing ``as ALIAS`` at depth 0.
    as_split = _split_top_level_on_as(body)
    if as_split is not None:
        path_part, alias = as_split
        segs = _path_segments(path_part)
        if segs is None:
            return None
        full = prefix + tuple(segs)
        if not full:
            return None
        binding = alias.strip()
        raw = (raw_prefix + ("::" if raw_prefix else "") + "::".join(segs)) if segs else raw_prefix
        return [ImportLeaf(
            path=full, binding=binding, visibility=vis, cfg=cfg,
            kind="rename", alias=binding, raw_path_text=raw,
        )]

    # Plain name or self.
    segs = _path_segments(body)
    if segs is None:
        return None
    full = prefix + tuple(segs)
    if not full:
        return None
    if segs and segs[-1] == "self":
        # ``a::b::self`` → binding is ``b`` (the last real segment). Path is (a, b).
        if len(full) < 2:
            # bare ``self`` → can't form a binding; refuse.
            return None
        real = list(full[:-1])
        return [ImportLeaf(
            path=tuple(real), binding=real[-1], visibility=vis, cfg=cfg,
            kind="self", raw_path_text=raw_prefix + "::".join(segs),
        )]
    binding = segs[-1]
    raw = (raw_prefix + ("::" if raw_prefix else "") + "::".join(segs)) if raw_prefix else "::".join(segs)
    return [ImportLeaf(
        path=full, binding=binding, visibility=vis, cfg=cfg,
        kind="name", raw_path_text=raw,
    )]


def _path_segments(s: str) -> list[str] | None:
    """Split a path on ``::`` into identifier segments.

    Returns None if any segment is empty or contains invalid characters
    (conservative — a malformed path means we don't understand the tree).
    ``self`` and ``crate``/``$crate`` are allowed as segments.
    """
    s = s.strip()
    if not s:
        return None
    segs = re.split(r"::", s)
    out: list[str] = []
    for seg in segs:
        seg = seg.strip()
        if not seg:
            return None
        # An identifier segment. Allow ``self``, ``crate``, ``$crate``,
        # and normal Rust identifiers (incl. raw ``r#name``).
        if seg in ("self", "crate", "$crate"):
            out.append(seg)
            continue
        # ``(?:r#)?`` optionally matches the raw-identifier prefix as a unit
        # (NOT ``r#?``, which would require every ident to start with 'r').
        m = re.fullmatch(r"(?:r#)?[A-Za-z_][A-Za-z0-9_]*", seg)
        if not m:
            return None
        out.append(seg)
    return out


def _find_group_introducer(body: str) -> int | None:
    """Find the index of the first ``::{`` introducer at bracket depth 0.

    Returns the index of the ``::`` (so body[:idx] is the path prefix and
    body[idx:] is ``::{...}``). Returns None when there's no brace group.
    """
    depth = 0
    i = 0
    while i < len(body):
        ch = body[i]
        if ch in "{([":
            depth += 1
        elif ch in "})]":
            depth -= 1
        elif depth == 0 and ch == ":" and body[i:i + 2] == "::":
            # Look ahead for ``{`` (skipping whitespace).
            j = i + 2
            while j < len(body) and body[j].isspace():
                j += 1
            if j < len(body) and body[j] == "{":
                return i
        i += 1
    return None


def _split_top_level_on_as(body: str) -> tuple[str, str] | None:
    """Split ``X as Y`` at depth 0 into (X, Y). None when no ``as``.

    Matches the Rust ``as`` keyword as a whole word, not inside identifiers.
    """
    depth = 0
    i = 0
    while i < len(body):
        ch = body[i]
        if ch in "{([":
            depth += 1
            i += 1
            continue
        if ch in "})]":
            depth -= 1
            i += 1
            continue
        if depth == 0 and body[i:i + 2] == "as" and (
            (i == 0 or not (body[i - 1].isalnum() or body[i - 1] == "_"))
            and (i + 2 >= len(body) or body[i + 2].isspace())
        ):
            path_part = body[:i].rstrip()
            alias = body[i + 2:].strip()
            if path_part and alias:
                return path_part, alias
            return None
        i += 1
    return None


def parse_use_leaves(line: str) -> list[ImportLeaf] | None:
    """Parse a single Rust ``use`` line into canonical leaves.

    Returns the flat list of leaves, or None when the line isn't a
    recognizable single-line ``use`` statement OR contains a construct this
    parser can't fully classify (nested groups, multi-line bodies, malformed
    paths, multiple attributes). None propagates to AMBIGUOUS — we never
    guess at a tree we don't understand.
    """
    if not line or not line.strip():
        return None
    if "use " not in line and "use\t" not in line:
        # Not a use statement at all.
        return None
    parsed = _parse_visibility_and_attrs(line)
    if parsed is None:
        return None
    vis, cfg, body, _trailing = parsed
    return _parse_use_tree(body, (), vis, cfg, "")


# ---------------------------------------------------------------------------
# Local validity checks (cheap, no subprocess)
# ---------------------------------------------------------------------------


def _brackets_balanced(s: str) -> bool:
    """True when (), [], {} are balanced across the whole string."""
    pairs = {")": "(", "]": "[", "}": "{"}
    opens = set("([{")
    depth = {c: 0 for c in "([{"}
    stack: list[str] = []
    # Naive scan — does not skip comments/strings, but for a single ``use``
    # line there are no block comments and strings are pathological. The
    # authoritative parse is rustc; this is the cheap pre-filter.
    for ch in s:
        if ch in opens:
            depth[ch] += 1
            stack.append(ch)
        elif ch in pairs:
            if not stack or stack[-1] != pairs[ch]:
                return False
            stack.pop()
    return not stack


# ---------------------------------------------------------------------------
# The union proposer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportUnionResult:
    """The outcome of :func:`propose_import_union`.

    ``text`` is the edited resolved_text when ``status == APPLIED`` (otherwise
    the original, untouched). ``certificate`` records the transaction for the
    journal/jury.
    """
    status: str
    text: str
    certificate: dict = field(default_factory=dict)


def _leaf_identity(leaf: ImportLeaf) -> tuple:
    """The key a leaf is known by for collision detection.

    Two leaves collide when they introduce the SAME binding from a DIFFERENT
    path (``a::Client`` vs ``b::Client`` both bind ``Client``), or when two
    globs overlap. ``_`` aliases are special: they don't bind a name, so
    multiple ``Trait as _`` coexist (matched by full path, not binding).
    """
    if leaf.kind == "glob":
        # Globs are matched by their full path prefix; two globs on the same
        # prefix are a duplicate, on different prefixes they're an
        # interaction risk we don't model → caller rejects.
        return ("glob", leaf.path)
    if leaf.binding == "_":
        # ``as _`` imports don't bind a user-visible name; coexist by path.
        return ("_as", leaf.path)
    return ("binding", leaf.binding)


def _merge_into_group_line(
    dest_line: str, dest_leaves: list[ImportLeaf], to_add: list[ImportLeaf],
) -> str | None:
    """Add ``to_add`` leaves into an existing ``use PATH::{...}`` line.

    Returns the edited line, or None when the destination isn't a clean FLAT
    single-line brace group we can extend (caller treats None as "fall back
    to a separate line" or AMBIGUOUS).

    This is a SURGICAL splice, not a reconstruction: we find the closing ``}``
    of the target group and insert the new member(s) before it, preserving
    the destination's existing text byte-for-byte. This is critical because
    reconstruction would flatten nested groups (``a::{b::{C,D}, E}`` → wrong
    level). We therefore refuse (return None) when the destination contains
    a nested group — those are left to the model.
    """
    # Guard: only act on FLAT groups. A nested group (``a::{b::{C}, D}``)
    # cannot be safely extended by textual splice because the closing ``}``
    # we'd insert before is the INNER group's, not the outer's. Detected
    # when any dest leaf's path is deeper than prefix + 1 segment, OR the
    # brace-group body itself contains a ``{``.
    parsed = _parse_visibility_and_attrs(dest_line)
    if parsed is None:
        return None
    _vis, _cfg, body, _trailing = parsed
    if "{" in body[body.find("{") + 1:] if "{" in body else "":
        return None  # nested group present → refuse
    # All dest leaves must share exactly ONE path prefix (path[:-1]).
    prefixes = {leaf.path[:-1] for leaf in dest_leaves if leaf.path[:-1]}
    if len(prefixes) != 1:
        return None
    prefix = next(iter(prefixes))
    for leaf in to_add:
        if leaf.path[:-1] != prefix:
            return None  # leaf targets a different group → can't extend this one
    # Idempotency: drop leaves already present in the destination.
    existing_identities = {_leaf_identity(l) for l in dest_leaves}
    fresh = [l for l in to_add if _leaf_identity(l) not in existing_identities]
    if not fresh:
        return None  # nothing new to add (idempotent no-op)
    # Surgical splice: locate the LAST ``}`` in body (the group's closing
    # brace) and insert the new members before it.
    close = body.rfind("}")
    if close == -1:
        return None  # malformed — no closing brace (shouldn't happen post-parse)
    pre = body[:close]
    post = body[close:]
    # Determine the separator: if the content before ``}`` already ends with
    # whitespace/comma, we don't add another comma. Otherwise prepend ", ".
    new_members = ", ".join(_render_group_member(l) for l in fresh)
    if pre.rstrip().endswith(","):
        sep = " "
    else:
        sep = ", "
    # Insert before the closing brace, after any existing trailing whitespace
    # inside the group (``{A, B }`` → ``{A, B, C }``).
    rstripped = pre.rstrip()
    trailing_ws = pre[len(rstripped):]
    new_body = rstripped + sep + new_members + trailing_ws + post
    # Reassemble the full line: visibility + cfg + ``use `` + new_body + trailing.
    head = _reconstruct_head(dest_line)
    if head is None:
        return None
    return head + new_body + _trailing


def _reconstruct_head(line: str) -> str | None:
    """Rebuild the ``[cfg] [vis] use `` prefix of a use line, verbatim order.

    Preserves the attribute + visibility text that preceded ``use`` so the
    surgical group splice doesn't drop them. Returns None when the prefix
    can't be cleanly reconstructed.
    """
    parsed = _parse_visibility_and_attrs(line)
    if parsed is None:
        return None
    vis, cfg, _body, _trailing = parsed
    head = ""
    if cfg:
        head += cfg + " "
    if vis:
        head += vis + " "
    head += "use "
    return head


def _render_group_member(leaf: ImportLeaf) -> str:
    """Render a leaf back to its form inside a ``use PREFIX::{...}`` group.

    The prefix is implied by the group; we render only the tail. ``self`` and
    glob tails are handled by their kind.
    """
    if leaf.kind == "self":
        return "self"
    tail = leaf.path[-1]
    if leaf.kind == "rename" or leaf.alias:
        return f"{tail} as {leaf.alias}"
    if leaf.kind == "glob":
        return "*"
    return tail


def _add_separate_use_line(
    dest_line: str, to_add: list[ImportLeaf], full_text: str,
) -> tuple[str, list[str]] | None:
    """Add ``to_add`` as new ``use`` line(s) adjacent to ``dest_line``.

    Used when the destination is a separate ``use PATH::X;`` (not a brace
    group) or when merging into the group isn't possible. Returns
    (new_full_text, list_of_added_line_texts) or None on failure.
    """
    parsed = _parse_visibility_and_attrs(dest_line)
    if parsed is None:
        return None
    vis, cfg, _body, _trailing = parsed
    lines = full_text.splitlines(keepends=True)
    # Locate the destination line index (exact match).
    dest_idx = None
    dest_stripped = dest_line.rstrip("\n")
    for i, ln in enumerate(lines):
        if ln.rstrip("\n") == dest_stripped:
            dest_idx = i
            break
    if dest_idx is None:
        return None
    added: list[str] = []
    new_lines: list[str] = []
    for leaf in to_add:
        head = ""
        if cfg:
            head += cfg + " "
        if vis:
            head += vis + " "
        path_str = "::".join(leaf.path)
        line_text = f"{head}use {path_str};\n"
        added.append(line_text.rstrip("\n"))
        new_lines.append(line_text)
    # Insert AFTER the destination line (imports cluster together).
    out = lines[:dest_idx + 1] + new_lines + lines[dest_idx + 1:]
    return "".join(out), added


def _render_leaf_line(leaf: ImportLeaf) -> str:
    """Render one leaf as a canonical single-leaf ``use`` line.

    The engine's unit of obligation is the leaf (matching the original:
    obligation lines decompose into leaves and the leaf is the unit of
    union). The rendered line round-trips through ``parse_use_leaves``
    back to the same leaf identity (path, binding, visibility, cfg, kind).
    """
    head = ""
    if leaf.cfg:
        head += leaf.cfg + " "
    if leaf.visibility:
        head += leaf.visibility + " "
    tail = "::".join(leaf.path)
    if leaf.alias:
        tail += f" as {leaf.alias}"
    elif leaf.kind == "glob":
        # path's last segment is the glob's module; render the star form.
        tail = "::".join(leaf.path[:-1]) + "::*" if len(leaf.path) > 1 else "*"
    return f"{head}use {tail};"


def _import_codec():
    """The Rust import codec for the KeyedCollectionMerge engine.

    Stage 3 (the SWITCH, primitive #5 of 5). Carries the exact union
    semantics of the original inline lifecycle, DELEGATING to the
    genuinely language-specific tree machinery
    (``parse_use_leaves`` / ``_merge_into_group_line`` /
    ``_add_separate_use_line``) — the codec is an adapter, not a
    reimplementation:

    - engine items are LEAVES (rendered as canonical single-leaf use
      lines) — the original's unit of obligation;
    - ``already_present`` is the §2 path-keyed pre-filter (a leaf whose
      FULL PATH is in any candidate use line is accounted for; same
      binding from a different path is a collision, not presence);
    - ``try_edit`` re-derives destinations from the RUNNING text (the
      original computed destinations once, but sequential leaf edits
      to the same group only converge if each edit sees the extended
      group — final text identical);
    - the per-destination preconditions gate (visibility / cfg domain /
      binding collision / glob) accumulates on ``preconditions`` for
      the certificate;
    - ``local_validity`` = bracket balance + leaf round-trip over the
      closed paths (the original's transactional check);
    - certificate state (closed paths, edit strings, unresolved paths)
      accumulates on the codec for the adapter.
    """

    class _ImportCodec:
        def __init__(self) -> None:
            self.closed_paths: list[str] = []
            self.edit_strings: list[str] = []
            self.unresolved_paths: list[str] = []
            self.preconditions: dict[str, bool] = {
                "same_visibility": True,
                "same_cfg_domain": True,
                "binding_collision": False,
                "contains_glob": False,
            }
            self._blocked = False

        def applicable_obligations(self, obligations):
            # The parser is the authority on whether a line is an
            # actionable import — NOT the channel label (a
            # ``#[cfg(...)] use`` is classified "directive" but still
            # unions). An exclusive import is a CHOICE — to the model.
            out = []
            for ob in obligations or []:
                if getattr(ob, "operation", "") != "added":
                    continue
                if getattr(ob, "exclusive", False):
                    continue
                line = getattr(ob, "line", "") or ""
                if not line.strip():
                    continue
                leaves = parse_use_leaves(line)
                if leaves is None:
                    continue
                for leaf in leaves:
                    out.append(_render_leaf_line(leaf))
            return out

        def already_present(self, text, item):
            to_add = parse_use_leaves(item)
            if not to_add:
                return True
            leaf = to_add[0]
            existing_paths = set()
            for dl in _find_use_lines(text):
                dl_leaves = parse_use_leaves(dl)
                if dl_leaves:
                    for l in dl_leaves:
                        existing_paths.add(l.path)
            return leaf.path in existing_paths

        def try_edit(self, text, item, context):
            if self._blocked:
                return None
            to_add = parse_use_leaves(item)
            if not to_add:
                return None
            leaf = to_add[0]
            prefix = leaf.path[:-1]
            if not prefix:
                self.unresolved_paths.append("::".join(leaf.path))
                return None
            # A glob import interacts with everything; never auto-union.
            if leaf.kind == "glob":
                self.preconditions["contains_glob"] = True
                self.unresolved_paths.append("::".join(leaf.path))
                return None
            dest_lines = _find_use_lines(text)
            if not dest_lines:
                self.unresolved_paths.append("::".join(leaf.path))
                return None
            dest_line = _best_destination(dest_lines, prefix)
            if dest_line is None:
                self.unresolved_paths.append("::".join(leaf.path))
                return None
            dest_leaves = parse_use_leaves(dest_line)
            if dest_leaves is None:
                self.unresolved_paths.append("::".join(leaf.path))
                return None
            dest_vis = {l.visibility for l in dest_leaves}
            dest_cfg = {l.cfg for l in dest_leaves}
            # Visibility + SYMMETRIC cfg-domain agreement (a gated sibling
            # vs an unconditional leaf changes semantics — refuse).
            if leaf.visibility not in dest_vis:
                self.preconditions["same_visibility"] = False
            if leaf.cfg not in dest_cfg:
                self.preconditions["same_cfg_domain"] = False
            if not self.preconditions["same_visibility"] \
                    or not self.preconditions["same_cfg_domain"]:
                self.unresolved_paths.append("::".join(leaf.path))
                return None
            # Binding collision: the candidate already binds the same name
            # from a DIFFERENT path.
            existing_bindings: dict[str, tuple] = {}
            for dl in dest_leaves:
                ident = _leaf_identity(dl)
                if ident[0] == "binding":
                    existing_bindings[ident[1]] = dl.path
            ident = _leaf_identity(leaf)
            if ident[0] == "binding":
                existing_path = existing_bindings.get(ident[1])
                if existing_path is not None and existing_path != leaf.path:
                    self.preconditions["binding_collision"] = True
            if self.preconditions["binding_collision"]:
                self.unresolved_paths.append("::".join(leaf.path))
                return None

            # Prefer extending an existing brace group...
            new_line = _merge_into_group_line(dest_line, dest_leaves, [leaf])
            if new_line is not None:
                lines = text.splitlines(keepends=True)
                dest_stripped = dest_line.rstrip("\n")
                for i, ln in enumerate(lines):
                    if ln.rstrip("\n") == dest_stripped:
                        ending = "\n" if ln.endswith("\n") else ""
                        start = sum(len(l) for l in lines[:i])
                        self.edit_strings.append(new_line.strip())
                        self.closed_paths.append("::".join(leaf.path))
                        return (start, start + len(ln),
                                new_line.rstrip("\n") + ending)
                return None  # destination vanished — unresolved
            # ...fall back to a separate line adjacent to the destination.
            result = _add_separate_use_line(dest_line, [leaf], text)
            if result is None:
                self.unresolved_paths.append("::".join(leaf.path))
                return None
            new_text, added = result
            # Derive the insertion span: exactly one line was inserted
            # after the destination (position = end of dest line).
            lines = text.splitlines(keepends=True)
            dest_stripped = dest_line.rstrip("\n")
            for i, ln in enumerate(lines):
                if ln.rstrip("\n") == dest_stripped:
                    pos = sum(len(l) for l in lines[:i + 1])
                    pos = min(pos, len(text))
                    line_text = added[0] + "\n" if added else ""
                    self.edit_strings.extend(added)
                    self.closed_paths.append("::".join(leaf.path))
                    return (pos, pos, line_text)
            return None

        def local_validity(self, text):
            ok = _brackets_balanced(text) and \
                _roundtrip_confirms(text, self.closed_paths)
            if not ok:
                self._blocked = True
            return ok

    return _ImportCodec()


def propose_import_union(
    resolved_text: str, missing_obligations: list,
) -> ImportUnionResult:
    """Propose a deterministic import-union edit on a merge candidate.

    Args:
        resolved_text: the candidate's current resolved_text (the file the
            model produced — typically a verbatim copy of one conflict side).
        missing_obligations: ``BranchObligation`` records from
            ``change_accounting.derive_missing_obligations``. Only the
            additive (non-exclusive) executable ``added`` import lines are
            candidates for union; everything else is ignored.

    Stage 3 (the SWITCH): the KeyedCollectionMerge engine runs the
    lifecycle; this function is a thin adapter mapping the engine's
    :class:`PrimitiveResult` to the wire format (original certificate
    keys, ``rust.use_leaf_union/v1``). The use-tree parsing and
    group-merging stay in this module — the codec delegates to them.

    Returns an :class:`ImportUnionResult`. The function NEVER raises on
    unrecognized input — it returns NOT_APPLICABLE / AMBIGUOUS / BLOCKED so
    the caller's control flow is undisturbed. Internal errors are caught and
    mapped to BLOCKED (transactional rollback).
    """
    from capybase.keyed_collection import merge_keyed_collection, to_wire_result

    codec = _import_codec()
    result = merge_keyed_collection(
        codec, resolved_text, missing_obligations,
        other_side_text="",
        mechanism_id="rust.use_leaf_union/v1",
    )

    def _applied_cert(r):
        return {
            "closed_obligations": list(codec.closed_paths),
            "remaining_obligations": len(codec.unresolved_paths),
            "edits": list(codec.edit_strings),
            "preconditions": dict(codec.preconditions),
            "risk_tier": RISK_TIER_A,
            "skipped_lines": [],
            "unresolved": list(codec.unresolved_paths),
        }

    return to_wire_result(
        result, resolved_text,
        mechanism_id="rust.use_leaf_union/v1",
        reason_map={
            "no applicable items": "no additive import obligations",
            "all items already present (idempotent)":
                "all leaves already present (idempotent)",
            "no items could be safely inserted":
                "no leaves could be safely unioned",
            "local validity check failed":
                "local validity check failed (brace imbalance or round-trip)",
        },
        applied_cert=_applied_cert)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_use_lines(text: str) -> list[str]:
    """All single-line ``use`` statements in ``text`` (one string per line).

    Recognizes ``use``, ``pub use``, ``pub(crate) use``, AND lines with a
    leading ``#[cfg(...)]`` / ``#[cfg_attr(...)]`` attribute (the attribute
    precedes the visibility/use keywords). Detection uses the parser's own
    visibility+attribute matcher so the two stay in sync.
    """
    out: list[str] = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        # The authoritative parse already handles attributes + visibility;
        # if it accepts the line, it's a single-line use statement we model.
        if _parse_visibility_and_attrs(s) is None:
            continue
        if _is_single_line_use(s):
            out.append(ln)
    return out


def _is_single_line_use(line: str) -> bool:
    """True when the ``use`` line is self-contained (balanced braces)."""
    # A multi-line opener like ``use foo::{`` has unbalanced ``{``.
    depth = 0
    for ch in line:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
    return depth == 0 and ";" in line


def _best_destination(dest_lines: list[str], prefix: tuple[str, ...]) -> str | None:
    """Find the destination ``use`` line whose path best matches ``prefix``.

    A leaf ``util::BoxCloneService`` (prefix ``("util",)``) matches a dest
    ``use util::{MapErrLayer, Oneshot};`` (path prefix ``("util",)``). When
    multiple destinations match, prefer the one with the longest matching
    prefix (most specific). When none match, return None.
    """
    best: str | None = None
    best_len = -1
    for dl in dest_lines:
        leaves = parse_use_leaves(dl)
        if not leaves:
            continue
        for leaf in leaves:
            dest_prefix = leaf.path[:-1] if leaf.path[:-1] else leaf.path
            # Exact prefix match (the leaf shares the destination's path head).
            if leaf.path[:len(prefix)] == prefix or prefix == dest_prefix:
                match_len = len(prefix)
                if match_len > best_len:
                    best = dl
                    best_len = match_len
                    break
    return best


def _replace_line(text: str, old_line: str, new_line: str) -> tuple[str, bool]:
    """Replace the first occurrence of ``old_line`` with ``new_line``.

    Returns (new_text, success). Fails (returns original, False) when the old
    line isn't found exactly.
    """
    lines = text.splitlines(keepends=True)
    old_stripped = old_line.rstrip("\n")
    for i, ln in enumerate(lines):
        if ln.rstrip("\n") == old_stripped:
            # Preserve the original line ending.
            ending = "\n" if ln.endswith("\n") else ""
            lines[i] = new_line.rstrip("\n") + ending
            return "".join(lines), True
    return text, False


def _roundtrip_confirms(text: str, closed_paths: list[str]) -> bool:
    """Verify every closed path is now present as a leaf in some ``use`` line."""
    all_paths: set[str] = set()
    for dl in _find_use_lines(text):
        leaves = parse_use_leaves(dl)
        if not leaves:
            continue
        for leaf in leaves:
            all_paths.add("::".join(leaf.path))
    return all(p in all_paths for p in closed_paths)


__all__ = [
    "ImportLeaf",
    "ImportUnionResult",
    "parse_use_leaves",
    "propose_import_union",
    "RISK_TIER_A",
    "STATUS_APPLIED", "STATUS_NOT_APPLICABLE", "STATUS_BLOCKED", "STATUS_AMBIGUOUS",
]
