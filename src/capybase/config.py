"""Typed runtime configuration loaded from capybase.toml.

Packaging metadata lives in pyproject.toml; this module owns the *runtime*
config surface ([model], [policy], [tests], [validation], [journal],
[future]). The `[future]` section documents planned seams and is parsed but
intentionally inert in the MVP.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


# The default artifacts filename each calibration artifact uses in the config
# dir. Used to rewrite the repo-relative defaults (``.rebase-agent/memory/...``)
# to config-dir-absolute paths at load time (see ``Config.load``).
_PROFILE_FILENAME = "model_profile.json"
_CALIBRATION_FILENAME = "calibration.json"
# The repo-relative defaults from CalibrationConfig; ``Config.load`` rewrites
# these (and only these) to live in the config dir so the user repo need not
# duplicate calibration artifacts. An explicit value in the toml is always
# respected.
_REPO_DEFAULT_PROFILE_PATH = ".rebase-agent/memory/model_profile.json"
_REPO_DEFAULT_CALIBRATION_PATH = ".rebase-agent/memory/calibration.json"


def default_config_dir() -> Path:
    """The shared capybase config dir, per the XDG Base Directory spec.

    ``$XDG_CONFIG_HOME/capybase`` if ``XDG_CONFIG_HOME`` is set, else
    ``~/.config/capybase``. capybase reads ``capybase.toml`` and the calibration
    artifacts (``model_profile.json``, ``calibration.json``) from here, so the
    user repo need not carry any capybase config. Override with ``--config DIR``.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "capybase"


def default_data_dir() -> Path:
    """The shared capybase data dir, per the XDG Base Directory spec.

    ``$XDG_DATA_HOME/capybase`` if ``XDG_DATA_HOME`` is set, else
    ``~/.local/share/capybase``. Used for cross-session operational logs
    (``logs/capybase.log``) that span runs and repos — distinct from the
    per-session, repo-relative ``.rebase-agent/`` artifact tree (which holds
    the authoritative per-run journal).
    """
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "capybase"


class ModelConfig(BaseModel):
    provider: Literal["openai_compatible"] = "openai_compatible"
    base_url: str = "http://127.0.0.1:8080/v1"
    api_key: str = "sk-local"
    model: str = "vibethink"
    temperature: float = 0.2
    # Samples per fresh resolve. Default 1 (single draw): best-of-N + self-
    # consistency is OPT-IN (raise samples AND set enable_self_consistency=true).
    # The live eval showed samples=3 with VibeThinker-3B trades 5× latency for no
    # convergence benefit on these conflicts — the model's per-draw success rate
    # is too low for best-of-3 to reliably include a correct candidate, so
    # consensus voting just picks the most-common wrong answer. Best-of-N helps
    # when a model has a higher per-draw success rate; default to 1 here.
    samples: int = 1
    # Difficulty-aware sample allocation (survey §4 UAB-lite): when routing is
    # enabled and a unit classifies as "complex", draw this many samples instead
    # of the base ``samples``. Concentrates test-time compute where a 3B model
    # genuinely struggles (multi-hunk files, large enclosing AST nodes) without a
    # cross-unit scheduler. 0 (default) = disabled: complex units use ``samples``
    # as today, so behavior is unchanged. Difficulty is the viable signal here
    # because it is computed BEFORE any LLM call (unlike mean_token_entropy, which
    # is post-generation and can't drive the first sample count).
    samples_complex: int = 0
    # Self-consistency: when samples > 1 and future.enable_self_consistency is
    # on, candidates are clustered by normalized text and the majority wins.
    # Below this agreement fraction the merge is flagged low-confidence (the
    # risk engine can treat it as a retry/escalate signal).
    consensus_min_agreement: float = 0.4
    # Two-pass prompting (Step 2): first request extracts semantic intents
    # only (small, fast), second request generates code conditioned on those
    # intents. Only activates when samples > 1 (the multi-candidate path) to
    # avoid doubling requests for the single-sample case.
    two_pass: bool = False
    # PlanSearch (survey §1): in the two-pass path, sample MULTIPLE distinct
    # NL resolution plans (Pass 1) and generate one code candidate per plan
    # (Pass 2), instead of one plan → N code samples. Adds diversity on the
    # planning axis — orthogonal to temperature and prompt-variant sampling.
    # Only engages when two_pass AND samples > 1. Defaults off; falls back to
    # the single-intent path if the plan-search call fails or yields <2 plans.
    plan_search: bool = False
    # Raised temperature for the diverse multi-sampling pass (distinct from
    # the low `temperature` used for focused retries). Higher temp → more
    # diverse candidates → better consensus signal.
    sampling_temperature: float = 0.7
    # Draw samples concurrently in a thread pool (each is a blocking HTTP call).
    # Safe because the LLM adapter is stateless per-call.
    parallel_samples: bool = True
    # Parameter-diversity portfolio (survey §4.1): when sampling N>1, split the
    # samples across the high sampling_temperature (exploratory) and the low
    # base temperature (conservative). Raises the odds that at least one sample
    # is both valid and distinct on a 3B model. Bypasses the server-side batched
    # n path (which forces one temperature) to use N separate requests. Off by
    # default; for N=1 it is a no-op.
    diverse_sampling: bool = False
    # Prompt-variant sampling (survey §4 Code Roulette): when on AND samples > 1
    # AND this is a fresh resolve (no CEGIS retry/repair), draw the samples across
    # semantically-equivalent resolve-prompt phrasings instead of identical prompts
    # at varied temperatures. A candidate stable across prompt variants is a
    # stronger correctness signal, and the existing consensus + rank-order
    # validation already selects the largest stable cluster. Defaults off so
    # behavior is unchanged; retry/repair paths never use variants (they must stay
    # single-template for reproducible counterexample feedback).
    prompt_variants: bool = False
    # Self-consistency mirror: when on AND samples > 1, candidates are clustered
    # by normalized text and the majority cluster wins (see FutureConfig for the
    # legacy location). Duplicated onto ModelConfig so ``capybase calibrate`` can
    # store it in the model profile (which overlays ModelConfig only). The
    # orchestrator reads this in preference to future.enable_self_consistency.
    # OPT-IN (paired with samples>1): the live eval showed best-of-3 with this
    # model trades 5× latency for no convergence gain, so default off. Raise
    # samples AND set true to engage the consensus/voting path.
    enable_self_consistency: bool = False
    # ``response_format: {type: json_object}`` is sent on every completion so
    # the model emits a single parseable JSON object. A few local servers reject
    # this key (older llama.cpp builds, some vLLM configs). ``capybase calibrate``
    # detects support and flips this to False, after which the adapter omits the
    # key and resolution falls back to the fenced-JSON parser. Default True keeps
    # current behavior; off only when a profile says the server can't handle it.
    json_mode: bool = True
    # Reasoning models emit long <think> chains before answering; 2048 starves
    # them. 8192 leaves headroom for reasoning + the final JSON answer.
    max_tokens: int = 8192
    request_timeout_seconds: int = 600
    # Hard wall-clock deadline for ONE generation attempt (across all streamed
    # tokens). Distinct from request_timeout_seconds (per-read socket timeout):
    # a generation that trickles data forever without finishing must still abort
    # and become a retryable failure. Real completions on a 3B reasoning model
    # take ~30-90s; this gives headroom without hanging for minutes on a stall.
    generation_timeout_seconds: int = 180
    # TECP token-entropy capture (survey §4.1): when on, requests per-token
    # logprobs from the API and reduces them to a scalar mean token-entropy
    # (mean negative log-probability) carried on each candidate. This is the
    # logit-free, black-box uncertainty signal the conformal "flywheel" learns
    # from — never the model weights. Defaults off so deployments that don't
    # need it pay no request-shape cost and see no behavior change; the API
    # simply omits ``logprobs`` from the request body when off.
    capture_token_entropy: bool = False
    # Transport-layer retry for transient LLM failures (connection reset, socket
    # timeout, HTTP 5xx, or the stalled-connection hard-deadline RuntimeError the
    # adapter raises). This sits BELOW the application-level CEGIS re-prompt loop
    # (policy.max_retries_per_unit, which re-prompts with feedback): a single
    # generation gets up to retry_attempts transport retries, then CEGIS takes
    # over. Does NOT retry HTTP 4xx (caller errors) or the "unexpected response
    # shape" error (malformed — a retry would just fail identically). 1 = no
    # retries. 3 (default) favors first-use resilience over latency on a flaky
    # local endpoint. Exponential backoff with full jitter is applied between
    # attempts (see llm_openai._with_retry), capped by retry_max_delay_seconds.
    retry_attempts: int = 3
    retry_base_delay_seconds: float = 1.0
    retry_max_delay_seconds: float = 30.0
    # Model context window (input budget), in tokens. 0 = DISABLED: capybase
    # sends the prompt unbounded (the historical default, fully backward-
    # compatible). When set, the resolve prompt is capped to this window: the
    # three conflict sides + the JSON contract are ALWAYS sent intact, and the
    # augmentation sections (few-shot examples, cross-file deps, surrounding
    # context) are trimmed — lowest-value first — to fit. ``capybase calibrate``
    # auto-discovers this from the server's /v1/models endpoint (its
    # ``context_length``) and stores it in the model profile; it can also be set
    # manually here. Never trim the conflict sides themselves: a unit whose sides
    # + boilerplate alone exceed the window is sent anyway (the model must see
    # the actual conflict) with a logged warning.
    context_window: int = 0
    # Tokens to reserve for the completion when computing the usable input
    # budget: available_input = context_window - completion_reserve. Kept modest
    # relative to context_window so trimming only triggers when genuinely needed.
    completion_reserve: int = 1024


