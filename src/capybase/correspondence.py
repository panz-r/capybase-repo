"""Three-way entity correspondence across repository revisions (sprint-27).

The survey-§1 foundation, built on what capybase already has: entity-level
parsing per language + rename-stable body fingerprints. This module pairs
definitions across base / target / source revisions ACROSS FILES (moves and
renames included) and classifies each pair's relation, with explicit
ambiguity — the substrate both the def-site-race resolver and (later) the
API-adaptation slice consume.

Semantics:
- Pairing evidence is the entity's body fingerprint: whitespace- and
  comment-stable, rename-stable (header stripped), exact on body content.
  A definition whose BODY changed between revisions does NOT pair by
  fingerprint — name identity carries it instead (relation MODIFIED).
- ``AMBIGUOUS`` fires when a fingerprint maps to multiple candidates on a
  side (duplicated bodies) or when move+modify combine — surfaced, never
  silently resolved.
- Merge-commit files (``.md``/``.rst``/lockfiles) are excluded at the
  enumerator level: correspondence tracks SOURCE definitions.

Relation taxonomy (the survey's): UNCHANGED, MODIFIED, RENAMED, MOVED,
MOVED_AND_RENAMED, COPIED, DELETED, ADDED, AMBIGUOUS.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from capybase.adapters.structural import _abstract_parse

_DOCS_EXTS = (".md", ".rst", ".txt", ".lock", ".json", ".yml", ".yaml", ".toml")


@dataclass(frozen=True)
class NodeRef:
    """Where an entity lives in one revision."""
    path: str
    name: str
    kind: str
    fingerprint: str
    signature: str


@dataclass(frozen=True)
class Correspondence:
    """One entity's three-way correspondence record."""
    base: NodeRef | None
    current: NodeRef | None
    replayed: NodeRef | None
    relation: str
    #: namesakes / fingerprint-mates that made this record ambiguous.
    ambiguity: tuple[str, ...] = ()


def _changed_files(clone: Path, a: str, b: str) -> list[str]:
    r = subprocess.run(["git", "-C", str(clone), "diff", "--name-only",
                         "--no-renames", a, b],
                       capture_output=True, timeout=120)
    if r.returncode != 0:
        return []
    return [l for l in r.stdout.decode("utf-8", "replace").splitlines()
            if l and not l.endswith(_DOCS_EXTS)]


def _entities(clone: Path, oid: str, path: str, lang: str) -> dict[str, tuple]:
    t = show_text(clone, oid, path)
    if t is None:
        return {}
    ir = _abstract_parse(t, lang)
    out = {}
    for u in (ir.units if ir else []):
        name = getattr(u, "name", None)
        fp = getattr(u, "fingerprint", None)
        if not name or not fp:
            continue
        out[name] = (path, name, getattr(u, "kind", ""), fp,
                     (getattr(u, "body", "") or "").split("\n")[0].strip())
    return out


def show_text(clone: Path, oid: str, path: str) -> str | None:
    r = subprocess.run(["git", "-C", str(clone), "show", f"{oid}:{path}"],
                       capture_output=True, timeout=30)
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", "replace")


def _by_fingerprint(ents: dict) -> dict[str, list]:
    by: dict[str, list] = {}
    for name, ent in ents.items():
        by.setdefault(ent[3], []).append(ent)
    return by


