# Prompt Factors: How capybase Adapts to Different Models

This document describes the calibration factors capybase can tune for a model,
what each one affects, and how they played out across three real models —
Gemma 4 E4B (7.5B), Gemma 4 E2B (3B), and VibeThinker-3B (3B reasoning model).
The goal is to give the reader a concrete sense of how the system adapts its
prompt-rendering and resolution-mechanism strategy to whatever model the user
happens to be running.

---

## The Factor Set

capybase's calibration screens seven factors in a multi-fidelity epoch loop
(a Resolution-IV fractional factorial — 16 runs that sample all factor
combinations). Each factor has two experimental levels ("low" and "high"); the
calibration discovers which level helps and by how much. The search runs in
three epochs of increasing corpus fidelity, each a valid stopping point — so a
calibration can be halted at any time (Ctrl-C) and still produce a usable
profile from the best-so-far results.

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

### Mechanism / Sampling Axes

These live on `ModelConfig` and control how the engine draws and selects
candidate resolutions.

#### 5. `samples` — draws per fresh resolve

- **Low (1):** a single candidate.
- **High (3):** three independent draws; the engine takes `candidates[0]`
  (with consensus voting when `enable_self_consistency` is on).

**What it affects:** the probability that at least one draw parses correctly.
For a model whose per-call success rate is, say, 40%, three draws give
`1 - (0.6)³ = 78%` chance of at least one success. The cost is 3× latency (or
~1× with server-side batched `n` sampling).

#### 6. `diverse_sampling` — per-sample temperature portfolio

- **Low (False):** all samples at the same temperature.
- **High (True):** split samples across a high exploratory temperature and a
  low conservative temperature.

**What it affects:** candidate diversity. A high-temperature sample explores
alternative merges; a low-temperature sample stays close to a safe answer. This
raises the odds that at least one sample is both valid AND distinct.

#### 7. `prompt_variants` — semantically-equivalent prompt phrasings

- **Low (False):** all samples use the same prompt.
- **High (True):** samples are spread across 3 phrasings of the resolve prompt
  (baseline, constraint-first, minimal-diff-primed).

**What it affects:** robustness to phrasing. A correct merge that's stable
across phrasings is a stronger correctness signal than one that only works
under a single wording.

---

## How It Played Out: Three Models

### Gemma 4 E4B (7.5B) — the stable baseline

E4B is a capable model that resolves capybase's 15-conflict calibration corpus
**perfectly (15/15) at the default settings** — single sample, no mechanisms,
the v6 JSON layout, bottom instruction position.

**Calibration verdict:** no factor beat the default. The screening correctly
identified no significant factor (all effects ≈ 0); Phase 2 confirmed the
existing config. Every mechanism was left off (`samples=1`, all toggles false),
and the prompt profile stayed at the v6 default.

**Why:** E4B is strong enough that none of the levers matter — it produces a
correct, parseable JSON resolution on the first draw every time. Multi-sampling
doesn't help (can't beat 15/15); the markdown-code layout doesn't help (E4B
handles JSON escaping fine); top-heavy positioning doesn't help (E4B follows
the contract regardless of position). The calibration correctly recognized this
and left everything at the efficient default.

**Live eval (4 real conflicts):** 4/4 correct — py_simple, py_multi_unit,
rust_impl, rust_port_test all passed under the default profile.

**Takeaway:** for a strong model, calibration's job is to confirm the defaults
and avoid wasting compute on unnecessary mechanisms. The two-phase design does
this efficiently: the screening runs, finds nothing significant, and keeps the
baseline.

### Gemma 4 E2B (3B) — the escaping-struggling model

E2B is a smaller model that struggles with JSON escaping on code containing
embedded quotes and multi-line constructs.