class PolicyConfig(BaseModel):
    supported_conflict_types: list[str] = Field(
        default_factory=lambda: ["UU", "AU", "UA"]
    )
    supported_file_kinds: list[str] = Field(default_factory=lambda: ["text"])
    max_retries_per_unit: int = 2
    # Separate retry budget for verifier-critic disagreements. A critic-driven
    # retry (the model produced a structurally-valid merge the LLM judge flagged
    # for dropped intent) consumes THIS budget, NOT max_retries_per_unit — so a
    # stubborn dropped-intent case can't starve the syntactic-CEGIS retries.
    # 0 = mirror max_retries_per_unit (the same-size default — merge correctness
    # is essential, latency is not, so the critic gets as many chances as the
    # resolver). A nonzero value overrides.
    max_critic_retries_per_unit: int = 0
    # Recovery retry budget for model self-refusals (needs_human). When the model
    # self-reports needs_human, a single recovery retry with a reframed prompt
    # (build_recovery_prompt) is granted before escalating — a struggling model
    # often succeeds with better scaffolding. 1 = one recovery retry (default);
    # 0 = disable (escalate immediately on needs_human, the legacy behavior).
    # Recovery retries use a SEPARATE counter so they can't starve syntactic or
    # critic retries.
    max_recovery_retries_per_unit: int = 1
    # Whole-file repair retry budget (Fix #3). The Phase 2 loop re-resolves the
    # attributed unit and re-validates the spliced file when a cross-unit error
    # (brace imbalance, duplicate symbol) surfaces. This is a SEPARATE budget
    # from max_retries_per_unit (which governs per-unit CEGIS) because a
    # whole-file cycle is more expensive (~cargo run) but also more likely to
    # converge with the deterministic brace-repair fallback (Fix #2) + enriched
    # cross-hunk context (Fix #1). 0 = mirror max_retries_per_unit (the default,
    # preserving the legacy behavior). A higher value grants more repair cycles
    # for multi-hunk conflicts where the model needs several shots.
    max_whole_file_repair_retries: int = 0
    # Confidence-gated escalation: when the critic budget is exhausted, a
    # high-confidence critic flag (verifier_confidence >= this threshold)
    # escalates instead of accepting-with-warning. Uses the critic's own
    # confidence — 0.0 = never confidence-escalate (always accept-with-warning
    # when the budget is gone, the conservative default); 0.8 = escalate only
    # when the judge is quite sure the side was dropped.
    critic_confidence_escalate_threshold: float = 0.8
    # Hard wall-clock budget for resolving ONE unit, across ALL retries
    # (syntactic CEGIS, critic-driven, and whole-file repair). A unit that
    # can't converge within this many seconds is escalated rather than looping
    # indefinitely — bounds latency regardless of how the retry budgets split.
    # 0 = disabled (retry-count budgets alone govern; the legacy behavior).
    # Sits ABOVE the per-retry budgets: it's the outermost deadline.
    max_wall_time_per_unit_seconds: float = 0.0
    allow_skip: bool = False
    allow_delete_conflicted_file: bool = False
    stage_only_validated_paths: bool = True
    context_lines: int = 15
    # Acceptance strictness (#10): how boldly capybase auto-accepts a merge.
    #   "interactive" (default) — bold: a passing candidate is accepted; the
    #     human is at the terminal to catch a bad one via the fallback.
    #   "dry_run"        — bold (the run is a rehearsal; no real cost to accept).
    #   "ci"             — cautious: escalate anything not deterministic-or-
    #     high-confidence (a CI run has no human in the loop mid-step).
    #   "unattended"     — most cautious: accept ONLY a deterministic merge or a
    #     high-confidence candidate with no dropped obligations, no new
    #     diagnostics, tests passing, and no low-confidence/needs-human signal.
    policy_mode: Literal["interactive", "dry_run", "ci", "unattended"] = "interactive"
    # Below the unattended path: require the candidate's self-reported
    # confidence ≥ this to accept (else escalate). 0.0 disables the floor.
    unattended_min_confidence: float = 0.6
    # In unattended mode, escalate any conflict whose classification band is in
    # this set (default: hard conflicts need a human). Empty disables the gate.
    unattended_escalate_bands: list[str] = Field(
        default_factory=lambda: ["hard"]
    )


