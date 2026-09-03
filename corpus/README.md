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
