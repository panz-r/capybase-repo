# Sprint-20 capabilities delta (S20.E12)

Factual deltas to `capabilities-c.md` / the system's C-C++ conflict
capabilities, per the labeling rule: **unit-tested ≠ live-exercised ≠
corpus-proven** — each line states its evidence level.

## New deterministic capabilities (unit-tested + live-exercised)

- **Toolchain-era preflight**: a conflict whose two pristine sides AND
  the oracle all fail the real build gate with identical compile-error
  signatures is classified `ESCALATE_TOOLCHAIN` before any budget is
  spent (tokio-0109: 3/3 in 8.9s). Live-proven on four corpus cases.
- **Lockfile generated-file takeover**: `Cargo.lock` conflicts resolve
  to the current side's pristine file pre-cascade, verified through the
  standard machinery; lockfile-named cases bypass the prompt-size guard.
  Live-proven: axum-0015/0017 PASS at sim 1.00 in 18-23s (0017
  previously burned 103 LLM units for a WORKING at sim 0.625).
- **Anchor-scoped mechanical re-application**: duplicate-repair /
  mechanical-substitution merges apply only at base positions that
  survived into the semantic side (the R10 fix). Suite-proven (the
  xfail is closed); live-proven via the suite's regression fixtures.

## New repair rungs (unit-tested; live firing not yet observed)

- **Micro-CEGIS at the compiler-authority gate**: deterministic
  redefinition-deletion (provenance-checked) + missing-symbol
  SEARCH/REPLACE micro-patch, re-gated by the same command before any
  escalate. Unit-tested; the harvest journals are the live census.
- **Sibling-boundary brace insertion**: mid-file missing closers
  insert at the next same-scope construct, not EOF. Unit-tested; both
  originally-planned corpus cases proved era-dead (not model
  failures).
- **Prompt compaction (context-only)**: comment/blank stripping of
  context sections before the budget's drop cascade. Unit-tested; no
  live context-dominated overflow case exists in the current corpus.

## Measurement stages (journal-only, enabling data-gated)

- **Move-and-edit shape detection** (corpus population: 3/677) and the
  **class-member split candidate** stamping carry on; enabling follows
  the pre-registered thresholds in `docs/sprint21-decision-template.md`.
- **Skeleton intent similarity** (eval-only diagnostic, never a gate):
  recorded on every eval result beside token jaccard.

## Reliability (operational, live-proven this sprint)

- Build teardown kills whole process trees (the load-92 incident class);
  ccache verified at 100% cross-worktree hit rate (case wall 70s→11s
  warm); a shared stale-process sweep runs at every entry point.
- Empty-resolution content failures get one reframed recovery retry
  (transport failures excluded by design — they retry plain).

The harvest (`S20.12`) upgrades evidence levels where journals show
live firings; this delta is updated accordingly at sprint close.
