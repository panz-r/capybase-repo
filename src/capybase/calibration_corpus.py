"""Blessed-output conflict corpus for mechanism calibration.

Distinct from the single ``x=1/2/3`` probe in :mod:`capybase.probes` (which is
unresolvable and only measures parseability). Each conflict here has a
**genuinely-correct merge** — the block-interior text that replaces the marker
span. Calibration resolves each conflict under a candidate setting and scores
correctness by comparing the candidate's ``resolved_text`` (normalized) to the
blessed text. This is the only non-self-referential quality signal: it is
checked against merges authored here, not against the model's self-report or
capybase's own accept/reject decision.

Coverage spans the shapes that stress *engine-level* mechanisms (the ones
calibration A/B-selects): union-combines, Rust syntax (struct fields + impl
methods), multi-hunk same-file, import/dependency combine, duplicate-symbol
tension, semantically-incompatible same-line edits, and modify/delete
keeper-wins. NOTE: this corpus is broad on conflict *shape* but not yet
statistically robust — ``probe_mechanisms`` refuses to A/B-select expensive
mechanisms below a minimum corpus size (see ``_MIN_CORPUS_FOR_MECHANISM_SELECTION``),
so a too-small corpus leaves mechanisms off rather than flipping them on noise.
The orchestrator-level mechanisms (structural resolution, block-capture) are NOT
exercised here — calibration resolves through the engine, not the orchestrator —
so they are always on by default and not subject to this selection.

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
    ``task_type`` tags the conflict's family (feedback §4): the default is
    ``"merge_conflict_resolution"``; new task families (config_merge, test_port)
    carry their own tag so calibration can evaluate per-task profiles.
    """

    title: str
    unit: ConflictUnit
    expected_text: str
    task_type: str = "merge_conflict_resolution"


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
    *,
    task_type: str = "merge_conflict_resolution",
) -> CalibrationConflict:
    return CalibrationConflict(
        title=title,
        unit=_unit(unit_id, path, language, base, current, replayed, original),
        expected_text=expected,
        task_type=task_type,
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
    # 6. Rust struct-field merge: both sides add a distinct field to a struct
    #    body. Correct merge keeps both fields. Exercises Rust indentation/
    #    trailing-comma conventions (a naive merge drops a comma or a field).
    _conflict(
        title="rust-struct-fields",
        unit_id="rust-struct-0", path="config.rs", language="rust",
        base="pub struct Config {\n    pub name: String,\n}",
        current="pub struct Config {\n    pub name: String,\n    pub max_retries: u32,\n}",
        replayed="pub struct Config {\n    pub name: String,\n    pub timeout_ms: u32,\n}",
        original="pub struct Config {\n    pub name: String,\n}",
        expected="pub struct Config {\n    pub name: String,\n    pub max_retries: u32,\n    pub timeout_ms: u32,\n}",
    ),
    # 7. Rust impl-block conflict: both sides add a distinct method to the same
    #    impl. Correct merge keeps both methods (order-respecting).
    _conflict(
        title="rust-impl-methods",
        unit_id="rust-impl-0", path="service.rs", language="rust",
        base="impl Service {\n    pub fn start(&self) {}\n}",
        current="impl Service {\n    pub fn start(&self) {}\n    pub fn stop(&self) {}\n}",
        replayed="impl Service {\n    pub fn start(&self) {}\n    pub fn restart(&self) {}\n}",
        original="impl Service {\n    pub fn start(&self) {}\n}",
        expected="impl Service {\n    pub fn start(&self) {}\n    pub fn stop(&self) {}\n    pub fn restart(&self) {}\n}",
    ),
    # 8. Multi-hunk same file: two well-separated regions each edited by both
    #    sides. Correct merge applies BOTH hunks. Stresses keeping regions
    #    distinct (a naive single-region merge drops one hunk).
    _conflict(
        title="multi-hunk",
        unit_id="multi-0", path="tuning.py", language="python",
        base='HOST = "localhost"\nPORT = 8000\n\nDEBUG = False\nLOG_LEVEL = "info"',
        current='HOST = "0.0.0.0"\nPORT = 8000\n\nDEBUG = False\nLOG_LEVEL = "debug"',
        replayed='HOST = "localhost"\nPORT = 9000\n\nDEBUG = True\nLOG_LEVEL = "info"',
        original='HOST = "localhost"\nPORT = 8000\n\nDEBUG = False\nLOG_LEVEL = "info"',
        expected='HOST = "0.0.0.0"\nPORT = 9000\n\nDEBUG = True\nLOG_LEVEL = "debug"',
    ),
    # 9. Import/dependency combine: both sides add a distinct import. Correct
    #    merge keeps both imports. A naive merge that copies one side drops the
    #    other's dependency (a real breakage at runtime).
    _conflict(
        title="import-combine",
        unit_id="import-0", path="app.py", language="python",
        base="import os",
        current="import os\nimport json",
        replayed="import os\nimport sys",
        original="import os",
        expected="import os\nimport json\nimport sys",
    ),
    # 10. Duplicate-symbol tension: both sides add a function whose body extends
    #     a shared base helper in DIFFERENT ways. Correct merge keeps both
    #     additions (distinct names), not two copies of one or a clobbering.
    _conflict(
        title="distinct-functions",
        unit_id="dup-0", path="calc.py", language="python",
        base="def base():\n    return 0",
        current="def base():\n    return 0\n\ndef add(x, y):\n    return x + y",
        replayed="def base():\n    return 0\n\ndef mul(x, y):\n    return x * y",
        original="def base():\n    return 0",
        expected="def base():\n    return 0\n\ndef add(x, y):\n    return x + y\n\ndef mul(x, y):\n    return x * y",
    ),
    # 11. Semantically-incompatible same-line edit: both sides change the SAME
    #     line to different values. There is no clean union; the correct merge
    #     takes ONE canonical value (here the upstream/higher value). This is
    #     the shape where a model that "compromises" (e.g. emits both values)
    #     is WRONG — it must pick.
    _conflict(
        title="same-line-pick",
        unit_id="pick-0", path="config.py", language="python",
        base='VERSION = 1',
        current='VERSION = 2',
        replayed='VERSION = 3',
        original='VERSION = 1',
        expected='VERSION = 3',
    ),
    # 12. Config/dict-key combine (mirrors dict-combine but with assignment
    #     context): both sides add a distinct key to a settings dict. Correct
    #     merge keeps both keys.
    _conflict(
        title="config-keys",
        unit_id="cfg-0", path="settings.py", language="python",
        base='OPTIONS = {\n    "verbose": True,\n}',
        current='OPTIONS = {\n    "verbose": True,\n    "dry_run": False,\n}',
        replayed='OPTIONS = {\n    "verbose": True,\n    "strict": True,\n}',
        original='OPTIONS = {\n    "verbose": True,\n}',
        expected='OPTIONS = {\n    "verbose": True,\n    "dry_run": False,\n    "strict": True,\n}',
    ),
    # 13. Modify/delete where the KEEPER wins: upstream deleted a helper block;
    #     replayed kept AND adapted it. The correct merge keeps the adapted
    #     keeper (its change is load-bearing). Exercises producing the full
    #     keeper text (deletion must NOT win here). NOTE: calibration resolves
    #     through the engine, so this scores the model's ability to reproduce
    #     the keeper; block-capture (the orchestrator mechanism that would
    #     splice it verbatim) is not exercised here.
    _conflict(
        title="modify-delete-keeper-wins",
        unit_id="md-keep-0", path="utils.py", language="python",
        base="def helper():\n    return 1",
        current="",  # upstream deleted
        replayed="def helper():\n    return 1  # adapted: documented",
        original="def helper():\n    return 1",
        expected="def helper():\n    return 1  # adapted: documented",
    ),
    # 14. Long-context combine: a larger block (>40 lines) where both sides
    #     append distinct constant definitions. Exercises prompt-window handling
    #     and reliable reproduction of a non-trivial block (the failure mode
    #     block-capture exists for, here at the engine level).
    _conflict(
        title="long-block-combine",
        unit_id="long-0", path="constants.py", language="python",
        base="\n".join(f"C{i} = {i}" for i in range(40)),
        current="\n".join(f"C{i} = {i}" for i in range(40)) + "\n" + "\n".join(f"D{i} = {i}" for i in range(8)),
        replayed="\n".join(f"C{i} = {i}" for i in range(40)) + "\n" + "\n".join(f"E{i} = {i}" for i in range(8)),
        original="\n".join(f"C{i} = {i}" for i in range(40)),
        expected="\n".join(f"C{i} = {i}" for i in range(40))
        + "\n" + "\n".join(f"D{i} = {i}" for i in range(8))
        + "\n" + "\n".join(f"E{i} = {i}" for i in range(8)),
    ),
    # 15. Rename-with-adaptation flavor: one side renames a symbol's value
    #     (replayed), the other adds an independent field (current). Correct
    #     merge takes the rename AND keeps the addition — testing that the model
    #     doesn't revert a rename while keeping an addition.
    _conflict(
        title="rename-plus-add",
        unit_id="rn-0", path="meta.py", language="python",
        base='NAME = "old"\nTAG = "v1"',
        current='NAME = "old"\nTAG = "v1"\nDESC = "service"',
        replayed='NAME = "new"\nTAG = "v1"',
        original='NAME = "old"\nTAG = "v1"',
        expected='NAME = "new"\nTAG = "v1"\nDESC = "service"',
    ),
]