class TestsConfig(BaseModel):
    pre_continue: str | None = "pytest"
    final: str | None = "pytest"
    timeout_seconds: int = 300
    required: bool = True
    # Test-continuity invariant (survey §2.1a): capture which tests PASS on the
    # pre-rebase tree, then treat a baseline-passing test that FAILS post-merge
    # as a behavioral regression the merge introduced — a high-signal
    # counterexample the syntactic/intent validators can't catch (a merge can
    # preserve structure + intent-units yet still break behavior). Runs the
    # configured pre_continue/final command at rebase() start (best-effort: a
    # failed/missing baseline leaves the invariant inert). pytest is run with
    # -v so per-test node-IDs are parseable. OPT-OUT (default ON).
    enable_test_continuity: bool = True


class PolicyRule(BaseModel):
    """One deterministic safety rule for the VeriGuard-style policy gate.

    The gate statically extracts import/call facts from a candidate patch's
    resolved text (stdlib ``ast``, Python only) and evaluates each rule. A rule
    ``forbid_import`` matches when ``pattern`` is a prefix of any imported
    module path (so ``"subprocess"`` catches ``subprocess.run`` usage too, since
    importing ``subprocess`` is the precondition); ``forbid_call`` matches when
    ``pattern`` is a prefix of any call target (so ``"eval"`` catches the
    builtin and ``"os.system"`` catches the dotted call). All deterministic at
    runtime — no LLM, no execution (survey §4 VeriGuard).
    """

    name: str
    kind: Literal["forbid_import", "forbid_call"]
    pattern: str
    severity: Literal["error", "warning"] = "error"
    reason: str = ""


