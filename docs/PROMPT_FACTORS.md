# Prompt Factors: How capybase Adapts to Different Models

This document describes the calibration factors capybase can tune for a model,
what each one affects, and how they played out across three real models —
Gemma 4 E4B (7.5B), Gemma 4 E2B (3B), and VibeThinker-3B (3B reasoning model).
The goal is to give the reader a concrete sense of how the system adapts its
prompt-rendering and resolution-mechanism strategy to whatever model the user
happens to be running.

---

## The Factor Set

capybase's calibration can screen up to **13 factors** in a multi-fidelity epoch
loop (a Resolution-IV fractional factorial — 16 runs that sample all factor
combinations). Each factor has two experimental levels ("low" and "high"); the
calibration discovers which level helps and by how much. Not all 13 are
screened on every model — the capability probe drives adaptive selection (a
model that struggles with JSON gets the layout factors; a thinking model gets
the structure factors). The search runs in three epochs of increasing corpus
fidelity, each a valid stopping point — so a calibration can be halted at any
time (Ctrl-C) and still produce a usable profile from the best-so-far results.

The factors fall into two groups: **prompt-rendering axes** (how the prompt is
arranged and framed) and **mechanism/sampling axes** (how the engine draws and
selects candidates).

### Prompt-Rendering Axes

These live on the `PromptProfile` — a process-wide rendering layer that
decides how the engine's analytical content (the three conflict sides,
obligations, structural context) is arranged into the final prompt string.

#### 1. `output_layout` — JSON vs raw fenced code

- **Low (`json_v6`):** the model emits one JSON object whose `resolved_text`
  string holds the merged code. The code is escaped (newlines as `\n`, quotes
  as `\"`).
- **High (`markdown_code`):** the model emits the merged code as a raw fenced
  code block (no escaping), then a small JSON object for metadata. The parser
  extracts the code block verbatim.

**What it affects:** the JSON-escaping burden. Small models frequently corrupt
JSON when the merged code contains embedded quotes, newlines, or backslashes —
they produce mixed real/literal `\n` that breaks the splice. The markdown-code
layout eliminates this entirely: the code goes in a fence, character for
character, and the parser pulls it out without any unescaping.

**Important interaction:** the markdown-code layout requires `json_mode=False`
(server-side `response_format: {type: json_object}` structurally forbids
fenced code blocks — it constrains the model to JSON-only output). The engine
handles this automatically: when the active layout is `markdown_code`, it
forces `json_mode=False` for candidate-producing requests regardless of the
config.

#### 2. `instruction_position` — where the output contract sits

- **Low (`bottom`):** the canonical ordering — intro → data (the conflict
  sides) → output contract → critical rules. The rules are closest to the
  answer (recency bias of an autoregressive model).
- **High (`top_heavy`):** the contract + rules come BEFORE the data payload.
  The model reads the output shape before it sees the conflict sides.

**What it affects:** whether the model knows the target format *before* it
starts reasoning. For thinking models that emit long chains of thought, this
matters: if the model doesn't know it needs to produce JSON (or a fenced code
block) until after it has reasoned over the sides, it may ramble without ever
producing a parseable answer. Stating the contract first gives the reasoning a
target shape.

#### 3. `history_framing` — the commit-history context prose

- **Low (`untrusted`):** the default — a warning that commit messages are
  untrusted metadata (a security guard against prompt injection via commit
  subjects).
- **High (`neutral`):** a softer "Commit context for intent inference:" header,
  without the adversarial framing.

**What it affects:** tone and instruction density. The untrusted warning is
defensive prose that adds to the prompt's instruction load. For a small model
with limited instruction-following capacity, a neutral framing may reduce
confusion. (A `stripped` option — no framing at all — also exists but is not a
screening factor.)

#### 4. `example_limit` — few-shot example density

- **Low (2):** up to 2 similar-past-merge examples, each side truncated to 5
  lines.
- **High (1):** only 1 example.

