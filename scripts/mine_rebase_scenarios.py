"""Multi-commit rebase scenario mining from real git history.

The companion to ``fetch_mergeconflict_datasets.py``: where that script mines
*single-file* 3-way merge conflicts, this one mines *multi-commit rebase
scenarios* — a source branch of N commits replayed onto a target, with conflicts
recorded at specific replay steps. This is the data shape that actually exercises
the history-aware mechanisms (conflict chains, future probes, branch intent):
single-file 3-way tuples have no replay sequence, so they can't.

How a scenario is mined (mirrors the production orchestrator's plan build +
the test ``multistep_builder``'s real-rebase drive, applied to a real clone):

1. Select a merge commit M with parents P1, P2 and merge-base O.
2. The SOURCE side = the longer of ``O..P1`` / ``O..P2`` (biases toward the
   feature branch); the TARGET = the other side. Skip if the source has fewer
   than ``require_min_commits`` (we want genuine multi-commit replays).
3. In a disposable linked worktree: check out the target tip, branch off, and
   ``git rebase <source_tip>`` — replaying the source commits onto the target.
4. At each rebase stop, if paths are unmerged, record a :class:`ConflictStep`
   (step index, paths, 3-way stage blobs, marker-regenerated text). Resolve by
   taking the source side verbatim and ``--continue`` to advance.
5. Emit a :class:`RebaseScenario` carrying the full source-commit sequence (via
   ``replayed_commit_sequence``, the same call the orchestrator makes) + the
   conflict steps. The output JSON is ``RebasePlan``-compatible.

The clones live in ``external-datasets/`` (blob-filtered, gitignored); scenarios
are written to ``extracted-testdata/rebase-scenarios/`` (gitignored,
clean-skip-when-empty, same contract as the realworld cases).

CLI: ``python scripts/mine_rebase_scenarios.py --dataset {id|all}
--max-scenarios N --language {rust|python|all}``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

# Reuse the dataset registry + clone helper + path constants from the sibling
# fetch script so this stays the single source of truth for where clones live.
# (scripts/ is a package — see scripts/__init__.py — so this import works both
# when run as `python scripts/mine_...` from the repo root and via -m.)
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from scripts.fetch_mergeconflict_datasets import (  # noqa: E402
    DATASETS,
    EXTERNAL,
    REPO_ROOT,
    TESTDATA,
    Dataset,
    clone_repo,
)

_log = logging.getLogger("capybase.mine_rebase_scenarios")

#: Output dir for mined rebase scenarios (gitignored, clean-skip-when-empty).
SCENARIO_DIR = REPO_ROOT / "extracted-testdata" / "rebase-scenarios"

#: Only consider merge commits whose replayed side has this many commits.
#: Lower = more scenarios but noisier (1-commit branches aren't "multi-commit").
DEFAULT_MIN_COMMITS = 2
#: Cap scenarios per repo (each drives a real rebase in a worktree — expensive).
DEFAULT_MAX_SCENARIOS = 40


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConflictStep:
    """One conflicted replay step, with the 3-way blobs captured from the index.

    ``step`` is 1-based (the Nth replayed commit). ``path`` is the repo-relative
    file. The ``base``/``current``/``replayed`` blobs are the stage-1/2/3 index
    entries (base=merge-base, current=target-side, replayed=source-side), decoded
    as text. ``replayed_commit_oid`` is the commit being replayed at this step
    (so history features can derive future touches). ``marker_text`` is the
    conflict-marker-regenerated file content at this step.
    """

    step: int
    path: str
    replayed_commit_oid: str
    base: str
    current: str
    replayed: str
    marker_text: str


@dataclass(frozen=True)
class RebaseScenario:
    """A mined multi-commit rebase scenario."""

    id: str
    dataset: str
    source_tip_oid: str
    target_tip_oid: str
    merge_base_oid: str
    #: The replayed source-commit sequence (ReplayCommit-shaped dicts, oldest-
    #: first). Matches the persisted ``rebase_plan.json`` schema exactly.
    source_commits: list[dict]
    conflict_steps: list[ConflictStep]
    license: str
    source_url: str
    clone_subdir: str

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ---------------------------------------------------------------------------
# git helpers (subprocess-based; the clone is a real repo, not a GitBackend)
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str, check: bool = True, input_bytes: bytes | None = None) -> str:
    """Run git in ``repo``; return stdout. Raise on failure when check=True."""
    import subprocess

    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        input=input_bytes,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {args[0]} failed (rc={proc.returncode}): "
            f"{proc.stderr.decode('utf-8', errors='replace').strip()}"
        )
    return proc.stdout.decode("utf-8", errors="replace")


def _git_raw(repo: Path, *args: str, check: bool = True) -> bytes:
    import subprocess

    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {args[0]} failed (rc={proc.returncode}): "
            f"{proc.stderr.decode('utf-8', errors='replace').strip()}"
        )
    return proc.stdout


def _merge_commits(repo: Path, *, limit: int) -> list[str]:
    """Recent 2-parent merge commit OIDs (newest-first), capped at ``limit``."""
    out = _git(
        repo, "rev-list", "--merges", "--min-parents=2", f"--max-count={limit}",
        "--format=%H", "HEAD", check=False,
    )
    # rev-list --format prepends "commit <oid>"; extract the bare OIDs.
    oids = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("commit "):
            oids.append(line[len("commit "):])
    return oids


def _parents(repo: Path, oid: str) -> list[str]:
    out = _git(repo, "rev-list", "--parents", "--max-count=1", oid).strip()
    parts = out.split()
    return parts[1:]  # parts[0] is the commit itself


def _merge_base(repo: Path, a: str, b: str) -> str | None:
    out = _git(repo, "merge-base", a, b, check=False).strip()
    return out or None


def _commit_count(repo: Path, base: str, tip: str) -> int:
    """Number of commits in ``base..tip`` (the replayed side's length)."""
    out = _git(repo, "rev-list", "--count", f"{base}..{tip}", check=False).strip()
    try:
        return int(out)
    except ValueError:
        return 0


def _build_markers(base: str, current: str, replayed: str) -> str | None:
    """Regenerate authentic conflict markers via ``git merge-file``.

    Returns the marker-marked text, or None if the merge is clean (no conflict).
    Mirrors ``fetch_mergeconflict_datasets.build_markers`` but operates on text
    we already have (the mining worktree holds the 3-way blobs in its index).
    """
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "O").write_text(base)
        (d / "A").write_text(current)   # "ours" = target side
        (d / "B").write_text(replayed)  # "theirs" = source side
        # git merge-file A O B: merges B into A using base O; writes markers to A.
        import subprocess
        proc = subprocess.run(
            ["git", "merge-file", "-p", "--diff3", str(d / "A"), str(d / "O"), str(d / "B")],
            capture_output=True,
        )
        # Exit 0 = clean merge (no conflict); non-zero (incl. 1) = conflict present.
        if proc.returncode == 0:
            return None
        return proc.stdout.decode("utf-8", errors="replace")