class ValidationConfig(BaseModel):
    require_no_markers: bool = True
    require_exact_splice_scope: bool = True
    require_syntax_if_supported: bool = True
    reject_if_copies_one_side: bool = True
    # Both-sides-represented (survey §5.1 cheap necessary condition): flag a
    # candidate that drops a side's additions entirely — a tweaked-but-still-
    # one-sided merge the copy heuristic misses. Advisory warning.
    reject_if_drops_a_side: bool = True
    # Side-obligation contract (#3): flag a candidate that reverts a side's
    # MODIFICATION of an existing line back to base (a silent undo the token-set
    # both-sides-represented check misses — a same-line edit often adds no
    # distinctive token), or drops a side's added line entirely. Derived from a
    # line-level diff of each side vs base. Advisory warning (feeds retry).
    reject_if_drops_obligation: bool = True
    # Dependency preservation (survey §2.2 SafeMerge necessary condition): warn
    # when a merge drops a base-referenced symbol that has an in-repo definition
    # and neither side removed. Companion to both-sides-represented — that
    # guards a side's additions; this guards a shared base dependency (e.g. a
    # validate() call the model silently removed). Advisory warning. Only active
    # when [structural] cross_file_slice is on (the validator needs the slicer);
    # inert otherwise.
    reject_if_drops_referenced_symbol: bool = True
    reject_if_model_needs_human: bool = True
    # Phase B: validate the fully-spliced file (with *all* units resolved)
    # after per-unit validation passes. This catches cross-unit errors that
    # per-unit checks structurally cannot — leaked markers from sibling
    # blocks, syntax errors that only arise when two resolutions are
    # juxtaposed, duplicate symbols across hunks. Meaningful even for
    # single-unit files; disable only for non-code where it's moot.
    require_whole_file_validation: bool = True
    # AST preservation (requires the structural parser): prove that nodes OUTSIDE the
    # conflict span are structurally unchanged after splicing. Catches a model
    # silently rewriting or deleting unchanged code that the line-level
    # ExactSpliceScope check misses (it only guards line boundaries). When the
    # grammar is absent this validator is inert.
    require_ast_preservation: bool = True
    # Intent-coverage floor (requires the structural parser): the minimum fraction of a
    # side's ADDED structural units (functions/classes/fields beyond base) that
    # must survive in the resolution. A deterministic, hard coverage guarantee —
    # "never silently drop > (1-ratio) of a side's added units without a retry".
    # Warning severity (feeds the critic retry path); a deterministic backstop
    # that fires even when the LLM critic is uncertain or skipped. 0.0 = disabled
    # (no coverage floor). Only fires when a side added ≥1 structural entity, so
    # value-only conflicts are unaffected (the token-set validator backstops those).
    min_preservation_ratio: float = 0.5
    # LSP / type-checker diagnostics (requires pyright/rust-analyzer): reject a
    # candidate that introduces NEW type or compilation errors not present in
    # the pre-conflict baseline. Runs in Phase B on the fully-spliced file.
    # Inert when the tool is absent.
    enable_lsp_diagnostics: bool = False
    pyright_path: str = "pyright"
    rust_analyzer_path: str = "rust-analyzer"
    cargo_path: str = "cargo"
    lsp_baseline_strict: bool = True
    # Rust compile floor (survey: parity with Python's py_compile). Rust files
    # are compiled with ``rustc --emit=metadata`` in Phase B — the exact analog
    # of ``py_compile``: a dependency-free syntax/parse check that rejects a
    # non-compiling merge (dropped ``;``, unbalanced braces, duplicate field)
    # the same way Python rejects a syntax error. Runs whenever
    # ``require_syntax_if_supported`` is on (the default) and ``rustc`` is on
    # PATH; degrades to "not checked" (never crashes) when the tool is absent.
    # ``rust_edition`` overrides the edition ("2015"/"2018"/"2021"); empty
    # (default) means infer from the nearest ``Cargo.toml``'s ``edition``
    # field, falling back to "2021".
    rustc_path: str = "rustc"
    rust_edition: str = ""
    # Clippy lint check (cargo clippy) for Rust: a quality check that runs in
    # Phase B on the fully-spliced file and flags clippy findings the merge
    # INTRODUCES (compared to a pre-conflict baseline, so a repo's pre-existing
    # lint debt is ignored). Distinct from the compile floor: clippy findings
    # are quality issues, not compile errors, so the default severity is
    # "warning" (bias toward review, don't hard-reject a compiling merge); set
    # "error" to block lint-introducing merges. Reuses the cargo JSON format,
    # so it needs a cargo project (inert for loose .rs / non-Rust / missing
    # cargo). Opt-in like the LSP diagnostics.
    enable_clippy: bool = False
    clippy_severity: str = "warning"
    # Shadow tests: if a tests/test_<module>.py exists for the modified file,
    # run it before declaring success (best-effort, Phase B).
    enable_shadow_tests: bool = False
    # Verifier-model critic (surveys §1/§5 Proposer-Critic; the reserved
    # `enable_verifier_model` seam): an LLM judge that checks the resolved text
    # preserves BOTH sides' semantic intent — the one failure mode the syntactic
    # validators (markers, splice scope, AST, LSP) are structurally blind to:
    # a merge that parses cleanly but silently drops a side's intent. Uses the
    # same black-box API client already in the orchestrator; no model is trained
    # or hosted. OPT-OUT (default ON): the critic is the only check for
    # silently-dropped intent, so it runs by default in every real resolution
    # — set false to disable (e.g. to cut latency/cost on a trusted corpus).
    enable_verifier_model: bool = True
    # Severity of a critic disagreement: "warning" (default — bias toward
    # retry/escalate but don't hard-reject a syntactically-valid merge) or
    # "error" (strict — treat a dropped-intent verdict as a hard failure).
    verifier_severity: str = "warning"
    # Critic guardrail — Phase 1: inject the deterministic preservation math into
    # the critic's initial prompt as a SYSTEM ASSERTION, so it doesn't hallucinate
    # drops the AST disproves. Default-on (strictly improves the prompt).
    enable_verifier_assertion: bool = True
    # Critic guardrail — Phase 2: when the critic still flags a drop, a second
    # "show-your-work" call demanding it quote the exact missing/mangled snippet.
    # The evidence is verified programmatically (substring match); null or
    # fabricated evidence squashes the flag. Default-on; only fires when the
    # critic flags AND min coverage >= the floor below.
    enable_verifier_reflection: bool = True
    # Critic guardrail — Phase 3: hard suppress a critic drop-flag when the
    # deterministic coverage is UNANIMOUSLY perfect (both ratios 1.0, no dropped
    # additions). The mathematically-authoritative backstop. Default-on.
    enable_verifier_guardrail: bool = True
    # Below this min coverage, Phase 2 reflection is skipped — the critic is
    # likely right (a real drop), so don't waste the reassessment call.
    verifier_reflection_coverage_floor: float = 0.9
    # Recovery retry for model self-refusals (needs_human): when the model gives
    # up, grant one retry with a reframed prompt (build_recovery_prompt) before
    # escalating. A struggling model often succeeds with better scaffolding. The
    # budget is max_recovery_retries_per_unit in [policy]. Default-on.
    enable_recovery_retry: bool = True
    # Per-unit syntax checks (CEGIS loop hardening): PythonSyntaxValidator +
    # RustSyntaxValidator run on each candidate so a code syntax error becomes a
    # hard failure that seeds PROMPT_REPAIR (targeted fix showing the broken
    # candidate + the compile diagnostic). Distinct from
    # require_syntax_if_supported (which gates the Phase B whole-file check);
    # this is the PER-UNIT early-feedback check. Default-on; the hermetic suite
    # opts out (fake clients produce partial snippets that don't compile standalone).
    enable_per_unit_syntax_check: bool = True
    # VeriGuard-style deterministic policy gate (survey §4): statically extract
    # import/call facts from each candidate's resolved text and evaluate them
    # against ``policy_rules``. The ONLY check that inspects WHAT a patch
    # introduces (every other validator is syntactic/structural) — catches a
    # clean-but-unsafe merge (e.g. adds subprocess to api/). Fully deterministic
    # at runtime (stdlib ast, no LLM, no execution), Python-only, graceful no-op
    # for other languages. When off OR no rules configured, the gate is inert.
    enable_policy_gate: bool = False
    policy_rules: list[PolicyRule] = Field(default_factory=list)
    # LLM code-smell checks (survey §7): statically detect smells common in
    # LLM-generated code via stdlib ast — NaN comparison (x == np.nan, always
    # False), pandas chain indexing (df[a][b], ambiguous), uncontrolled
    # randomness (random.* with no seed). A cheap pre-test quality filter,
    # deterministic (no LLM, no execution), Python-only, graceful no-op
    # otherwise. Only the AST-clean smells are implemented; dataflow smells
    # (missing scaling, data leakage, implicit hyperparameters) need richer
    # analysis and are deferred. When off (default) the checker is inert.
    enable_code_smell_checks: bool = False
    code_smell_severity: str = "warning"
    # Silent-resurrection detection (survey "silent loss of intent"): git's
    # 3-way merge can resolve CLEANLY (no conflict markers) while resurrecting
    # dead code the ``onto`` branch deliberately deleted — because the replayed
    # branch predates the cleanup. Git sees no conflict; without this scan,
    # capybase sees none either and the cleanup is silently undone. After a clean
    # rebase (and per replayed step), capybase compares the result against the
    # content ``onto`` removed since the merge-base and reports any that came
    # back. Advisory detection — never breaks a rebase that would otherwise
    # succeed, even when ``resurrection_policy`` is "stop" (it halts BEFORE the
    # bad completion is left as final, keeping the backup ref recoverable).
    enable_resurrection_detection: bool = True
    # What to do when a resurrection is detected: "stop" (default — halt before
    # completing, write a review bundle with the suspected resurrections, and
    # route to the interactive fallback when a TTY is present; the existing
    # abort-on-escalation keeps the repo recoverable via the backup branch) or
    # "warn" (continue to completion, but surface the findings in the summary +
    # journal for post-hoc review — useful in CI where a hard stop is undesired).
    resurrection_policy: Literal["warn", "stop"] = "stop"
    # Minimum non-blank lines in a deleted block for it to count as a
    # resurrection. Tiny reappearances (a lone blank line, a one-line import) are
    # usually coincidental, not a revival of deliberately-removed code.
    resurrection_min_block_lines: int = 3
    # Minimum line-coverage for a deleted block to count as "back" in the result
    # (1.0 = whole block returned; 0.85 default tolerates minor edits). Higher =
    # fewer false positives but may miss a partially-resurrected block.
    resurrection_min_similarity: float = 0.85
    # Cross-commit dependency guardian (survey §3.1): a deterministic post-rebase
    # audit that catches cross-window dependency breaks the per-commit validators
    # miss (e.g. commit A renames ``foo``→``bar``, a later commit B still calls
    # ``foo`` — locally valid per commit, broken across the window). When enabled
    # (default), runs after the resurrection scan on clean completion and surfaces
    # ``cross_commit_dependency_break`` findings; in "stop" mode (cross_commit_policy)
    # it escalates like the resurrection scan, in "warn" it continues. The guardian
    # is purely deterministic (tree-sitter defines/uses, no LLM) and degrades to a
    # no-op for unsupported languages / when the structural parser is unavailable.
    enable_cross_commit_guardian: bool = True
    cross_commit_policy: Literal["warn", "stop"] = "warn"
    # Structural parser backend (Round 1 of the abstract-parser migration).
    # Deprecated: the grammar-free abstract parser
    # (capybase.adapters.abstract_parser) is the sole structural backend. This
    # field is retained for config-file backward compatibility (it parsed from
    # capybase.toml) but has no effect — the tree-sitter backend was removed.
    parser_backend: Literal["abstract", "tree_sitter"] = "abstract"
    # Intent evolution trace (survey §3.2): a deterministic post-rebase audit
    # that, for an entity touched across ≥2 commits, checks the final merge
    # matches the entity's LAST source-branch evolution (its most recent body).
    # A divergence flags an ``intent_evolution_gap`` — the merge likely reverted
    # to or kept an earlier version, silently losing an intermediate step no
    # per-commit validator sees. Purely advisory (observability/assurance, never
    # blocks): the survey notes the retry would be too expensive for multi-commit
    # chains, so this produces a report rather than a gate. Degrades to a no-op
    # when the structural parser is unavailable. ``evolution_policy`` is reserved for a
    # future "stop" mode; currently always advisory.
    enable_evolution_audit: bool = True
    # Session-level coverage SLO (survey §3.3): aggregate the per-unit intent
    # preservation coverage across the whole rebase window into one ratio
    # (preserved units / total units) and surface it in the completion report —
    # an observability metric for detecting regressions across orchestrator
    # changes (e.g. "session coverage dropped from 97% to 91%"). Purely advisory
    # (never blocks the rebase). ``session_coverage_slo`` is the floor; when > 0
    # and the session ratio falls below it, an advisory is emitted (still not a
    # hard gate — observability, not enforcement, per the survey). 0 disables.
    session_coverage_slo: float = 0.0


