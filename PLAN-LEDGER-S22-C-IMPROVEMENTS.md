# Sprint-22 C-Shard Improvement Plan (from three-reviewer synthesis)

## Reviewer rating

**Reviewer 3 — most useful.** The strategic frame is the best insight
across all three: "Python is fighting the model-capability frontier;
C is fighting the compile-validation frontier." The model produces
98-100% correct merges (every Class A case at sim ≥ 0.981); the
escalations are single-symbol compile errors. The fix is deterministic
post-generation repair, not better reasoning. R3's "what I would NOT
do" section is precise and correctly identifies that model size,
compile-gate relaxation, and few-shot examples are all wrong answers.
R3's disagreement with "no repairs" for the variance class is
well-argued — the deterministic repair interleaved into the retry loop
converts variance cases where the defect is a missing symbol.

**Reviewer 1 — solid consensus builder.** The symbol injection
mechanism is well-specified with the clearest implementation steps.
The "aggressive prompt compaction" partially overlaps S20.9. The
"candidate selection" (multiple LLM calls per unit) is interesting but
2-3x cost for uncertain gain on a 4B model.

**Reviewer 2 — strong on context injection, but repeats the rejected
skeleton override.** The "Preceding Block Injection" is a genuinely
good idea (inject resolved context before a failing unit's prompt).
The AST skeleton override is rejected for the SECOND TIME — same
reasoning as the Python round.

## The synthesized plan

Consensus across all three reviewers: **side-provenance symbol
injection is the highest-leverage mechanism** — the compiler tells you
exactly what's wrong, the sides contain the fix, the system just
needs to connect them.

| # | Item | Source | Target cases | Effort | Priority |
|---|------|--------|-------------|--------|----------|
| C1 | Side-provenance symbol injection (deterministic missing-symbol repair) | all 3 (R1#1, R2#1, R3#1) | redis-0002/0012, sqlite-0030, redis-0013 | 4-6h | **P0** |
| C2 | Include-directive repair (implicit-declaration class) | R1#2, R3#2 | redis-0013/0014 | 3-4h | **P1** |
| C3 | Preceding-block injection (context expansion for stubborn units) | R2#5 | sqlite-0029, redis-0015/0049 | 2h | **P2** |
| C4 | Deterministic repair interleaved into retry loop | R3#4 | Class B variance (6 cases) | 3h | **P3** |
| C5 | Oversized-prompt splitting diagnosis (sqlite-0004) | R1#3, R3#3 | sqlite-0004 | 2h diag | **P4** |
| C6 | Unit re-resolve archaeology (stubborn units) | R3#7 | sqlite-0029, redis-0015/0049 | 2h | **P5** |
| C7 | Branch-stall archaeology (redis-0054/0055) | R3#6 | redis-0054/0055 | 2h | **P6** |

### Explicitly rejected (second occurrence for some)

- **AST Skeleton Intent Override** (R2, AGAIN): same rejection as the
  Python round — violates pre-registration, metric gaming, the
  eval-only constraint exists precisely to prevent this
- **Mid-band fast path extension** (R1#5): needs calibration data
  from the sharded harvest; cannot enable on shape metrics alone
  (the sprint-18 WS4 lesson)
- **Candidate selection with multiple LLM calls** (R1#6): 2-3x cost
  for uncertain gain; the 4B model's variance is compile-level, not
  reasoning-level — more candidates won't produce compile-clean output
  more reliably than deterministic repair

### Design principle (from R3's strategic frame)

C-shard improvements are **repair-layer**, not **reasoning-layer**.
The model already produces correct merges. The mechanisms connect
compiler errors to side-content fixes. No prompt changes, no model
routing, no few-shot additions.

## Implementation notes

### C1: Side-provenance symbol injection

```
Parse error → extract symbol name + error type
  'X' undeclared → search base/current/replayed for X's declaration
  implicit declaration of 'F' → search for F's prototype
  unknown type name 'T' → search for T's typedef/struct
Find the declaration line in the sides
Determine injection point (same scope level, before first use)
Inject verbatim from the side → re-compile → accept if clean
```

Safety: the declaration comes from a merge side (not invented); the
compiler validates; a failed injection falls through to escalation.

### C3: Preceding-block injection

When a unit fails validation twice:
1. Look at the working buffer (all previously resolved units spliced)
2. Extract 10 lines immediately preceding the current conflict marker
3. Inject into the LLM prompt as LOCKED_PRECEDING_CONTEXT
4. Instruction: "Your resolution must be compatible with this code."

This bridges independent unit resolution and whole-file coherence.

### C4: Repair-interleaved retry

```
Round 1: Model generates → compile fails → attempt C1/C2 repair → if fixed: ACCEPT
Round 2: Model generates WITH Round 1 error context → compile fails → attempt repair → if fixed: ACCEPT
Round 3: Final → if still fails: ESCALATE
```

The deterministic repair catches missing-symbol/include cases; the
error-context retry helps the model avoid the same mistake.