**What it affects:** context density (lost-in-the-middle). Small models degrade
on dense prompts — a second few-shot example competes for the model's attention
with the actual conflict sides. Fewer examples means more token budget for the
model's reasoning over the real conflict. This is especially relevant for
thinking models whose verbose reasoning needs maximum token headroom.

#### 5. `rule_emphasis` — how the critical rules are formatted

- **Low (`plain`):** rules as plain text lines (the default).
- **High (`formatted`):** rules with emphasis formatting (bold/structure).

**What it affects:** how much the rules stand out. A model that loses rules in
long prompts may benefit from visual emphasis on the critical constraints. Opt-in
via `--enable-factor` or when the capability probe signals instruction-following
weakness.

#### 6. `conflict_summary_mode` — how the conflict sides are presented

- **Low (`full`):** both sides shown in full (the default).
- **High (`intent_only`):** a summarized intent description instead of raw side
  text.

**What it affects:** context length vs fidelity. For very long conflicts,
presenting only the intent summary reduces the prompt size. The risk is losing
exact content the model needs to reproduce. Opt-in.

#### 7. `side_ordering` — which conflict side is presented first

- **Low (`current_first`):** the upstream/current side first (the default).
- **High (`base_first`):** the base/original side first.

**What it affects:** ordering bias. Some models anchor on whichever side they
read first. Opt-in.

### Mechanism / Sampling Axes

These live on `ModelConfig` and control how the engine draws and selects
candidate resolutions.

#### 8. `samples` — draws per fresh resolve

- **Low (1):** a single candidate.
- **High (3):** three independent draws; the engine takes `candidates[0]`
  (with consensus voting when `enable_self_consistency` is on).

**What it affects:** the probability that at least one draw parses correctly.
For a model whose per-call success rate is, say, 40%, three draws give
`1 - (0.6)³ = 78%` chance of at least one success. The cost is 3× latency (or
~1× with server-side batched `n` sampling).

#### 9. `diverse_sampling` — per-sample temperature portfolio

- **Low (False):** all samples at the same temperature.
- **High (True):** split samples across a high exploratory temperature and a
  low conservative temperature.

**What it affects:** candidate diversity. A high-temperature sample explores
alternative merges; a low-temperature sample stays close to a safe answer. This
raises the odds that at least one sample is both valid AND distinct.

#### 10. `prompt_variants` — semantically-equivalent prompt phrasings

- **Low (False):** all samples use the same prompt.
- **High (True):** samples are spread across 3 phrasings of the resolve prompt
  (baseline, constraint-first, minimal-diff-primed).

**What it affects:** robustness to phrasing. A correct merge that's stable
across phrasings is a stronger correctness signal than one that only works
under a single wording.

#### 11. `enable_self_consistency` — majority-vote clustering

- **Low (False):** candidates are ranked by score; the top one wins.
- **High (True):** candidates are clustered by semantic similarity; the largest
  cluster's consensus text wins.

**What it affects:** stability vs best-single-draw. Consensus voting filters
out a lucky-but-wrong outlier in favor of the merge most candidates agree on.
Requires `samples > 1`. Opt-in via `--enable-factor`.

#### 12. `parse_repair_mode` — how the parser handles malformed responses

- **Low (`auto_repair`):** the parser runs `json-repair` salvage on malformed
  responses (the default — lenient).
- **High (`strict`):** skip the repair tier entirely; only exact `json.loads`
  + balanced-object scan run.

**What it affects:** whether a slightly-broken response is salvaged or counted
as a failure. Strict mode surfaces the real parse failure rate (useful for
diagnosing whether repair is masking a deeper issue). Opt-in.

#### 13. `retry_schedule` — CEGIS re-prompt budget

- **Low (`standard`):** `max_retries_per_unit=2` (the default).
- **High (`light`):** `max_retries_per_unit=1` (fewer retries).

**What it affects:** how many CEGIS repair attempts a failing unit gets. A
lighter schedule fails faster (lower latency on hard conflicts); the standard
schedule gives more chances to converge. Opt-in.

---

## How It Played Out: Three Models