class JournalConfig(BaseModel):
    enabled: bool = True
    store_prompts: bool = True
    store_raw_responses: bool = True
    store_snapshots: bool = True
    store_candidates: bool = True
    store_validations: bool = True
    # Semantic accept report (#4): append a "why we accepted this merge" summary
    # (preserved obligations, validation, test verdict) per step to
    # final/accept-report.md. Advisory; never blocks the rebase.
    write_accept_reports: bool = True


class FutureConfig(BaseModel):
    """Resolution-mechanism toggles (the pre-LLM + RAG layers).

    A mix of operational toggles and documented planned seams. NOTE: the
    history-aware features (future probes, obligations, branch intent, exact
    reuse, provenance restamping) are NOT config knobs here — they are always-on
    and ADAPTIVE: they derive their behavior from the conflict's own data (e.g.
    the probe mode is chosen by whether intervening commits exist, not a setting).
    Tuning those behaviors is a code change (documented constants in the relevant
    module), not a per-deploy config, by design — minimal config, no hidden knobs.
    """

    # OPT-IN (mirrors [model]); see ModelConfig.enable_self_consistency. Default
    # off — best-of-N is opt-in for models whose per-draw success rate justifies it.
    enable_self_consistency: bool = False
    enable_rag: bool = False
    enable_structural_context: bool = False
    # Now wired AND default-on in [validation] (opt-out); mirrored here for the
    # [future] seam documentation. See ValidationConfig.enable_verifier_model.
    enable_verifier_model: bool = True
    enable_mutation_testing: bool = False
    # Deterministic structural pre-resolution (survey §6.4 layer 1): BEFORE the
    # LLM, attempt a model-free resolution from base+sides via provably-safe
    # rules (identical sides, one-sided change, disjoint line edits). Every
    # resolution still runs the full validation pipeline; a guess that fails
    # validation falls through to the model — so this only cuts LLM load on
    # trivial conflicts, never produces a worse merge. Default ON (safe-by-
    # construction); flip off to force the model to handle every conflict.
    enable_structural_resolver: bool = True
    # Search-based combination resolution (survey §4.1 SBCR): AFTER the
    # structural resolver declines and BEFORE the LLM, search order-preserving
    # interleavings of the two sides for the best combination (mean similarity
    # to both parents). Covers the ~98.6% of combination resolutions that use no
    # newly-invented lines. Pure/heuristic — so, like the structural resolver,
    # every candidate is STILL validated (syntax/AST/splice) before acceptance;
    # an invalid combination (e.g. contradictory lines concatenated) is rejected
    # and falls through to the LLM. Default ON; only fires when the structural
    # resolver declined, so the cheap provably-safe rules always run first.
    enable_combination_search: bool = True
    # Block-capture resolution (large modify/delete): when one side deleted a
    # large block and the keeper side kept/modified it, the model can't reliably
    # reproduce the block as an escaped JSON string (placeholder collapse +
    # escaping corruption). Instead it makes a keep/accept_deletion/needs_human
    # DECISION and capybase splices the chosen conflict side verbatim — the model
    # never reproduces the text, so truncation and escaping errors are
    # structurally impossible. Default ON; only engages on modify/delete conflicts
    # whose kept block exceeds block_capture_min_lines, so small conflicts still
    # use the full-LLM path.
    enable_block_capture: bool = True
    # Minimum non-blank lines in the kept block for block-capture to engage.
    # Below this the full-LLM path reproduces the block fine; above it
    # reproduction becomes unreliable and the decision-style prompt takes over.
    block_capture_min_lines: int = 50


