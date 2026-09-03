# Corpus test suite (NOT pytest)

These checks drive REAL corpus cases (extracted-testdata/ + the
external-datasets clones) through the real verification engine — real
git worktrees, real cargo/make. They are deliberately outside pytest:

- pytest runs ONLY unit tests that need nothing external fetched.
- The corpus suite has its own execution model and contract
  (multi-GB fetches, worktree hygiene, serial builds) and its own
  commands. NEVER move these back under tests/.

## Setup (once — fetches the corpora)

    .venv/bin/python scripts/fetch_mergeconflict_datasets.py --language python --limit 50
    # and the C/Rust datasets as needed (see the script's DATASETS registry)

## Run

    ./corpus/run.sh            # all corpus checks
    ./corpus/run.sh python     # the Python (zenodo-hdiff) subset
    ./corpus/run.sh rust       # the Rust cargo-worktree subset

## What the corpus suite IS (and is NOT)

**Deterministic only — zero model calls.** The checks drive the
human-authored merge M (the oracle) through the VerificationEngine's
real floors: py_compile (Python), gcc -fsyntax-only (C standalone),
and real `cargo check` in a per-case git worktree (Rust — the only
honest signal for `crate::` resolution). They validate the verifier
and the corpus oracle against real-world conflict shapes. The LIVE
tests (real model calls through the full orchestrator) are the EVAL
harness — `scripts/live_eval_realworld.py --provider NAME` — a
separate command with its own provider/calibration contract.

**File inventory**: setup/download = `scripts/fetch_mergeconflict_datasets.py`
(+ `scripts/mine_rebase_scenarios.py` for the scenario family); run =
`corpus/run.sh` → `corpus/run.py`; checks = `corpus/checks.py`.
`scenario_checks_pending.py` is the not-yet-ported scenario family
(pytest-style, excluded from the runner until ported).