All three calibrations below ran the full multi-fidelity epoch search
(screening → refinement → tie-breaker) on 2026-07-12 against live llama-server
/ LM Studio endpoints.

### Gemma 4 E4B (7.5B) — the stable baseline

E4B is a capable model that resolves capybase's 15-conflict calibration corpus
perfectly at the default settings — single sample, no mechanisms, the v6 JSON
layout, bottom instruction position.

**Calibration verdict:** the capability probe measured 100% JSON success and
100% corpus correctness on the spot-check → the early-exit fired correctly. The
DOE was skipped, the baseline locked in. Every mechanism left off
(`samples=1`, all toggles false), prompt profile at the v6 default.

**Profile:**
```
max_tokens=4096, json_mode=True, context_window=8192
samples=1, all mechanisms off
prompt: output_layout=json_v6, instruction_position=bottom
```

**Takeaway:** for a strong model that's near-perfect on both parseability AND
real-merge correctness, calibration's job is to confirm the defaults and skip
the expensive sweep. The corpus-correctness gate on the early-exit ensures this
only fires when the DOE genuinely can't improve the model.

### Gemma 4 E2B (3B) — the diversity-dependent model

E2B initially appeared strong: 100% JSON success on the capability probe. But
the corpus spot-check revealed 80% correctness on real conflicts — below the
0.95 early-exit threshold, so the DOE ran. (Without the spot-check gate, the
early-exit would have fired and locked in the suboptimal default config.)

**Factor ranking (Phase 1):**

| Factor | Direction | \|t-stat\| |
|--------|-----------|------------|
| `samples` | high (3) | 2.0 |
| `diverse_sampling` | high (True) | 2.0 |
| `output_layout` | ≈ (indifferent) | 0.0 |

**Key finding:** `output_layout` is *indifferent* for E2B — both `json_v6` and
`markdown_code` perform identically. The real lever is `diverse_sampling`. This
overturns the earlier assumption (from a 4-conflict live eval) that
markdown-code was E2B's advantage; the full designed experiment showed the
layout doesn't matter, the sampling strategy does.

**Epoch 3 tie-breaker:** two finalists tied at 10/10 on the 10-conflict corpus
(both with `diverse_sampling=True`). On the full 15-conflict corpus:
- `diverse_sampling + json_v6`: **13/15**
- `diverse_sampling + markdown_code`: **15/15** ← winner

The layout was indifferent in screening but broke the tie at full fidelity.

**Profile:**
```
max_tokens=4096, json_mode=True
samples=1, diverse_sampling=True
prompt: output_layout=markdown_code, instruction_position=bottom
```

**Takeaway:** `diverse_sampling` was the decisive lever for E2B (9/10 → 15/15).
The capability probe's parseability check alone (100%) would have falsely
triggered the early-exit — the corpus-correctness spot-check (80%) correctly
prevented this and let the DOE find the improvement.

### VibeThinker-3B (3B reasoning model) — the thinking-model challenge

VT3B is a permanently-thinking model: it emits thousands of tokens of chain-of-
thought reasoning before its answer, and this reasoning length is highly
variable. Single-sample probes were dominated by noise (whether the model
finished reasoning within the token budget was a coin-flip).

**Factor ranking (Phase 1, 5-conflict corpus):**

| Factor | Direction | \|t-stat\| |
|--------|-----------|------------|
| `samples` | high (3) | **3.9** |
| `diverse_sampling` | low (off) | 1.1 |
| `instruction_position` | low | 0.3 |
| `output_layout` | low | 0.3 |

**Key finding:** `samples=3` is by far the dominant factor (|t|=3.9 — 4×
stronger than anything else). `diverse_sampling` actively *hurts* VT3B (the
high-temperature exploratory draws waste the limited token budget). The prompt
axes (`instruction_position`, `output_layout`) were weak on the 5-conflict
screening corpus but the refinement surfaced their effect.

**Epoch 2 refinement (10-conflict corpus):** the winning configuration
(`samples=3 + instruction_position=top_heavy + json_v6 + diverse_sampling=off`)
scored **8/10** — a 2.7× improvement over the default config's 3/10 on the same
corpus.