class StructuralConfig(BaseModel):
    """Tree-sitter AST parsing for structural context + preservation checks.

    When enabled and the ``structural`` optional deps are installed, the
    conflict extractor populates ``ConflictUnit.structural_metadata`` with the
    lowest enclosing AST node (e.g. the specific ``def``/``impl``) so the
    resolver and validators see a logical block rather than an arbitrary line
    window. The abstract parser is imported lazily; when the language is
    unrecognized or parsing fails, capybase silently degrades to the
    line-window behavior.
    """

    enabled: bool = False
    languages: list[str] = Field(default_factory=lambda: ["python", "rust"])
    max_enclosing_node_lines: int = 60
    cross_file_slice: bool = True
    slice_search_globs: list[str] = Field(
        default_factory=lambda: ["**/*.py", "**/*.rs"]
    )
    # Use the enclosing AST node as primary_text instead of the line window.
    # When the node fits within max_enclosing_node_lines, the model sees the
    # full logical block (def/impl) rather than an arbitrary text slice.
    use_enclosing_as_primary: bool = True
    # Strip comment lines, docstrings, and blank runs from the context shown
    # to the model. Reduces noise for a 3B model prone to "lost in the middle."
    # Does NOT alter resolved_text — the model still emits exact indentation.
    canonicalize_context: bool = True
    # Refine conflict boundaries with `git merge-file --diff3` to get the
    # tightest possible marker span (git may auto-resolve adjacent lines).
    refine_with_diff3: bool = True
    # xdiff backend for ``git merge-file`` alignment (survey §1.3). Histogram
    # anchors on rare lines → tighter, more stable conflict regions than Myers
    # on noisy code (conflict-size reduction in ~10% of conflicting merges).
    # One of "histogram" (default), "patience", "minimal", "myers". Unknown
    # values fall back to histogram silently; refinement is advisory only.
    diff_algorithm: Literal["histogram", "patience", "minimal", "myers"] = "histogram"
    # Sesame-style separator projection (survey §1.2): for brace/semicolon
    # languages (Rust/C/Java/JS/...), split each ``{`` ``}`` ``(`` ``)`` ``;`` onto
    # its own line BEFORE re-running diff3, so the line-merger anchors on real
    # statement/block boundaries instead of entangling trailing punctuation.
    # ~41% fewer conflicts / ~88% fewer false positives vs raw diff3 on those
    # languages; a no-op for Python (indentation/colon-based). The projected
    # refinement is recorded only when it produces fewer/smaller conflict blocks
    # than the raw diff3 view. Advisory only.
    project_separators: bool = True


