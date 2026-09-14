"""Deterministic seed registration (s27-78): the seeds family goes native.

The three census-perfect arms (content-convergence 1080/1080, transient
deletion 499/499, generator-output take-source 30/30 + 8/8 + the s27-71
12/12 benign recheck) were built and censused against the EVAL HARNESS,
which registers ``orch._convergence_seeds`` before the orchestrator runs.
This module computes the SAME seeds inside a production rebase: during
``git rebase --onto TARGET BASE SOURCE`` the rebase state pins SOURCE's
tip (``rebase-merge/orig-head``) and TARGET's tip (``rebase-merge/onto``)
— exactly the two mined oids the arms compare — for as long as the rebase
is in progress.

Semantics are rebase-relative, not harness-relative: "the two final states
THIS rebase reconciles" replace "the two mined branch tips". The harness's
replay IS a production-shaped rebase, so the census transfers. The native
universe is a DOCUMENTED SUPERSET of the harness's (--name-status records
rename source paths that --name-only drops; absent-at-both still means
deletion is the agreed state), so dicts can differ by those parity notes;
the harness's registration stays authoritative when already present.

Best-effort by contract: a blob read that fails or overruns the wall-clock
budget just shrinks the seed dict (mechanisms decline; the cascade runs).
"""
from __future__ import annotations

import re
import time

#: Generator-output basename pattern (s27-57/60; the strict grammar/pb/tab
#: family plus the s27-67 broadened ``generated_`` alternative — censused
#: 12/12 benign across the scenario corpus, s27-71). Single source of truth:
#: the eval harness imports THIS pattern, so smoke() asserts the production
#: regex and the native registrar uses it.
_GEN_OUTPUT = re.compile(
    r"(compiled_grammar|inlined_grammar|\.pb\.cc|\.pb\.h|"
    r"\.generated\.|\.tab\.c|\.yy\.c|"
    r"transform_generated_|_generated\.|generated_)",
    re.IGNORECASE)


def compute_convergence_seeds(
    git,
    *,
    target_tip: str,
    source_tip: str,
    merge_base: str | None = None,
    paths: set[str] | None = None,
    budget_seconds: float = 60.0,
) -> dict[str, str]:
    """Compute the seed dict for one rebase from its two final states.

    Arms (in the harness's order, per touched path):
    - both tips carry the SAME blob -> convergence seed = that content
      (the replay's conflict is transient churn over a decided state);
    - absent at BOTH tips -> transient-file delete-seed ``""``;
    - sides disagree AND the basename matches the generator pattern ->
      take-source seed = the SOURCE tip's blob verbatim.

    ``paths`` is the log-union of per-commit touches over
    ``merge_base..source_tip`` — the net diff hides changed-and-reverted
    files whose replay still conflicts (the compiled_grammar.cpp lesson).

    Best-effort: any git failure or the wall-clock budget ends the scan
    with whatever was gathered. Returns {} when the refs don't resolve.
    """
    seeds: dict[str, str] = {}
    deadline = time.monotonic() + budget_seconds
    if paths is None:
        # standalone path: derive the universe from the range walk
        try:
            mb = git.merge_base(target_tip, source_tip) or merge_base
            commits = git.replayed_commit_sequence(mb, source_tip)
        except Exception:  # noqa: BLE001 — seeds are advisory
            return seeds
        paths = set()
        for c in commits:
            paths.update(c.get("touched_files") or [])
    for p in sorted(paths):
        if time.monotonic() > deadline:
            break  # keep partial coverage; mechanisms decline on misses
        try:
            # s27-80: equality via blob OIDs — blob_at's decode-with-replace
            # is not byte-faithful, and two distinct blobs differing only in
            # invalid-UTF-8 bytes compared EQUAL as bytes, writing a
            # corrupted replacement-char file as a "converged final state".
            # The harness compares rev-parse OIDs; so does the native
            # registrar now. Bytes are fetched only when a seed needs them.
            cur_oid = git.blob_oid_at(target_tip, p)
            src_oid = git.blob_oid_at(source_tip, p)
        except Exception:  # noqa: BLE001 — on-demand fetch / IO trouble
            continue
        if cur_oid is None and src_oid is None:
            # TRANSIENT FILE: absent at both tips — born and deleted inside
            # the replay window. Final state is deletion (census 499/499).
            seeds[p] = ""
            continue
        if cur_oid is None or src_oid is None or cur_oid != src_oid:
            # Generator-output exception: when the sides disagree, the
            # regenerated (source) version wins — 30/30 census.
            if src_oid is not None and _GEN_OUTPUT.search(p.rsplit("/", 1)[-1]):
                src = git.blob_at(source_tip, p)
                if src is None:
                    continue
                try:
                    src.decode("utf-8")
                except UnicodeDecodeError:
                    # s27-85: non-UTF-8 generator output — same skip
                    # rationale as the convergence arm above.
                    continue
                seeds[p] = src.decode("utf-8", errors="replace")
        elif cur_oid is not None:
            cur = git.blob_at(target_tip, p)
            if cur is None:
                continue
            try:
                cur.decode("utf-8")
            except UnicodeDecodeError:
                # s27-85: a non-UTF-8 converged blob (generated/compiled
                # content) would be written lossily through the text
                # pipeline (errors="replace" mangles bytes -> the
                # "converged final state" is a mangled file). Skip — the
                # file doesn't conflict textually, so the cascade or the
                # byte-faithful checkout handles it.
                continue
            seeds[p] = cur.decode("utf-8", errors="replace")
    return seeds
