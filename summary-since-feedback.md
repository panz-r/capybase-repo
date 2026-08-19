# capybase — Changes Since Last Review

This document summarizes the work done since the last review feedback. All
changes are on the `dev` branch. No external dependencies were added.

## Live-eval result: 0% → 38.4% success rate

The C corpus live eval went from **0 of 73 cases passing** (v2 baseline) to
**28 of 73 passing** (v8, all improvements applied). By dataset: json-c
10/16 (62.5%), redis 16/22 (72.7%), sqlite 2/35 (5.7% — 29 of 33 failures
are case timeouts from the slow `./configure && make` build × model latency,
not capability gaps).

## Build-gate error localization (5 commits)

The build gate had no error-localization logic — it picked the first stderr
line containing "error" and attributed it to the conflict file regardless of
which file gcc actually named. This rejected 12+ correct merges (all sim ≥
0.99) because a pre-existing error in a sibling file (tool/lemon.c,
deps/hiredis.c) was treated as a merge defect.

Added three classification mechanisms:
- **Sibling-file detection**: parse gcc's `file:line:col:` prefix, compare
  the file stem against the conflict file. Errors in sibling files
  (lemon.c while resolving delete.c) are compile-pass.
- **-Werror promotion detection**: detect the `[-Werror=...]` tag that
  distinguishes warning-promoted errors from real compile failures.
- **Build-driver line skipping**: skip `make[2]: ***` / `CMake Error` lines
  that reference build targets, not source files.

Also added: `unknown type name` to the GCC semantic pattern list (GCC's
wording for undefined typedefs, complementing clang's `does not name a type`),
header include paths (`-I`) for standalone gcc compilation, and header-file
skip for the per-unit CCS gate (headers are never compiled standalone).

## Timeout and repair infrastructure (5 commits)

Four infrastructure bugs were causing timeouts, crashes, and missed repairs:

- **Exit-A repair bypass**: when the LLM re-resolve escalated, the
  deterministic-only repair pass was skipped entirely (a guard accidentally
  excluded it). 7 deterministic repair mechanisms now always get a final shot.
- **Timeout/cleanup race**: `shutil.rmtree` ran before checking if the worker
  thread was still alive, destroying the temp dir mid-operation. Deferred
  cleanup to end-of-run for timed-out cases.
- **File-level wall deadline**: the whole-file repair loop created nested
  `_resolve_unit` calls each with a fresh 360s budget, causing real wall clock
  to explode past the case timeout. Added `max_wall_time_per_file_seconds`
  that threads a monotonic deadline through both Phase 1 and Phase 2.
- **Adaptive build-system detection**: json-c's older commits use autotools,
  not cmake. The prepare step now probes the tree (CMakeLists.txt vs
  configure.ac) and adapts, falling back gracefully when configure fails.

## C skeleton extractor improvements (3 commits)

Systematic testing across all 70 corpus files revealed three limitations:

- **`extern "C" { ... }`**: the brace swallowed all declarations inside.
  4 header files (json_object.h etc.) extracted 0 functions. Now detected
  and skipped — json_object.h correctly yields 41 functions.
- **Function-pointer typedefs**: `typedef T (*name)(...)` was invisible
  (the name is inside parens, which the scanner strips from the buffer).
  145 fp-typedefs across the corpus now correctly classified.
- **Typedef struct body alias**: `typedef struct Foo { ... } Foo` lost the
  alias (split across the `{` boundary). Now tracked via pending-typedef flag.

## Five new mechanisms from reviewer feedback (5 commits)

All five items from the agreed reviewer priorities:

- **gcc fix-it hints** (`-fdiagnostics-parseable-fixits`): apply the
  compiler's own structured repair suggestions inside the deterministic
  repair beam. Subsumes the regex-based cc-repair — gcc covers more error
  types with surgical precision.
- **git merge-file --union**: added as a 6th source-derived portfolio
  candidate. Handles disjoint appends that concatenation heuristics miss.
- **LCS-based side-consistency repair**: re-insert dropped common lines at
  the correct position using 2-line context matching from the base, instead
  of blindly inserting at the error line.
- **Block-aware combination search**: group lines into logical blocks
  (blank lines, indentation drops, preprocessor directives) before
  interleaving. Prevents splitting if/for/while blocks across insertion
  boundaries and reduces the search space.
- **Function-local context in prompt**: the enclosing function signature
  and ±3 lines of pre/post-conflict context from the worktree. Gives the
  model local scope awareness (parameter types, variable declarations) at
  ~100-token cost.

## Remaining gap

sqlite cases (29 of 35) timeout because the 35K-char files × ~100s/generation
× multiple CEGIS iterations × 54s/build-verification exceed the 900s case
timeout. When sqlite cases DO complete, they resolve correctly (sim 0.97–1.00).
This is a throughput bottleneck, not a capability gap — the deferred
oversized-conflict splitting at function boundaries would address it.