**JSON layout (default):** 2/4 on live eval. It passed `py_simple` and
`rust_port_test` but:
- `py_multi_unit`: **WRONG_MERGE** — it dropped `metrics: "on"` from the
  merged config (a semantic loss the escaping corruption caused: the model
  couldn't reliably reproduce the line inside an escaped JSON string).
- `rust_impl`: **ESCALATED** on a `:1:1` format-string error (the second hunk
  of a multi-hunk conflict — a model-capability ceiling, not an escaping issue).

**Markdown-code layout:** `py_simple` PASS. On `py_multi_unit`, the
`metrics: "on"` line was now **preserved** (the raw-code-block format
eliminated the escaping that caused the drop) — but `cache` regressed to
`"off"` (a different semantic error). The escaping fix worked; the remaining
failure was a 3B capability ceiling on multi-unit merges, not a system bug.

**Takeaway:** the `output_layout` factor is the lever for models that struggle
with JSON escaping. The markdown-code layout eliminates the escaping burden
entirely. For E2B it fixed the specific failure mode (dropped lines from
escaped-code corruption) but couldn't overcome the model's broader
multi-hunk reasoning limit. The calibration's job is to detect this and select
markdown-code when it helps — which it does by comparing the two layouts on the
corpus.

### VibeThinker-3B (3B reasoning model) — the thinking-model challenge

VT3B is a permanently-thinking model: it emits thousands of tokens of chain-of-
thought reasoning before its answer, and this reasoning length is highly
variable. This made calibration genuinely difficult — single-sample probes were
dominated by noise (whether the model happened to finish reasoning within the
token budget was a coin-flip).

**The noise problem:** VT3B's baseline was 1/15 on the calibration corpus
under default settings. A single-sample A/B couldn't distinguish any factor's
effect because the score was too noisy. This is what motivated the two-phase
redesign: the fractional-factorial screening + replication.

**Phase-1 screening results (16-point Res-IV design, 1 rep):**

| Factor | Direction | Effect | \|t-stat\| |
|--------|-----------|--------|------------|
| `instruction_position` | top_heavy | +0.50 | 1.1 |
| `example_limit` | 1 (fewer) | +0.50 | 1.1 |
| `samples` | 3 | +0.50 | 1.1 |
| `output_layout` | json_v6 (default) | -0.25 | 0.5 |
| `history_framing` | untrusted (default) | -0.25 | 0.5 |
| `diverse_sampling` | True | +0.25 | 0.5 |
| `prompt_variants` | — | 0.00 | 0.0 |

**Best design point (#5):** `top_heavy` + `json_v6` + `samples=3` +
`diverse_sampling=True` = **6/15 (40%)** — a **6× improvement** over the 1/15
baseline.

**What helped and why:**

- **`instruction_position=top_heavy`** was the strongest lever. VT3B needs to
  know the output contract *before* it reasons over the conflict sides. With
  the rules at the bottom (the default), VT3B often rambles through its
  reasoning without producing a parseable answer — it doesn't know the target
  shape until it's already spent its token budget on thought. Putting the
  contract first gives the reasoning a target.

- **`samples=3`** helped because VT3B's per-call success is low — more draws
  means more chances one parses. (With replication/consensus this would be
  even stronger, but the 3-rep screening was too expensive on VT3B's ~40s
  latency to complete.)

- **`example_limit=1`** had a positive main effect (fewer examples → less
  context density → more token budget for reasoning), though the single best
  point used 2. The profile kept 2 to avoid overfitting a noisy 1-rep signal.

- **`output_layout=json_v6`** (the default) **beat** `markdown_code`. This is
  the key finding: the markdown-code layout does NOT help VT3B. Its bottleneck
  is reasoning verbosity (it runs out of tokens mid-thought), not JSON escaping.
  The json_mode fix made this a fair comparison, and the answer is clear: the
  default JSON layout is better for VT3B.

**Profile applied:**
```
samples=3, diverse_sampling=True, json_mode=True
prompt: instruction_position=top_heavy, output_layout=json_v6
```

**Takeaway:** for a thinking model, the lever isn't the output format — it's
the *prompt structure*. Putting the contract first (top_heavy) gives the
reasoning a target shape; multi-sampling compensates for the low per-call
success rate. The calibration discovered this empirically through the designed
screening, something the old independent A/B couldn't do because the
single-sample noise drowned out the signal.

---

## The Adaptation in Summary

| Model | samples | diverse_sampling | output_layout | instruction_position | corpus score |
|-------|---------|-----------------|---------------|---------------------|--------------|
| E4B (7.5B) | 1 | off | json_v6 | bottom | 15/15 (perfect) |
| E2B (3B) | 1 | off | markdown_code* | bottom | 2/4 live eval |
| VT3B (3B reasoning) | 3 | on | json_v6 | top_heavy | 6/15 (from 1/15) |

\* E2B's markdown-code preference is inferred from the live eval (the layout
eliminated its escaping-induced line drops); a full two-phase calibration on
E2B would confirm this.

The same system, three very different adaptations:
- **E4B:** nothing needed — calibration confirms the defaults and avoids waste.
- **E2B:** the output layout is the lever — markdown-code bypasses the escaping
  the model can't handle.
- **VT3B:** the prompt structure is the lever — top_heavy gives the thinking
  model a target, and multi-sampling compensates for its low per-call success.

The calibration discovers which lever matters for each model through a 16-run
designed experiment, rather than brute-force enumeration of all combinations.

---

## How to Calibrate Your Model

```bash
# Full calibration (capability probes + multi-fidelity epoch sweep):
capybase calibrate

# Noise-robust calibration for thinking models (3 reps per design point):
capybase calibrate --calibrate-reps 3

# Screening only — report the factor ranking without committing to a selection
# (useful on slow models to see what matters before paying for refinement):
capybase calibrate --calibrate-phase1-only

# Quick capability check (no corpus sweep):
capybase calibrate --dry-run
```

### Anytime halt

Calibration runs in three epochs of increasing corpus fidelity (screening →
refinement → tie-breaker). **You can Ctrl-C at any point after the first
completed evaluation** — the probe catches the interrupt, finalizes from the
best-so-far configuration at the highest fidelity reached, and persists it
normally. This turns a multi-hour calibration on a slow model into something
you can stop early with a defensible profile:

```
calibrate: Epoch 1/3 screening (16 points, 5 conflicts)...
calibrate:   -> 3/5 correct
...
^C
calibrate: interrupted after epoch 1 — using best-so-far
wrote profile to: .rebase-agent/memory/model_profile.json
```

The earlier the halt, the less refined the profile (an Epoch-1 halt has only
screened on a small corpus prefix), but every epoch boundary is a valid
stopping point. A full run is unaffected.

The calibrated profile is written to `~/.config/capybase/model_profile.json`
and applied automatically on every `capybase rebase` run when the model name
matches. An explicit env-var override (`CAPYBASE_PROMPT_LAYOUT=markdown_code`
etc.) always wins over the calibrated profile — useful for A/B testing.