class MemoryConfig(BaseModel):
    """RAG experience replay: retrieve past successful merges as few-shot.

    The journal already stores every prompt/response/candidate/validation
    triple. The memory layer distills accepted resolutions into a labeled
    corpus of ``HistoricalExample`` records, retrieves the most similar past
    merges for a new conflict, and injects them into the prompt as dynamic
    few-shot demonstrations. Disabled by default; activates
    ``future.enable_rag``.
    """

    enabled: bool = False
    store_path: str = ".rebase-agent/memory/experiences.jsonl"
    # "lexical" (dependency-free BM25, the default) or "embedding" (semantic
    # retrieval via the /v1/embeddings endpoint, survey §4.2). The embedding
    # retriever is used only when the endpoint actually supports embeddings
    # (capybase calibrate detects this); otherwise it falls back to BM25.
    retriever: str = "lexical"
    # The embedding model name to send to /v1/embeddings (distinct from the
    # completion model on a server serving both). Leave empty to reuse the
    # completion model name; calibrate records the working model in the profile.
    embeddings_model: str = ""
    # The base_url for the embeddings endpoint, when it differs from the
    # completion model's (e.g. completion on a localhost LM Studio, embeddings on
    # a remote server). Empty (default) reuses the completion model's base_url —
    # the common single-server case. When set, only the embeddings client uses it;
    # the completion model, the verifier critic, and the block-capture decision
    # calls still hit config.model.base_url.
    embeddings_base_url: str = ""
    retriever_k: int = 3
    # Minimum experiences before retrieval is attempted (avoid noisy few-shot
    # from a near-empty corpus).
    min_examples_for_retrieval: int = 3
    # The cosine-similarity floor below which an embedding match is NOT surfaced
    # as few-shot (embeddings retriever only). Default is the conservative guess;
    # ``capybase calibrate-embeddings`` derives a model-specific value and stores
    # it in the profile, which overrides this at runtime ("profile wins").
    embedding_min_similarity: float = 0.35
    # The full embeddings-calibration envelope (EmbeddingCalibration.to_dict()),
    # carried from the profile so the retriever can apply the isotonic score
    # transform and use the calibrated red_threshold floor (survey §2.1). Empty
    # until ``calibrate-embeddings`` runs; the profile overrides this at runtime.
    embedding_calibration: dict[str, Any] = Field(default_factory=dict)
    # Hybrid-retrieval fusion method, read only when ``retriever == "hybrid"``
    # (survey §4). "rrf" (default, rank-only, scale-agnostic) or "dbsf"
    # (min-max normalized score sum). The profile may override this.
    fusion_method: str = "rrf"
    # Persisted vector cache for the embedding retriever (embeddings survey §1).
    # Without this, EmbeddingRetriever._build re-embeds every accepted experience
    # on every process start — a re-embed cliff as the corpus grows past hundreds.
    # "auto" (default) selects sqlite-vec when importable, else numpy, else
    # in-memory (re-embeds each run, the prior behavior). "sqlite_vec"/"numpy"
    # force a backend (ValueError if unavailable); "off" disables persistence.
    vector_cache: str = "auto"
    # Path stem for the persisted vector cache. The active backend appends its
    # extension (".vec.sqlite" / ".npy" + ".npy.manifest.jsonl"). Relative paths
    # resolve against the repo root, like store_path.
    vector_cache_path: str = ".rebase-agent/memory/vectors"
    # RAG into the repair/retry path (embeddings survey §2). The repair prompt
    # previously carried NO few-shot — the A/B failure site where the model
    # reproduces the same dropped-side merge across retries. Quality filter:
    # only surface examples that converged within this many retries (survey §1's
    # index-quality rule — merges that took many retries may have converged by
    # luck, not a generalizable strategy). -1 disables the filter.
    repair_retrieval_max_retries: int = 2
    # Higher floor than fresh-generation for the repair path (the cost of a
    # misleading example is higher when the model is already fixing a specific
    # error). Applied in addition to the per-retriever min_similarity.
    repair_retrieval_min_similarity: float = 0.55
    # Session-level semantic drift detection (embeddings survey §6). Advisory
    # only — never blocks a merge. Computes a session anchor once from the
    # branch intent + commit messages, then per-commit cosine distance from it.
    enable_drift_detection: bool = False
    # Cosine DISTANCE threshold (1 - similarity) above which a drift advisory
    # fires. 0.20 ≈ similarity 0.80 — tune on accumulated rebase history.
    drift_threshold: float = 0.20


class CalibrationConfig(BaseModel):
    """Calibrated risk routing: replace the rules threshold with a learned one.

    Once the experience store accumulates enough labeled outcomes, a lightweight
    classifier (logistic regression / isotonic) is fitted offline over
    ``VerificationResult.features`` and predicts the probability a merge will
    fail. The calibrated engine produces the same ``RiskDecision`` shape but
    overrides the accept/escalate boundary using the fitted threshold. Disabled
    by default until enough data is collected.
    """

    enabled: bool = False
    model_path: str = ".rebase-agent/memory/calibration.json"
    # Path to the model capability profile written by ``capybase calibrate``.
    # When present and its model name matches the active model, the profile's
    # tuned knobs (max_tokens, json_mode, capture_token_entropy,
    # generation_timeout_seconds) override the [model] settings at runtime —
    # "Profile wins". Inert when absent/mismatched/corrupt (never crashes).
    model_profile_path: str = ".rebase-agent/memory/model_profile.json"
    escalate_threshold: float = 0.7
    min_examples_for_calibration: int = 50
    # Consensus entropy above this → escalate (high-entropy splits mean no
    # candidate is trustworthy). 0=unanimous, 1=maximally split. Set high
    # (0.8) because even a 2-of-3 majority produces non-trivial entropy; we
    # only want to escalate when samples are *maximally* split.
    entropy_escalate_threshold: float = 0.8
    # Conformal escalation strictness (1-alpha). When a conformal model is
    # fitted, candidates with a p-value below this are escalated. This is an
    # empirical guardrail tuned on capybase's own accepted/escalated outcomes
    # (a correctness proxy), NOT a proven coverage guarantee — see
    # ConformalRiskModel's caveat. Lower = escalate more.
    conformal_alpha: float = 0.1


class RoutingConfig(BaseModel):
    """Difficulty-aware routing (survey §6.1, ICoT/RoutingGen pattern).

    Classifies a conflict as ``simple`` or ``complex`` *before* any LLM call
    using structural signals already on the ConflictUnit. Simple conflicts (a
    single isolated hunk) take a fast path (one low-temp sample, no two-pass,
    no consensus); complex ones (multi-hunk, large nodes) get the full
    test-time pipeline. Concentrates compute where a 3B model struggles and
    cuts ~half the tokens on easy cases. Disabled by default (opt-in).
    """

    enabled: bool = False
    # A file with more than one conflict hunk → complex.
    complex_if_sibling_count_gt: int = 0
    # Enclosing AST node larger than this (lines) → complex.
    max_simple_node_lines: int = 40
    # Combined base+current+replayed side text longer than this (chars) → complex.
    max_simple_side_chars: int = 1200
    # Minimum conflict balance for SBCR to ACCEPT outright (survey §4.2). Balance
    # = min/max of the two sides' non-blank line counts (1.0 = equal, →0 =
    # heavily imbalanced). SBCR wins on balanced conflicts and loses to the LLM
    # on imbalanced ones, so below this threshold an SBCR result is NOT
    # short-circuited — the LLM runs instead. 0.0 = always accept SBCR when it
    # resolves (the conservative default: don't change behavior unless tuned).
    min_balance_for_sbcr_accept: float = 0.0