def _unmerged_paths(repo: Path) -> list[str]:
    """Distinct unmerged paths in the worktree, sorted."""
    out = _git_raw(repo, "ls-files", "-u", "-z")
    paths: set[str] = set()
    for record in out.split(b"\0"):
        if not record.strip():
            continue
        # "<mode> <oid> <stage>\t<path>"
        meta, _, path = record.partition(b"\t")
        if path:
            paths.add(path.decode("utf-8", errors="replace"))
    return sorted(paths)


def _stage_blob(repo: Path, path: str, stage: int) -> str:
    """The decoded text of ``path`` at unmerged ``stage`` (1=base,2=target,3=source)."""
    raw = _git_raw(repo, "show", f":{stage}:{path}", check=False)
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# the mining core
# ---------------------------------------------------------------------------


def mine_rebase_scenarios(
    clone: Path,
    *,
    merge_limit: int = 200,
    max_scenarios: int = DEFAULT_MAX_SCENARIOS,
    require_min_commits: int = DEFAULT_MIN_COMMITS,
) -> Iterator[RebaseScenario]:
    """Yield multi-commit rebase scenarios mined from ``clone``.

    For each candidate merge commit, derives a source/target pair, drives a real
    rebase in a disposable worktree, and records the conflicted steps. Skips
    merges that don't yield a multi-commit replay or produce no conflicts. Stops
    after ``max_scenarios`` valid scenarios. Never raises on a single bad merge
    (logs + continues); a failed worktree is always cleaned up.
    """
    from capybase.git_backend import GitBackend

    gb = GitBackend(clone)
    merges = _merge_commits(clone, limit=merge_limit)
    _log.info("mining %s: %d candidate merges", clone.name, len(merges))

    found = 0
    for m in merges:
        if found >= max_scenarios:
            break
        try:
            scenario = _mine_one_merge(gb, clone, m, require_min_commits)
            if scenario is not None:
                yield scenario
                found += 1
        except Exception as exc:  # noqa: BLE001 - one bad merge must not stop the run
            _log.debug("merge %s skipped: %s", m[:8], exc)


