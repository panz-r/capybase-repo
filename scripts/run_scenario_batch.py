"""Crash-proof batch runner for scenario evals (s27-61).

Wraps scripts/live_eval_scenarios.py — one subprocess per scenario —
adding everything the sweeps had to do by hand:

- STABLE result names + MANIFEST: results land as <out-dir>/r-<id>.json;
  <out-dir>/manifest.json is rewritten after each scenario (verdict,
  elapsed, session, attempts, infra_hangs). A runner death loses nothing.
- RESUME: re-invocation skips scenarios whose result file already parses
  with a verdict (INFRA_HANG rows too, unless --retry-hangs).
- LOCKFILE: <out-dir>/batch.lock refuses a second live batch on the same
  directory (the s27-60 two-batches-one-endpoint race).
- WATCHDOG: a scenario whose journal size AND /proc io AND cpu are all
  flat for --hang-after seconds is killed (whole process group — the
  loop AND its python, the s27-60 lesson), retried once, then recorded
  as INFRA_HANG. Encodes the endpoint-hang signature (libuv-0019 ×2,
  duckdb-0001: a worker parked in poll() on an empty-queue socket past
  every deadline).
- SCENARIO DEADLINE: --max-scenario-seconds is the outer backstop the
  orchestrator's per-file budgets don't provide (observed totals ran
  9388-11465s); expiry records SCENARIO_TIMEOUT.
- PREFLIGHT (default on): one real endpoint completion, clone promisor
  check with auto-prepare, /tmp headroom (>=10G, the AGENTS.md rule),
  and orphan-worktree cleanup.

Usage:
    python scripts/run_scenario_batch.py --provider <name> \
        --dataset duckdb-history --out-dir /var/tmp/capybase-live/sX \
        [--max-scenario-seconds 14400]

Or with explicit scenarios (repeatable):
    ... --scenario tikv-history-rebase-0004 --scenario tikv-history-rebase-0005
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "scripts" / "live_eval_scenarios.py"

# The watchdog binds to the scenario's journal via the harness's stdout
# line (s27-61 item 3) — no orphan-worktree-glob guessing.
_WORKTREE_RE = re.compile(r"^\s*worktree=(\S+)", re.MULTILINE)

TMP_MIN_FREE_GB = 10  # AGENTS.md scratch rule


# ---------------------------------------------------------------------------
# lockfile
# ---------------------------------------------------------------------------

def acquire_lock(out_dir: Path) -> bool:
    """True when this process owns the batch lock. Refuses when a live
    holder exists; takes over a stale one."""
    lock = out_dir / "batch.lock"
    if lock.exists():
        prev = None
        try:
            parts = lock.read_text().strip().split()
            prev = int(parts[0])
        except (OSError, ValueError, IndexError):
            prev = None  # unreadable/partial → treat as stale
        if prev is not None:
            try:
                os.kill(prev, 0)  # raises if dead
                # EPERM would mean a LIVE foreign process — refuse too.
                print(f"batch: lock held by live pid {prev} — refusing. "
                      f"(Two batches on one out-dir race the endpoint; the "
                      f"s27-60 lesson.)", flush=True)
                return False
            except ProcessLookupError:
                print(f"batch: stale lock {lock} — taking over", flush=True)
            except PermissionError:
                print(f"batch: lock held by live (foreign-user) pid {prev} "
                      f"— refusing.", flush=True)
                return False
        # fall through: stale — remove it, then create fresh below
        try:
            lock.unlink()
        except OSError:
            pass
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # lost a concurrent create — re-check the holder once more
        print("batch: lock appeared during acquire — retrying", flush=True)
        return acquire_lock(out_dir)
    with os.fdopen(fd, "w") as fh:
        fh.write(f"{os.getpid()} {int(time.time())}\n")
    return True


def release_lock(out_dir: Path) -> None:
    lock = out_dir / "batch.lock"
    try:
        if lock.exists() and lock.read_text().strip().split()[0] == str(os.getpid()):
            lock.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# manifest + resume
# ---------------------------------------------------------------------------

def read_manifest(out_dir: Path) -> list[dict]:
    mf = out_dir / "manifest.json"
    if not mf.exists():
        return []
    try:
        data = json.loads(mf.read_text())
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001 — manifest is advisory, never fatal
        return []


def write_manifest(out_dir: Path, entries: list[dict]) -> None:
    (out_dir / "manifest.json").write_text(json.dumps(entries, indent=2))


def result_is_complete(path: Path) -> bool:
    """A result file counts as done when it parses and carries a verdict.
    INFRA_HANG rows are complete by default (they are RECORDED outcomes);
    --retry-hangs re-runs them."""
    try:
        rows = json.loads(path.read_text())
        return bool(rows) and all(
            isinstance(r, dict) and r.get("verdict") for r in rows)
    except Exception:  # noqa: BLE001
        return False


def result_verdict(path: Path) -> str:
    try:
        rows = json.loads(path.read_text())
        return rows[0].get("verdict", "") if rows else ""
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# process progress probes
# ---------------------------------------------------------------------------

def _proc_io_cpu(pid: int) -> "tuple[int, int] | None":
    """(read_bytes+write_bytes, cpu_ticks) for a pid, or None if gone."""
    try:
        io = {}
        for ln in open(f"/proc/{pid}/io"):
            k, _, v = ln.partition(":")
            if k in ("read_bytes", "write_bytes"):
                io[k] = int(v.strip())
        stat = open(f"/proc/{pid}/stat").read()
        # field 14/15 (utime/stime), after the parenthesized comm
        rest = stat[stat.rindex(")") + 2:].split()
        cpu = int(rest[11]) + int(rest[12])
        return (io.get("read_bytes", 0) + io.get("write_bytes", 0), cpu)
    except (OSError, ValueError, IndexError):
        return None


def _mtime_or_0(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _proc_io_cpu_group(pgid: int) -> "tuple[int, int] | None":
    """(read_bytes+write_bytes, cpu_ticks) summed over the process GROUP.

    s27-66 review: the direct child mostly WAITS — its git children hold
    the io, and a lone-child snapshot is flat during any long git call
    (false-positive kill). Summing the group sees the movers.
    """
    members = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            if os.getpgid(int(pid)) == pgid:
                members.append(int(pid))
        except OSError:
            continue
    if not members:
        return None
    total_io = total_cpu = 0
    for pid in members:
        r = _proc_io_cpu(pid)
        if r is not None:
            total_io += r[0]
            total_cpu += r[1]
    return (total_io, total_cpu)


def _journal_size(journal: Path | None) -> int:
    if journal is None:
        return -1
    try:
        return journal.stat().st_size
    except OSError:
        return -1


class HangDetector:
    """Pure decision function (unit-testable): feed snapshots of
    (journal_size, io_bytes, cpu_ticks) with their timestamps; flat across
    ALL three for the threshold = hang. The reference point is the last
    time the state CHANGED (flat samples don't extend the window). A long
    git replay keeps io moving; an in-flight LLM call bounds flatness by
    the generation deadline + a retry or two — the default 900s sits
    safely above both."""

    def __init__(self, threshold_s: float):
        self.threshold = threshold_s
        self._ref = None  # (t, (journal, io, cpu)) at last change

    def update(self, t: float, journal: int, io: int, cpu: int) -> bool:
        """Record a snapshot; True when a hang is declared."""
        cur = (journal, io, cpu)
        if self._ref is None:
            self._ref = (t, cur)
            return False
        ref_t, ref_state = self._ref
        if cur != ref_state:
            self._ref = (t, cur)
            return False
        return (t - ref_t) >= self.threshold


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------

def cleanup_orphan_worktrees() -> int:
    """Remove /tmp/capy-scen-* worktrees no live process references.

    Killed runs leave worktrees + their registrations behind; they poison
    glob-based journal discovery (the s27-47/60 monitoring trap).
    """
    live_cmdlines = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            live_cmdlines.append(
                open(f"/proc/{pid}/cmdline", "rb").read().decode(
                    "utf-8", "replace"))
        except OSError:
            continue
    blob = "\n".join(live_cmdlines)
    now = time.time()
    removed = 0
    for wt in Path("/tmp").glob("capy-scen-*"):
        if not wt.is_dir():
            continue
        if str(wt) in blob:
            continue  # referenced by a live process — keep
        # s27-66 review: a worktree's path appears in /proc cmdlines only
        # transiently (inside short-lived git subprocess argv) — a LIVE
        # batch's worktree looks orphaned between calls. Recency guard:
        # anything with file activity in the last hour is left alone.
        try:
            newest = max(
                (f.stat().st_mtime for f in wt.rglob("*") if f.is_file()),
                default=wt.stat().st_mtime)
            if now - newest < 3600:
                continue  # recent activity — keep
        except OSError:
            continue
        gitfile = wt / ".git"
        clone = None
        try:
            if gitfile.is_file():
                ptr = gitfile.read_text().strip()
                gd = Path(ptr.split("gitdir:", 1)[1].strip())
                clone = gd.parent.parent  # <clone>/.git/worktrees/<name>
        except (OSError, IndexError):
            pass
        if clone is not None:
            subprocess.run(["git", "-C", str(clone), "worktree", "remove",
                            "--force", str(wt)], capture_output=True)
            subprocess.run(["git", "-C", str(clone), "worktree", "prune"],
                           capture_output=True)
        else:
            shutil.rmtree(wt, ignore_errors=True)
        removed += 1
    if removed:
        print(f"preflight: removed {removed} orphan worktree(s)", flush=True)
    return removed


def tmp_free_gb() -> float:
    out = subprocess.run(["df", "-BG", "--output=avail", "/tmp"],
                         capture_output=True, text=True).stdout.splitlines()
    try:
        return float(out[1].strip().rstrip("G"))
    except (IndexError, ValueError):
        return float("inf")


def preflight(provider: str, datasets: list[str],
              allow_low_disk: bool) -> bool:
    """Endpoint probe + clone completeness + /tmp headroom + orphan
    cleanup. False aborts the batch (each check guards hours of spend).
    """
    # 1) Orphans first (cheap, and keeps later monitoring honest).
    cleanup_orphan_worktrees()

    # 2) /tmp headroom (AGENTS.md: keep >=10G; clickhouse worktrees alone
    # need real headroom — the ENOSPC run of s27-53).
    free = tmp_free_gb()
    if free < TMP_MIN_FREE_GB:
        msg = f"/tmp has {free:.0f}G free (< {TMP_MIN_FREE_GB}G rule)"
        if allow_low_disk:
            print(f"preflight: {msg} — continuing (--allow-low-disk)",
                  flush=True)
        else:
            print(f"preflight: {msg} — aborting (--allow-low-disk to force)",
                  flush=True)
            return False
    else:
        print(f"preflight: /tmp {free:.0f}G free", flush=True)

    # 3) Clone completeness: partial clones hang history-walking on
    # on-demand blob fetches (sweep-12's first attempt). Auto-prepare —
    # the refusal would only send the operator to run the same command.
    sys.path.insert(0, str(REPO / "scripts"))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "live_eval_scenarios", HARNESS)
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    for ds in datasets:
        if ds in ("smoke",):
            continue
        clone = harness._clone_for(ds)
        if clone is not None and harness._is_partial(clone):
            print(f"preflight: {ds} clone is blob-filtered — "
                  f"preparing (one-time)", flush=True)
            harness.prepare_clone(ds)

    # 4) Endpoint probe: one real completion. A dead endpoint must fail
    # the batch here, not three scenarios in.
    try:
        t0 = time.time()
        from capybase.provider_config import resolve_provider, apply_to_config
        from capybase.config import Config
        from capybase.adapters.llm_openai import OpenAICompatibleClient
        cfg = Config()
        cfg, _, _ = apply_to_config(cfg, resolve_provider(provider=provider))
        cfg.model.generation_timeout_seconds = 30
        client = OpenAICompatibleClient(cfg.model)
        resp = client.complete(
            [{"role": "user", "content": "Reply with the single word: ok"}],
            model=cfg.model.model, temperature=0.0, max_tokens=8,
            json_mode=False)
        dt = time.time() - t0
        print(f"preflight: endpoint ALIVE ({dt:.1f}s) "
              f"{resp.text[:20]!r}", flush=True)
        return True
    except Exception as exc:  # noqa: BLE001 — any probe failure aborts
        print(f"preflight: endpoint probe FAILED ({type(exc).__name__}: "
              f"{str(exc)[:120]}) — aborting", flush=True)
        return False


# ---------------------------------------------------------------------------
# per-scenario execution with watchdog
# ---------------------------------------------------------------------------

def run_one(scenario_id: str, provider: str, out_dir: Path,
            flights_dir: Path, *, max_seconds: float | None,
            hang_after: float, poll_s: float = 30.0) -> dict:
    """Run one scenario as a subprocess under the watchdog; return the
    manifest entry. Writes the result file the harness produced, or a
    synthetic INFRA_HANG / SCENARIO_TIMEOUT row.
    """
    out_file = out_dir / f"r-{scenario_id}.json"
    if out_file.exists():
        out_file.unlink()  # a fresh run replaces any prior result
    cmd = [sys.executable, str(HARNESS),
           "--provider", provider,
           "--scenario", scenario_id,
           "--out", str(out_file),
           "--preserve-flights", str(flights_dir)]
    log = out_dir / "run.log"

    attempt = 0
    while True:
        attempt += 1
        t0 = time.time()
        with open(log, "ab") as lf:
            lf.write(f"\n===== {scenario_id} attempt {attempt} "
                     f"{time.strftime('%H:%M:%S')} =====\n".encode())
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=str(REPO), start_new_session=True)
            # s27-66 review: readline BLOCKS — probing behind it meant the
            # watchdog never ran while the child was silent (i.e. during
            # exactly the hangs it exists to catch). Tee via a thread; the
            # main thread owns the timer.
            import queue as _queue
            import threading as _threading
            out_q: _queue.Queue = _queue.Queue()
            worktree: Path | None = None

            def _pump(stream, q):
                for ln in iter(stream.readline, b""):
                    q.put(ln)
                q.put(None)  # EOF sentinel

            _tee = _threading.Thread(
                target=_pump, args=(proc.stdout, out_q), daemon=True)
            _tee.start()

            def _drain_available() -> None:
                nonlocal worktree
                while True:
                    try:
                        line = out_q.get_nowait()
                    except _queue.Empty:
                        return
                    if line is None:
                        return
                    lf.write(line)
                    lf.flush()
                    text = line.decode("utf-8", "replace")
                    print(f"  [{scenario_id}] {text}", end="", flush=True)
                    m = _WORKTREE_RE.match(text)
                    if m and worktree is None:
                        worktree = Path(m.group(1))

            detector = HangDetector(hang_after)
            while True:
                _drain_available()
                if proc.poll() is not None:
                    # Drain any tail the pump already enqueued.
                    while True:
                        try:
                            line = out_q.get(timeout=0.5)
                        except _queue.Empty:
                            break
                        if line is None:
                            break
                        lf.write(line)
                        lf.flush()
                        print(f"  [{scenario_id}] "
                              f"{line.decode('utf-8', 'replace')}",
                              end="", flush=True)
                    break
                now = time.time()
                # deadline
                if (max_seconds is not None
                        and now - t0 >= max_seconds):
                    _kill_tree(proc)
                    _synthetic_result(
                        out_file, scenario_id, "SCENARIO_TIMEOUT",
                        f"exceeded --max-scenario-seconds {max_seconds}",
                        attempt)
                    return _entry(scenario_id, out_file, "SCENARIO_TIMEOUT",
                                  attempt, infra=False)
                # hang
                journal = None
                if worktree is not None:
                    j = worktree / ".rebase-agent" / "sessions"
                    if j.is_dir():
                        js = sorted(
                            j.glob("*/journal.jsonl"), key=_mtime_or_0)
                        if js:
                            journal = js[-1]
                io_cpu = _proc_io_cpu_group(proc.pid)
                jsz = _journal_size(journal)
                if io_cpu is None:
                    break  # process gone; outer loop will reap
                if detector.update(now, jsz, io_cpu[0], io_cpu[1]):
                    print(f"  [{scenario_id}] HANG: journal+io+cpu flat "
                          f"for {hang_after:.0f}s — killing process group",
                          flush=True)
                    _kill_tree(proc)
                    break
                time.sleep(poll_s)
            proc.wait()

        if result_is_complete(out_file):
            verdict = result_verdict(out_file)
            try:
                rows = json.loads(out_file.read_text())
                row = rows[0]
            except Exception:  # noqa: BLE001
                row = {}
            entry = _entry(scenario_id, out_file, verdict, attempt,
                           infra=False)
            entry["elapsed"] = row.get("elapsed")
            entry["files_ok"] = row.get("files_ok")
            entry["session_id"] = row.get("session_id", "")
            return entry

        # No result: hung (or crashed). Retry once, then INFRA_HANG.
        if attempt <= 1:
            print(f"  [{scenario_id}] no result (hang/crash) — retrying "
                  f"once", flush=True)
            continue
        _synthetic_result(
            out_file, scenario_id, "INFRA_HANG",
            "journal+io+cpu flat past the hang threshold on 2 attempts "
            "(endpoint-hang signature) or repeated crash without a result",
            attempt)
        return _entry(scenario_id, out_file, "INFRA_HANG", attempt,
                       infra=True)


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the whole process GROUP (the bash loop and its python — the
    s27-60 lesson: killing only the python lets a parent loop advance to
    the next scenario and race a new batch).

    start_new_session=True pins pgid == proc.pid at spawn, so killpg
    works even after the leader exits (group members — an orphaned
    python holding the endpoint — are exactly what needs killing).
    Re-deriving getpgid at kill time raced the leader's exit and fell
    back to killing only the dead leader (s27-66 review)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _synthetic_result(out_file: Path, scenario_id: str, verdict: str,
                      reason: str, attempts: int) -> None:
    out_file.write_text(json.dumps([{
        "id": scenario_id, "verdict": verdict, "reason": reason,
        "attempts": attempts, "elapsed": None,
    }], indent=2))


def _entry(scenario_id: str, out_file: Path, verdict: str, attempts: int,
           *, infra: bool) -> dict:
    return {
        "scenario": scenario_id,
        "verdict": verdict,
        "result_path": str(out_file),
        "attempts": attempts,
        "infra_hangs": infra,
        "elapsed": None,
        "files_ok": None,
        "session_id": "",
        "reason": "",
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--provider", required=True)
    ap.add_argument("--scenario", action="append", default=[],
                    help="exact scenario id (repeatable)")
    ap.add_argument("--dataset", help="all scenarios of a dataset")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--flights-dir", default=None,
                    help="default <out-dir>/flights")
    ap.add_argument("--no-resume", action="store_true",
                    help="re-run scenarios whose results already exist")
    ap.add_argument("--retry-hangs", action="store_true",
                    help="re-run INFRA_HANG rows on resume (default: skip)")
    ap.add_argument("--no-preflight", action="store_true")
    ap.add_argument("--allow-low-disk", action="store_true")
    ap.add_argument("--max-scenario-seconds", type=float, default=None,
                    help="outer backstop per scenario (the orchestrator's "
                         "per-file budgets don't bound totals)")
    ap.add_argument("--hang-after", type=float, default=900.0,
                    help="journal+io+cpu silence before a hang is declared "
                         "(default 900s)")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    flights_dir = Path(args.flights_dir) if args.flights_dir else out_dir / "flights"
    flights_dir.mkdir(parents=True, exist_ok=True)

    if not acquire_lock(out_dir):
        return 2
    try:
        sys.path.insert(0, str(REPO / "src"))
        sys.path.insert(0, str(REPO / "scripts"))
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "live_eval_scenarios", HARNESS)
        harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness)
        scenarios = harness.select_scenarios(
            args.scenario, args.dataset,
            include_inner_merges=False)
        if not scenarios:
            print("no scenarios selected")
            return 1
        ids = [s["id"] for s in scenarios]
        print(f"batch: {len(ids)} scenario(s) -> {out_dir}", flush=True)

        if not args.no_preflight:
            datasets = sorted({s["dataset"] for s in scenarios})
            if not preflight(args.provider, datasets, args.allow_low_disk):
                return 1

        # Resume: skip completed results (manifest is display; result
        # files are the source of truth).
        pending = []
        for sid in ids:
            rf = out_dir / f"r-{sid}.json"
            if args.no_resume or not result_is_complete(rf):
                pending.append(sid)
                continue
            v = result_verdict(rf)
            if v == "INFRA_HANG" and args.retry_hangs:
                pending.append(sid)
                continue
            print(f"resume: {sid} already {v} — skipping", flush=True)
        if not pending:
            print("batch: nothing to do (all complete)")
            return 0
        print(f"batch: running {len(pending)} scenario(s)", flush=True)

        entries = {e["scenario"]: e for e in read_manifest(out_dir)}
        t_start = time.time()
        for i, sid in enumerate(pending, 1):
            print(f"[{i}/{len(pending)}] {sid}", flush=True)
            entry = run_one(
                sid, args.provider, out_dir, flights_dir,
                max_seconds=args.max_scenario_seconds,
                hang_after=args.hang_after)
            entries[sid] = entry
            write_manifest(out_dir, list(entries.values()))
            print(f"  -> {entry['verdict']} "
                  f"({entry.get('elapsed') or '?'}s) "
                  f"[batch {time.time() - t_start:.0f}s]", flush=True)
        verdicts = {}
        for e in entries.values():
            verdicts[e["verdict"]] = verdicts.get(e["verdict"], 0) + 1
        print("batch summary: " + ", ".join(
            f"{v}×{n}" for v, n in sorted(verdicts.items())), flush=True)
        return 0
    finally:
        release_lock(out_dir)


if __name__ == "__main__":
    sys.exit(main())