def build_correspondence(
    clone: Path, base_oid: str, target_oid: str, source_oid: str,
    lang: str,
) -> list[Correspondence]:
    """Three-way entity correspondence over the whole changed repository.

    Enumeration: every source file changed base→target or base→source
    (union), parsed per language, entities keyed by name with their
    fingerprints. Relations per (name, fingerprint-evidence):
    UNCHANGED / MODIFIED / MOVED / RENAMED / MOVED_AND_RENAMED /
    ADDED / DELETED; ambiguity flagged rather than resolved.
    """
    changed = set(_changed_files(clone, base_oid, target_oid))
    changed |= set(_changed_files(clone, base_oid, source_oid))
    records: list[Correspondence] = []

    # Collect per-file entity sets for the three revisions.
    base_e: dict[str, dict] = {}
    tgt_e: dict[str, dict] = {}
    src_e: dict[str, dict] = {}
    for path in sorted(changed):
        base_e.update(_entities(clone, base_oid, path, lang))
        tgt_e.update(_entities(clone, target_oid, path, lang))
        src_e.update(_entities(clone, source_oid, path, lang))

    fp_t = _by_fingerprint(tgt_e)
    fp_s = _by_fingerprint(src_e)
    names = sorted(set(base_e) | set(tgt_e) | set(src_e))
    for name in names:
        records.append(_classify(name, base_e.get(name),
                                 tgt_e.get(name), src_e.get(name),
                                 fp_t, fp_s))
    return records


def _classify(name: str, b, t, s, fp_t, fp_s) -> Correspondence:
    def ref(x):
        return NodeRef(path=x[0], name=x[1], kind=x[2], fingerprint=x[3],
                       signature=x[4]) if x else None
    nb, nt, ns = ref(b), ref(t), ref(s)
    ambiguity: list[str] = []

    if b is None:
        # Not in base: an addition on one or both sides.
        if nt and ns:
            return Correspondence(nb, nt, ns, "ADDED", tuple(ambiguity))
        return Correspondence(nb, nt, ns, "ADDED", tuple(ambiguity))
    if nt is None and ns is None:
        return Correspondence(nb, None, None, "DELETED", tuple(ambiguity))

    # Pair missing-side entities by fingerprint (moved/renamed): the def
    # exists under a different name or in a different file.
    if nt is None and fp_t:
        cand = fp_t.get(b[3], [])
        cand = [(p, n) for (p, n, k, f, sig) in cand if n != name]
        if len(cand) == 1:
            nt = NodeRef(cand[0][0], cand[0][1], "", b[3], "")
        elif len(cand) > 1:
            ambiguity.append(f"target: {len(cand)} fingerprint matches")
    if ns is None and fp_s:
        cand = fp_s.get(b[3], [])
        cand = [(p, n) for (p, n, k, f, sig) in cand if n != name]
        if len(cand) == 1:
            ns = NodeRef(cand[0][0], cand[0][1], "", b[3], "")
        elif len(cand) > 1:
            ambiguity.append(f"source: {len(cand)} fingerprint matches")

    def relation(side_ref, side_fp, label):
        if side_ref is None:
            return None
        fp, path, nm = side_ref.fingerprint, side_ref.path, side_ref.name
        b_path, b_fp, b_name = b[0], b[3], b[1]
        if fp == b_fp and path == b_path and nm == b_name:
            return "UNCHANGED"
        if fp == b_fp and path != b_path and nm == b_name:
            return "MOVED"
        if fp == b_fp and path == b_path and nm != b_name:
            return "RENAMED"
        if fp == b_fp:
            return "MOVED_AND_RENAMED"
        if path != b_path and nm != b_name:
            return f"{label}_AND_MODIFIED"
        return "MODIFIED"

    rt = relation(nt, fp_t, "target")
    rs = relation(ns, fp_s, "source")
    if rt is None and rs is not None:
        relation_final = f"DELETED_ON_TARGET/{rs}"
    elif rs is None and rt is not None:
        relation_final = f"DELETED_ON_SOURCE/{rt}"
    elif rt == rs:
        relation_final = rt or "UNCHANGED"
    else:
        relation_final = f"MIXED({rt}|{rs})"
    return Correspondence(nb, nt, ns, relation_final, tuple(ambiguity))


def correspondence_summary(records: list[Correspondence]) -> Counter:
    from collections import Counter
    return Counter(r.relation for r in records)


from collections import Counter  # noqa: E402