#: The active task-type filter for calibration, or None for the default corpus.
#: Set by ``run_calibration(task=...)`` so the un-parameterized
#: ``conflicts_with_context()`` call inside ``evaluate_setting`` picks it up
#: without threading the task through every function signature.
_active_task_type: str | None = None


def set_active_task_type(task_type: str | None) -> None:
    """Set the process-wide active task-type filter for calibration."""
    global _active_task_type
    _active_task_type = task_type


def conflicts_with_context(
    task_type: str | None = None,
) -> list[tuple[CalibrationConflict, ContextBundle]]:
    """Return each conflict paired with its (minimal) ContextBundle, ready to
    pass to ``ResolutionEngine.propose``.

    ``task_type`` filters to a specific task family (feedback §4). None (the
    default) reads the process-wide ``_active_task_type`` (set by
    ``run_calibration``); when that's also None, returns the standard
    ``merge_conflict_resolution`` corpus — the backward-compatible behavior.
    """
    if task_type is None:
        task_type = _active_task_type or "merge_conflict_resolution"
    corpus = ALL_CONFLICTS_BY_TASK.get(task_type, CALIBRATION_CONFLICTS)
    return [(c, _context(c.unit)) for c in corpus]


# ---------------------------------------------------------------------------
# Task families (feedback §4): additional corpora beyond the standard
# merge-conflict-resolution set. Each is a small, focused corpus for a distinct
# conflict shape, so calibration can produce per-task profile overrides.
# ---------------------------------------------------------------------------