def _mine_one_merge(
    gb: GitBackend, clone: Path, merge_oid: str, require_min_commits: int
) -> RebaseScenario | None:
    """Mine one merge commit into a scenario, or None if it's unsuitable."""
    parents = _parents(clone, merge_oid)
    if len(parents) < 2:
        return None
    p1, p2 = parents[0], parents[1]
    base = _merge_base(clone, p1, p2)
    if base is None:
        return None
    # The source = the longer side; target = the other. Biases toward the
    # feature branch (more commits to replay).
    n1 = _commit_count(clone, base, p1)
    n2 = _commit_count(clone, base, p2)
    if n1 >= n2:
        source_tip, target_tip, source_n = p1, p2, n1
    else:
        source_tip, target_tip, source_n = p2, p1, n2
    if source_n < require_min_commits:
        return None

    # Drive the rebase in a throwaway worktree.
    wt_path: Path | None = None
    replay_branch = f"capybase-mine-{uuid.uuid4().hex[:8]}"
    try:
        wt_path = Path(tempfile.mkdtemp(prefix="capybase-mine-"))
        res = gb.add_worktree(wt_path, detach=True)
        if not res.ok:
            return None
        # Check out the target tip + branch off, then rebase the source onto it.
        _git(wt_path, "checkout", "-q", "-b", replay_branch, target_tip)
        # Capture the source-commit sequence BEFORE the rebase (stable OIDs).
        source_commits = gb.replayed_commit_sequence(base, source_tip)
        # Drive the rebase: replay source_tip onto the target branch.
        _git(wt_path, "rebase", "--onto", target_tip, base, source_tip, check=False)

        conflict_steps = _capture_conflict_steps(wt_path, source_commits)
        if not conflict_steps:
            return None  # clean rebase — no history-aware scenario

        return RebaseScenario(
            id="",  # filled by the writer (dataset + index)
            dataset="",  # filled by the writer
            source_tip_oid=source_tip,
            target_tip_oid=target_tip,
            merge_base_oid=base,
            source_commits=source_commits,
            conflict_steps=conflict_steps,
            license="",  # filled by the writer
            source_url="",  # filled by the writer
            clone_subdir="",  # filled by the writer
        )
    finally:
        if wt_path is not None and wt_path.exists():
            try:
                gb.remove_worktree(wt_path, force=True)
            except Exception:  # noqa: BLE001
                pass
            gb.prune_worktrees()


def _capture_conflict_steps(wt: Path, source_commits: list[dict]) -> list[ConflictStep]:
    """Walk a stopped rebase, recording each conflicted step until it completes.

    Resolves each conflict by taking the source side verbatim (we control the
    replay; the source-side content is the "intended" replay) and continues.
    Returns the captured steps (1-based). Mirrors ``multistep_builder``'s loop.
    """
    steps: list[ConflictStep] = []
    step_index = 0
    # Guard against infinite loops: never more iterations than source commits + a
    # small margin (a rebase can't stop more often than it has commits to replay).
    max_iters = len(source_commits) + 2 if source_commits else 50
    for _ in range(max_iters):
        unmerged = _unmerged_paths(wt)
        if not unmerged:
            # No conflict here. Is the rebase done?
            if not _rebase_in_progress(wt):
                break
            # Mid-clean-replay: nudge continue and advance.
            _git(wt, "rebase", "--continue", check=False)
            step_index += 1
            continue
        # Conflicted step: record each path's 3-way blobs.
        step_index += 1
        # The replayed commit at this step (1-based → 0-based list index).
        replayed_oid = (
            source_commits[step_index - 1]["oid"]
            if step_index - 1 < len(source_commits)
            else ""
        )
        for path in unmerged:
            base_b = _stage_blob(wt, path, 1)
            cur_b = _stage_blob(wt, path, 2)
            rep_b = _stage_blob(wt, path, 3)
            marker = _build_markers(base_b, cur_b, rep_b)
            if marker is None:
                continue  # somehow clean despite unmerged — skip
            steps.append(ConflictStep(
                step=step_index, path=path,
                replayed_commit_oid=replayed_oid,
                base=base_b, current=cur_b, replayed=rep_b,
                marker_text=marker,
            ))
        # Resolve by taking the source (stage 3) side verbatim, then continue.
        for path in unmerged:
            _git_raw(wt, "checkout", "--theirs", "--", path, check=False)
            _git(wt, "add", "--", path)
        _git(wt, "rebase", "--continue", check=False)
    return steps


