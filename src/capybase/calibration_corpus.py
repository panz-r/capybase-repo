"""Blessed-output conflict corpus for mechanism calibration.

Distinct from the single ``x=1/2/3`` probe in :mod:`capybase.probes` (which is
unresolvable and only measures parseability). Each conflict here has a
**genuinely-correct merge** — the block-interior text that replaces the marker
span. Calibration resolves each conflict under a candidate setting and scores
correctness by comparing the candidate's ``resolved_text`` (normalized) to the
blessed text. This is the only non-self-referential quality signal: it is
checked against merges authored here, not against the model's self-report or
capybase's own accept/reject decision.

Conventions (mirror ``tests/conftest.py::multi_unit_conflicted_repo``):
- Each ``CalibrationConflict`` carries ONE ``ConflictUnit`` (single-hunk; the
  corpus favors breadth of conflict *shape* over multi-hunk files, since
  mechanism quality differences show up at the hunk level).
- ``expected_text`` is the block-interior resolved text — exactly what
  ``resolved_text`` should equal (modulo normalization) after a correct merge.
- Sides are chosen so the correct merge is unambiguous (both sides' changes
  combine, no real semantic tension), so a capable model CAN score full marks.
"""

from __future__ import annotations

from dataclasses import dataclass

from capybase.conflict_model import ConflictSide, ConflictUnit, ContextBundle, SideLabel


@dataclass(frozen=True)
class CalibrationConflict:
    """One synthetic conflict + its known-correct resolution.

    ``expected_text`` is the canonical merged block-interior. ``title`` is a
    short label for the calibration report (what conflict shape this exercises).
    """

    title: str
    unit: ConflictUnit
    expected_text: str


def _side(label: SideLabel, text: str) -> ConflictSide:
    return ConflictSide(label=label, text=text)


def _unit(unit_id: str, path: str, language: str,
         base: str, current: str, replayed: str, original: str) -> ConflictUnit:
    return ConflictUnit(
        session_id="calibrate",
        step_index=0,
        path=path,
        language=language,
        unit_id=unit_id,
        base=_side("BASE", base),
        current=_side("CURRENT_UPSTREAM_SIDE", current),
        replayed=_side("REPLAYED_COMMIT_SIDE", replayed),
        original_worktree_text=original,
    )


def _conflict(
    title: str, unit_id: str, path: str, language: str,
    base: str, current: str, replayed: str, original: str, expected: str,
) -> CalibrationConflict:
    return CalibrationConflict(
        title=title,
        unit=_unit(unit_id, path, language, base, current, replayed, original),
        expected_text=expected,
    )


# A ContextBundle with just primary_text — the resolve prompt builder fills the
# rest. Reused for every conflict (calibration cares about the conflict shape,
# not surrounding-file context).
def _context(unit: ConflictUnit) -> ContextBundle:
    return ContextBundle(primary_text="")


CALIBRATION_CONFLICTS: list[CalibrationConflict] = [
    # 1. List-combine: both sides append distinct elements. Correct merge keeps
    #    all unique elements from both sides (union, order-respecting).
    _conflict(
        title="list-combine",
        unit_id="list-0", path="services.py", language="python",
        base='SERVICES = ["core"]',
        current='SERVICES = ["core", "scheduler"]',
        replayed='SERVICES = ["core", "reloader"]',
        original='SERVICES = ["core"]',
        expected='SERVICES = ["core", "scheduler", "reloader"]',
    ),
    # 2. Dict-combine: both sides flip distinct keys from off->on. Correct merge
    #    turns BOTH keys on (union of changes).
    _conflict(
        title="dict-combine",
        unit_id="dict-0", path="flags.py", language="python",
        base='    "cache": "off",\n    "metrics": "off"',
        current='    "cache": "off",\n    "metrics": "on"',
        replayed='    "cache": "on",\n    "metrics": "off"',
        original='    "cache": "off",\n    "metrics": "off"',
        expected='    "cache": "on",\n    "metrics": "on"',
    ),
    # 3. Both-sides-add: each side adds a distinct constant. Correct merge keeps
    #    both additions.
    _conflict(
        title="both-sides-add",
        unit_id="add-0", path="constants.py", language="python",
        base="MAX_CONN = 10",
        current="MAX_CONN = 10\nTIMEOUT = 30",
        replayed="MAX_CONN = 10\nRETRIES = 3",
        original="MAX_CONN = 10",
        expected="MAX_CONN = 10\nTIMEOUT = 30\nRETRIES = 3",
    ),
    # 4. Indentation-sensitive: a guard clause where both sides add a distinct
    #    condition inside a function body. Correct merge keeps both guards on
    #    separate lines at the SAME indentation.
    _conflict(
        title="indent-sensitive",
        unit_id="indent-0", path="app.py", language="python",
        base="    if not data:\n        return None",
        current="    if not data:\n        return None\n    if not ctx:\n        return None",
        replayed="    if not data:\n        return None\n    if not user:\n        return None",
        original="    if not data:\n        return None",
        expected="    if not data:\n        return None\n    if not ctx:\n        return None\n    if not user:\n        return None",
    ),
    # 5. Non-code text combine (markdown/docs): both sides add a distinct list
    #    item. Correct merge keeps both items. Exercises the non-Python path.
    _conflict(
        title="text-combine",
        unit_id="text-0", path="README.md", language=None,
        base="- feature A",
        current="- feature A\n- feature B",
        replayed="- feature A\n- feature C",
        original="- feature A",
        expected="- feature A\n- feature B\n- feature C",
    ),
]


def conflicts_with_context() -> list[tuple[CalibrationConflict, ContextBundle]]:
    """Return each conflict paired with its (minimal) ContextBundle, ready to
    pass to ``ResolutionEngine.propose``."""
    return [(c, _context(c.unit)) for c in CALIBRATION_CONFLICTS]