#: The config_merge family: TOML/INI-style key conflicts with semantic
#: constraints (both sides add/update keys; the merge must keep all).
CONFIG_MERGE_CONFLICTS: list[CalibrationConflict] = [
    _conflict(
        title="config-add-key",
        unit_id="cfg-add", path="app.toml", language="toml",
        base='timeout = 30\nretries = 3',
        current='timeout = 30\nretries = 3\nverbose = true',
        replayed='timeout = 30\nretries = 3\nlog_level = "debug"',
        original='timeout = 30\nretries = 3',
        expected='timeout = 30\nretries = 3\nverbose = true\nlog_level = "debug"',
        task_type="config_merge",
    ),
    _conflict(
        title="config-update-value",
        unit_id="cfg-upd", path="settings.ini", language="ini",
        base='max_connections = 10',
        current='max_connections = 20',
        replayed='max_connections = 15',
        original='max_connections = 10',
        expected='max_connections = 20',
        task_type="config_merge",
    ),
]

#: The test_port family: test files that must move in lockstep with impl
#: changes (the merge must preserve the test's assertions while adopting the
#: impl's new API).
TEST_PORT_CONFLICTS: list[CalibrationConflict] = [
    _conflict(
        title="test-rename-assertion",
        unit_id="test-rn", path="test_service.py", language="python",
        base='assert service.get_name() == "old"',
        current='assert service.get_name() == "old"',
        replayed='assert service.name == "old"',
        original='assert service.get_name() == "old"',
        expected='assert service.name == "old"',
        task_type="test_port",
    ),
    _conflict(
        title="test-new-param",
        unit_id="test-param", path="test_api.py", language="python",
        base='result = api.call("endpoint")',
        current='result = api.call("endpoint", timeout=30)',
        replayed='result = api.call("endpoint")',
        original='result = api.call("endpoint")',
        expected='result = api.call("endpoint", timeout=30)',
        task_type="test_port",
    ),
]

#: All task families, keyed by task_type. Used by conflicts_with_context and
#: the CLI's --list-tasks.
ALL_CONFLICTS_BY_TASK: dict[str, list[CalibrationConflict]] = {
    "merge_conflict_resolution": CALIBRATION_CONFLICTS,
    "config_merge": CONFIG_MERGE_CONFLICTS,
    "test_port": TEST_PORT_CONFLICTS,
}

#: The known task-family names (for --list-tasks and validation).
TASK_FAMILIES: tuple[str, ...] = tuple(ALL_CONFLICTS_BY_TASK.keys())