def _rebase_in_progress(wt: Path) -> bool:
    res = _git(wt, "rev-parse", "--git-path", "rebase-merge", check=False).strip()
    return bool(res) and (wt / res).is_dir()


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


def process(
    dataset: Dataset,
    *,
    max_scenarios: int = DEFAULT_MAX_SCENARIOS,
    require_min_commits: int = DEFAULT_MIN_COMMITS,
    language: str = "rust",
) -> int:
    """Mine scenarios for ``dataset`` and write them to SCENARIO_DIR.

    Clears prior scenarios for this dataset first (re-runs don't accumulate
    stale files). Returns the number written. Filters by ``language`` against
    the conflict files' extension (rust=.rs, python=.py) — pass ``"all"`` to keep
    every language.
    """
    clone = EXTERNAL / dataset.extract_subdir
    if not (clone / ".git").exists():
        _log.warning("[skip] %s: clone not present at %s", dataset.id, clone)
        return 0
    SCENARIO_DIR.mkdir(parents=True, exist_ok=True)
    # Clear prior scenarios for this dataset.
    for old in SCENARIO_DIR.glob(f"{dataset.id}-*.json"):
        old.unlink()

    ext = {"rust": ".rs", "python": ".py"}.get(language)
    written = 0
    for scenario in mine_rebase_scenarios(
        clone,
        max_scenarios=max_scenarios,
        require_min_commits=require_min_commits,
    ):
        # Language filter on the conflict paths.
        if ext is not None and not any(s.path.endswith(ext) for s in scenario.conflict_steps):
            continue
        scenario_id = f"{dataset.id}-rebase-{written + 1:04d}"
        full = RebaseScenario(
            id=scenario_id, dataset=dataset.id,
            source_tip_oid=scenario.source_tip_oid,
            target_tip_oid=scenario.target_tip_oid,
            merge_base_oid=scenario.merge_base_oid,
            source_commits=scenario.source_commits,
            conflict_steps=scenario.conflict_steps,
            license=dataset.license, source_url=dataset.source_url,
            clone_subdir=dataset.extract_subdir,
        )
        out = SCENARIO_DIR / f"{scenario_id}.json"
        out.write_text(json.dumps(full.to_dict(), ensure_ascii=False, indent=2))
        _log.info("[wrote] %s (%d commits, %d conflict steps)",
                  scenario_id, len(full.source_commits), len(full.conflict_steps))
        written += 1
    _log.info("mined %s: %d scenarios", dataset.id, written)
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dataset", default="all", choices=["all", *DATASETS],
                   help="dataset id to mine (default: all git-history datasets)")
    p.add_argument("--max-scenarios", type=int, default=DEFAULT_MAX_SCENARIOS,
                   help=f"cap scenarios per repo (default {DEFAULT_MAX_SCENARIOS})")
    p.add_argument("--min-commits", type=int, default=DEFAULT_MIN_COMMITS,
                   help=f"min source-side commits to keep a scenario (default {DEFAULT_MIN_COMMITS})")
    p.add_argument("--language", default="rust", choices=["rust", "python", "all"],
                   help="filter scenarios by conflict-file language (default rust)")
    p.add_argument("--list", action="store_true", help="list registered datasets and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if args.list:
        for did, ds in DATASETS.items():
            if ds.kind == "git-history":
                print(f"{did:24} {ds.extract_subdir:14} {ds.url}")
        return 0

    ids = [args.dataset] if args.dataset != "all" else [
        did for did, ds in DATASETS.items() if ds.kind == "git-history"
    ]
    total = 0
    for did in ids:
        ds = DATASETS[did]
        # Ensure the clone exists (blob-filtered, idempotent).
        clone_repo(ds)
        n = process(ds, max_scenarios=args.max_scenarios,
                    require_min_commits=args.min_commits, language=args.language)
        total += n
    print(f"mined {total} scenario(s) into {SCENARIO_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