**Profile:**
```
max_tokens=32768, json_mode=False (thinking prose defeats the json_object grammar)
context_window=32768
samples=3, diverse_sampling=off
prompt: output_layout=json_v6, instruction_position=top_heavy
```

**Takeaway:** for a thinking model, `samples=3` is the overwhelming lever —
more draws means more chances one finishes reasoning and produces a parseable
answer. `diverse_sampling` hurts (it dilutes the token budget). The large
`max_tokens` (32768) accommodates VT3B's ~13K-char thinking chains. The
`json_mode=False` finding reflects that VT3B's server emits valid JSON in
`message.content` but the reasoning prose defeats the `response_format` grammar
constraint.

---

## The Adaptation in Summary

| Model | samples | diverse_sampling | output_layout | instruction_position | corpus score |
|-------|---------|-----------------|---------------|---------------------|--------------|
| E4B (7.5B) | 1 | off | json_v6 | bottom | early-exit (100% spot-check) |
| E2B (3B) | 1 | **on** | markdown_code | bottom | **15/15** |
| VT3B (3B reasoning) | **3** | off | json_v6 | **top_heavy** | **8/10** |

The same system, three very different adaptations:
- **E4B:** nothing needed — near-perfect on both parseability and real
  correctness, so the early-exit correctly skips the DOE.
- **E2B:** `diverse_sampling` is the lever — it lifted the score from 9/10 to
  15/15. The layout (markdown_code) only broke the final tie.
- **VT3B:** `samples=3` is the dominant lever (|t|=3.9), while
  `diverse_sampling` actively hurts. The thinking model needs more draws, not
  more diversity.

The calibration discovers which lever matters for each model through the
designed experiment, rather than brute-force enumeration of all combinations.

---

## How to Calibrate Your Model

```bash
# Full calibration (capability probes + multi-fidelity epoch sweep):
capybase calibrate

# Noise-robust calibration for thinking models (3 reps per design point):
capybase calibrate --calibrate-reps 3

# Screening only — report the factor ranking without committing to a selection:
capybase calibrate --calibrate-phase1-only

# Force a specific factor into the screening (bypasses adaptive selection;
# also bypasses the early-exit so the DOE runs even for strong models):
capybase calibrate --enable-factor output_layout

# Calibrate a specific task family (config_merge, test_port):
capybase calibrate --task config_merge
capybase calibrate --list-tasks

# Quick capability check (no corpus sweep):
capybase calibrate --dry-run
```

### The early-exit

The capability probe measures both parseability (JSON success on a trivial
conflict) and correctness (a spot-check on 3 real corpus conflicts). A model
that scores ≥95% on **both** triggers the early-exit — the DOE is skipped and
the baseline is locked in. This only fires when the DOE genuinely can't improve
the model. `--enable-factor` bypasses the early-exit entirely.

### Anytime halt

Calibration runs in three epochs of increasing corpus fidelity (screening →
refinement → tie-breaker). **You can Ctrl-C at any point after the first
completed evaluation** — the probe catches the interrupt, finalizes from the
best-so-far configuration at the highest fidelity reached, and persists it
normally (fast finalize: no baseline re-eval on interrupt).

```
calibrate: Epoch 1/3 screening (16 points, 5 conflicts)...
calibrate:   -> 3/5 correct
...
^C
calibrate: interrupted after epoch 1 — using best-so-far
wrote profile to: ~/.config/capybase/model_profile.json
```

The earlier the halt, the less refined the profile, but every epoch boundary is
a valid stopping point. A full run is unaffected.

### Profile location

The calibrated profile is written to `~/.config/capybase/model_profile.json`
(the shared config dir, so it's available across all repos on the machine — the
model doesn't vary by directory). Override with `[calibration]
model_profile_path` or `--profile`. The profile is applied automatically on
every `capybase rebase` run when the model name matches. An explicit env-var
override (`CAPYBASE_PROMPT_LAYOUT=markdown_code` etc.) always wins over the
calibrated profile — useful for A/B testing.