class Config(BaseModel):
    model: ModelConfig = Field(default_factory=ModelConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    tests: TestsConfig = Field(default_factory=TestsConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    journal: JournalConfig = Field(default_factory=JournalConfig)
    structural: StructuralConfig = Field(default_factory=StructuralConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    calibration: CalibrationConfig = Field(default_factory=CalibrationConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    future: FutureConfig = Field(default_factory=FutureConfig)
    source_path: str | None = None

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        *,
        config_dir: str | Path | None = None,
    ) -> "Config":
        """Load config from a toml file, the config dir, or built-in defaults.

        Resolution (highest precedence first):
        1. ``path`` — an explicit ``capybase.toml`` *file* (direct/test use).
        2. Repo-local ``./capybase.toml`` (or ``capybase.local.toml``) in cwd —
           per-repo overrides.
        3. ``<config_dir>/capybase.toml`` — the user-global config dir (default
           ``~/.config/capybase``; override with the CLI ``--config DIR``).
        4. Built-in defaults.

        After loading, the calibration artifacts' paths
        (``model_profile.json``, ``calibration.json``) are rewritten to live in
        ``config_dir`` — these are machine/user-specific, shared across repos,
        so the user repo need not duplicate them. An explicit absolute path set
        in the toml is always respected (a deliberate override). The RAG
        experience store stays repo-relative (repo-specific merge patterns).
        """
        cdir = Path(config_dir).expanduser() if config_dir else default_config_dir()
        resolved = _resolve_config_path(path, cdir)
        if resolved is None:
            cfg = cls()
        else:
            # If the resolved source is a REPO-LOCAL override, merge it on top of
            # the config-dir toml (when one exists) rather than replacing it
            # wholesale. A partial override (e.g. just [tests]) must not drop the
            # global [model]/[validation]/... sections — otherwise a repo that
            # only wants to tweak its test gate silently loses its model config.
            # An explicit ``path`` file or the config-dir file itself is loaded
            # standalone (no merge): the merge is only for repo-local overrides.
            repo_local = _repo_local_config_path(path)
            dir_toml = cdir / "capybase.toml"
            if repo_local is not None and dir_toml.is_file() and resolved != dir_toml:
                with open(dir_toml, "rb") as fh:
                    base_data = tomllib.load(fh)
                with open(resolved, "rb") as fh:
                    override_data = tomllib.load(fh)
                cfg = cls.model_validate(_deep_merge_toml(base_data, override_data))
            else:
                with open(resolved, "rb") as fh:
                    data = tomllib.load(fh)
                cfg = cls.model_validate(data)
            cfg.source_path = str(resolved)
        # Rewrite the calibration artifacts to the config dir. A user repo has
        # no business carrying calibration data — it's the model endpoint's
        # capability profile, shared across every repo on this machine. Only the
        # repo-relative defaults are rewritten; an explicit path in the toml is a
        # deliberate override and is left alone.
        _relocate_calibration_paths(cfg, cdir)
        return cfg


def _resolve_config_path(
    path: str | Path | None, config_dir: Path | None = None
) -> Path | None:
    """Find the toml to load: explicit file → repo-local → config dir → None."""
    if path is not None:
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"config file not found: {p}")
        return p
    # Repo-local overrides (backward compat: a repo with its own toml wins).
    # Resolve to absolute so ``source_path`` is stable regardless of cwd.
    for name in ("capybase.toml", "capybase.local.toml"):
        candidate = Path(name)
        if candidate.is_file():
            return candidate.resolve()
    # User-global config dir (the default source for a repo with no toml).
    if config_dir is not None:
        candidate = config_dir / "capybase.toml"
        if candidate.is_file():
            return candidate
    return None


def _repo_local_config_path(path: str | Path | None) -> Path | None:
    """The repo-local override toml (``./capybase.toml`` or ``capybase.local.toml``),
    or None. An explicit ``path`` is NOT a repo-local override (it's a direct
    file for test use)."""
    if path is not None:
        return None
    for name in ("capybase.toml", "capybase.local.toml"):
        candidate = Path(name)
        if candidate.is_file():
            return candidate.resolve()
    return None


def _deep_merge_toml(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` onto ``base`` (both parsed-toml dicts).

    ``override`` wins at the leaf; nested tables are merged section-by-section so
    a partial override (e.g. just ``[tests]``) inherits the rest of ``base``.
    Lists are replaced wholesale (no list-merging heuristic — last writer wins,
    matching how toml config is normally understood). Returns a new dict; inputs
    are not mutated.
    """
    out = dict(base)
    for key, val in override.items():
        if (
            key in out
            and isinstance(out[key], dict)
            and isinstance(val, dict)
        ):
            out[key] = _deep_merge_toml(out[key], val)
        else:
            out[key] = val
    return out


def _relocate_calibration_paths(cfg: "Config", config_dir: Path) -> None:
    """Rewrite the calibration artifact paths to the config dir.

    ``model_profile.json`` and ``calibration.json`` are machine/user-specific
    (the model endpoint's capability profile + fitted risk model), shared across
    repos — so they live in the config dir, not each user repo. Only the
    repo-relative *defaults* (``.rebase-agent/memory/...``) are rewritten; any
    other value (an absolute path, or a different relative path) is a deliberate
    override and is left untouched.
    """
    if cfg.calibration.model_profile_path == _REPO_DEFAULT_PROFILE_PATH:
        cfg.calibration.model_profile_path = str(config_dir / _PROFILE_FILENAME)
    if cfg.calibration.model_path == _REPO_DEFAULT_CALIBRATION_PATH:
        cfg.calibration.model_path = str(config_dir / _CALIBRATION_FILENAME)
