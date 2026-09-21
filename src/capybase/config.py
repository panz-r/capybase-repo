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

from pydantic import BaseModel, Field, PrivateAttr


# The calibration-artifact default filename in the config dir, and the
# legacy repo-relative default that Config.load rewrites to it (the ONLY
# path relocation left: the ambient model profile is gone — provider
# configs are the canonical profile source).
_CALIBRATION_FILENAME = "calibration.json"
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
    base_url: str = "http://127.0.0.1:8080/v1"
    api_key: str = "sk-local"
    model: str = "vibethink"
    temperature: float = 0.2
    # UPPER LIMIT on resolution iterations for a unit (S28-129): each
    # iteration draws ONE candidate and validates it; the first pass exits
    # the loop, later iterations are feedback-conditioned (CEGIS repair).
    # Multisampling is never an upfront spend — predicted difficulty only
    # raises the ceiling, observed validation drives the actual cost.
    # Default 1: one draw, then CEGIS. Profiles may raise it for models with
    # high per-draw success rates (diversity beats repair).
    samples: int = 1
    # UPPER LIMIT on resolution iterations for COMPLEX units (routing/
    # classifier difficulty): same semantics as ``samples``, raised for
    # multi-hunk / large-node conflicts where a repaired retry is more
    # likely to need extra attempts. 0 (default) = complex units use
    # ``samples``. Sets only the ceiling — never an upfront spend.
    samples_complex: int = 0
    # Self-consistency: when samples > 1 and enable_self_consistency is on,
    # candidates are clustered by normalized text and the majority wins.
    # Below this agreement fraction the merge is flagged low-confidence (the
    # risk engine can treat it as a retry/escalate signal).
    consensus_min_agreement: float = 0.4
    # Two-pass prompting (Step 2): first request extracts semantic intents
    # only (small, fast), second request generates code conditioned on those
    # intents. Runs at ANY sample count — intent extraction helps the single
    # candidate too. (Formerly coupled to samples > 1; profiles written while
    # that coupling existed and relied on samples = 1 to keep this inert must
    # set two_pass = false explicitly — all existing profiles already do.)
    two_pass: bool = False
    # PlanSearch: in the two-pass path, sample MULTIPLE distinct
    # NL resolution plans (Pass 1) and generate one code candidate per plan
    # (Pass 2), instead of one plan → N code samples. Adds diversity on the
    # planning axis — orthogonal to temperature and prompt-variant sampling.
    # Only engages when two_pass AND more than one candidate is drawn (plan
    # diversity needs several code samples). Defaults off; falls back to the
    # single-intent path if the plan-search call fails or yields <2 plans.
    # NOTE (S28-129): the standard resolution path now draws ONE candidate
    # per validated iteration, so plan_search currently engages only via the
    # consensus batch (self-consistency + samples>1) and calibration probes.
    # A lazy per-plan iteration queue (plans drawn once, code per plan
    # validated across iterations) is a recorded follow-up.
    plan_search: bool = False
    # Raised temperature for the diverse multi-sampling pass (distinct from
    # the low `temperature` used for focused retries). Higher temp → more
    # diverse candidates → better consensus signal.
    sampling_temperature: float = 0.7
    # Draw samples concurrently in a thread pool (each is a blocking HTTP call).
    # Safe because the LLM adapter is stateless per-call.
    parallel_samples: bool = True
    # Parameter-diversity schedule (S28-129 re-scope): the iteration
    # temperature alternates base (conservative, first retry) and the high
    # sampling_temperature (exploratory, later retries) instead of drawing a
    # uniform batch. A truncated retry always overrides the schedule with the
    # engine's truncation-escape bump. Never touches the consensus batch
    # (enable_self_consistency draws its full set in one go, high-temp
    # weighted). Off by default; for a single iteration it is a no-op.
    diverse_sampling: bool = False
    # Prompt-variant sampling (Code Roulette): consensus-batch only — when on
    # AND samples > 1 AND this is a fresh resolve (no CEGIS retry/repair), draw
    # the samples across
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
    # TECP token-entropy capture: when on, requests per-token
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
    # NOTE: conflict modes (UU/AA/AU/UA) are NOT configurable — the set is
    # fixed in capybase.policy.SUPPORTED_CONFLICT_MODES. A config pin here
    # once silently disabled add/add and modify/delete for config-file loads
    # (the s27-68/71 stale-["UU"] regression), which is why the key was
    # removed from the format.
    supported_file_kinds: list[str] = Field(default_factory=lambda: ["text"])
    max_retries_per_unit: int = 2
    # CEGIS convergence threshold: if the model produces a candidate whose
    # NORMALIZED form (comments stripped + whitespace collapsed + lines sorted)
    # has been seen this many times, the loop is cycling on the same essential
    # output — escalate instead of wasting more tokens. Catches the case the
    # exact-hash oscillation backstop misses: a model making slightly-different
    # mistakes each retry (different whitespace, comment reordering) that are
    # cosmetically distinct but semantically identical. Default 2 (fires on the
    # 2nd normalized-duplicate); 0 = disabled (rely on exact-hash backstop only).
    cegis_convergence_threshold: int = 2
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
    # Comment-reconciliation CEGIS retry budget. After the code passes, the
    # comment pass gets this many repair iterations (plan → apply → verify).
    # Default 1 (one propose + one repair if the first plan fails the executable-
    # token invariant or the model produces an unparseable plan).
    comment_reconciliation_retries: int = 1
    # §10 code-reopening budget: when the comment pass detects a high-trust
    # contract conflict (a deferred invariant comment the verifiers can't
    # reconcile), the OUTER code CEGIS is re-entered with the conflict as a
    # seed_failure. This bounds how many times the code↔comment loop can cycle.
    # Default 1 (one reopening attempt; the design says "tightly bounded").
    # 0 disables code-reopening entirely (comment conflicts always escalate to
    # human review). Repeated cycling is itself evidence of an ambiguous merge.
    max_comment_to_code_repair_retries: int = 1
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
    # Outer cap on total resolution+repair time per FILE (across all units and
    # all whole-file repair iterations). When set, threads a monotonic deadline
    # through _whole_file_repair → _resolve_unit so nested repair calls respect
    # the cumulative elapsed time, not just their own fresh per-unit budget.
    # Without this, the whole-file repair loop can create nested _resolve_unit
    # calls each with a fresh max_wall_time_per_unit_seconds budget, causing the
    # real wall clock to explode past any case-level timeout. 0 = disabled.
    max_wall_time_per_file_seconds: float = 0.0
    # Strict time budget for Phase 2 whole-file repair (verify_file + CEGIS
    # repair loop). When set, Phase 2 runs at most: 1 verify_file + deterministic
    # repair beam + at most 1 model re-resolve + 1 final verify_file. The time
    # budget caps the total Phase 2 wall time regardless of how the iterations
    # split. 0 = disabled (use the existing iteration-count-based loop).
    # Design: tiered verification for oversized C files (design v2).
    max_whole_file_repair_seconds: float = 0.0
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
    # Test-continuity invariant: capture which tests PASS on the
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
    runtime — no LLM, no execution (VeriGuard).
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
    # Sprint-19 P2 (churn-aware preservation heuristic): when the ONLY
    # unaccounted obligation of the non-copied side is a pure DELETION of
    # base content (no additions, no exclusive choices), a verbatim copy
    # of the other side passes instead of retrying — a loser churn that
    # only deletes is more likely superseded than one that adds
    # functionality (tokio-0037: the oracle was current verbatim; the
    # heuristic's forced retries degraded into syntax errors). The
    # file-level gates (side-collapse guard, compile checks) still run.
    preservation_deletion_carveout: bool = True
    # Both-sides-represented: flag a
    # candidate that drops a side's additions entirely — a tweaked-but-still-
    # one-sided merge the copy heuristic misses. Advisory warning.
    reject_if_drops_a_side: bool = True
    # Side-obligation contract (#3): flag a candidate that reverts a side's
    # MODIFICATION of an existing line back to base (a silent undo the token-set
    # both-sides-represented check misses — a same-line edit often adds no
    # distinctive token), or drops a side's added line entirely. Derived from a
    # line-level diff of each side vs base. Advisory warning (feeds retry).
    reject_if_drops_obligation: bool = True
    # Dependency preservation (necessary condition): warn
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
    # Rust compile floor parity with Python's py_compile). Rust files
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
    # Repo root for path-aware validators (the per-unit Rust gate's edition
    # inference walks from the unit's repo-relative path to the nearest
    # Cargo.toml). Empty (default) — the orchestrator injects the live repo
    # on the verification-side config; this mirror exists so a standalone
    # ValidationConfig can carry it too.
    repo_root: str = ""
    # Rust error codes to SUPPRESS in the diagnostic delta (treat as not-new
    # even when genuinely introduced). The live realworld eval (Issue 3) showed
    # near-correct Rust merges rejected for E0432/E0433 (crate-path resolution
    # errors undecidable without the full dependency tree). Set to
    # ["E0432", "E0433"] to tolerate these when running outside a full crate.
    # Empty (default) = no suppression (strict). Also suppresses same-code errors
    # that drift in message text between baseline and candidate via code-keyed
    # delta matching (engages automatically when Diagnostics carry .code).
    rust_suppress_codes: list[str] = []
    # C/C++ compile floor (gcc/clang -fsyntax-only). Mirrors the Rust fields
    # above: a per-unit validator (Phase A) catches parse errors in the CEGIS
    # loop; a whole-file gate (Phase B) runs on the spliced TU. ``cc_path`` /
    # ``cxx_path`` select the compiler (gcc/g++ primary; clang/clang++ also
    # work — the resolver honors whichever is on PATH). ``c_std`` / ``cpp_std``
    # set the ``-std=`` flag; defaults (c11 / c++17) are widely supported
    # baselines. Degrades to "not checked" (never crashes) when the compiler is
    # absent — the same graceful-degrade contract as rustc.
    cc_path: str = "gcc"
    cxx_path: str = "g++"
    c_std: str = "c11"
    cpp_std: str = "c++17"
    # When set, the whole-file C/C++ verify_file branch runs this build command
    # in the repo dir (save/write/restore the resolved file) instead of
    # standalone gcc -fsyntax-only. The authoritative oracle for real-world C
    # (resolves sibling #include headers standalone gcc can't). Empty (default)
    # = standalone gcc (the existing behavior). The orchestrator propagates
    # ``tests.pre_continue`` here automatically for C/C++ languages.
    cc_build_command: str = ""
    # Build-target narrowing template for C/C++ per-file verification. When
    # set, verify_file formats this with the conflict file's stem and runs
    # the resulting command instead of the full cc_build_command. This
    # compiles ONLY the conflict file's translation unit (e.g. ``make
    # {stem}.o``), cutting build verification from ~54s (full make) to ~2-5s
    # (single object). Falls back to the full build if the target rule
    # doesn't exist. Empty (default) = use cc_build_command verbatim.
    cc_build_target_template: str = ""
    # Phase-2 build-check fallback when no per-file target template exists:
    # use the pre_continue build command (make / cmake --build / configure &&
    # make) as the whole-tree gate. Without this, the only build check that
    # runs regardless of tests.required never fires for datasets without
    # per-object Makefile rules (protobuf, fmt, json-c, nlohmann), and a
    # build-broken merge ships silently (protobuf-0055: sim 1.000, make rc=2,
    # accepted anyway). The no-classifiable-error guard makes build timeouts
    # / infra failures N/A rather than merge defects.
    cc_phase2_full_build_fallback: bool = True
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
    # Verifier-model critic: an LLM judge that checks the resolved text
    # preserves BOTH sides' semantic intent — the one failure mode the syntactic
    # validators (markers, splice scope, AST, LSP) are structurally blind to:
    # a merge that parses cleanly but silently drops a side's intent. Uses the
    # same black-box API client already in the orchestrator; no model is trained
    # or hosted. Its ACTIVATION lives in [features] llm_critic (FeaturesConfig,
    # default ON) — this section carries only severity. The verification
    # engine's internal mirror keeps an enable_verifier_model flag seeded from
    # the feature at orchestrator init.
    # Severity of a critic disagreement: "warning" (default — bias toward
    # retry/escalate but don't hard-reject a syntactically-valid merge) or
    # "error" (strict — treat a dropped-intent verdict as a hard failure).
    verifier_severity: str = "warning"
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
    # VeriGuard-style deterministic policy gate: statically extract
    # import/call facts from each candidate's resolved text and evaluate them
    # against ``policy_rules``. The ONLY check that inspects WHAT a patch
    # introduces (every other validator is syntactic/structural) — catches a
    # clean-but-unsafe merge (e.g. adds subprocess to api/). Fully deterministic
    # at runtime (stdlib ast, no LLM, no execution), Python-only, graceful no-op
    # for other languages. When off OR no rules configured, the gate is inert.
    enable_policy_gate: bool = False
    policy_rules: list[PolicyRule] = Field(default_factory=list)
    # LLM code-smell checks: statically detect smells common in
    # LLM-generated code via stdlib ast — NaN comparison (x == np.nan, always
    # False), pandas chain indexing (df[a][b], ambiguous), uncontrolled
    # randomness (random.* with no seed). A cheap pre-test quality filter,
    # deterministic (no LLM, no execution), Python-only, graceful no-op
    # otherwise. Only the AST-clean smells are implemented; dataflow smells
    # (missing scaling, data leakage, implicit hyperparameters) need richer
    # analysis and are deferred. When off (default) the checker is inert.
    enable_code_smell_checks: bool = False
    code_smell_severity: str = "warning"
    # Silent-resurrection detection ( "silent loss of intent"): git's
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
    # Bounded history-walk depth for deletion-stability verification. When a
    # candidate resurrection is found, capybase walks the deleting branch's
    # commit history (base..tip) to verify the deletion was stable (removed
    # and never re-added). This distinguishes deliberate cleanup deletions
    # from transient absences, reducing false positives. The depth bounds the
    # number of commits examined (default 50 covers most active branches).
    # Set to 0 to disable the history walk and use the old 3-way-only check.
    resurrection_history_depth: int = 50
    # Cross-commit dependency guardian: a deterministic post-rebase
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
    # Intent evolution trace: a deterministic post-rebase audit
    # that, for an entity touched across ≥2 commits, checks the final merge
    # matches the entity's LAST source-branch evolution (its most recent body).
    # A divergence flags an ``intent_evolution_gap`` — the merge likely reverted
    # to or kept an earlier version, silently losing an intermediate step no
    # per-commit validator sees. Purely advisory (observability/assurance, never
    # blocks): prior findings the retry would be too expensive for multi-commit
    # chains, so this produces a report rather than a gate. Degrades to a no-op
    # when the structural parser is unavailable.
    enable_evolution_audit: bool = True
    # Session-level coverage SLO: aggregate the per-unit intent
    # preservation coverage across the whole rebase window into one ratio
    # (preserved units / total units) and surface it in the completion report —
    # an observability metric for detecting regressions across orchestrator
    # changes (e.g. "session coverage dropped from 97% to 91%"). Purely advisory
    # (never blocks the rebase). ``session_coverage_slo`` is the floor; when > 0
    # and the session ratio falls below it, an advisory is emitted (still not a
    # hard gate — observability, not enforcement, per the). 0 disables.
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
    """Resolution-mechanism toggles (the pre-LLM layers).

    NOTE: the history-aware features (future probes, obligations, branch
    intent, exact reuse, provenance restamping) are NOT config knobs here —
    they are always-on and ADAPTIVE: they derive their behavior from the
    conflict's own data (e.g. the probe mode is chosen by whether intervening
    commits exist, not a setting). Tuning those behaviors is a code change
    (documented constants in the relevant module), not a per-deploy config,
    by design — minimal config, no hidden knobs.

    The SECTION-level feature switches (structural resolution, combination
    search, structural context, RAG, the LLM critic) live in [features]
    (FeaturesConfig), not here. In capybase.toml this model is written as
    TWO sections: [mechanisms] (the stable wired toggles below that are not
    in FUTURE_EXPERIMENTAL_FIELDS) and [experimental] (the dormant/planned
    seams in FUTURE_EXPERIMENTAL_FIELDS) — the loader maps both onto this
    model; see the partition constants under this class.
    """

    #: Calibrated-confidence priors (candidate-ref design P3): a json of
    #: historical per-class pass rates (capybase.calibration_priors
    #: derives it from eval results). When set, acceptance reasons carry
    #: the class prior — informing review, never flipping a tier.
    calibration_priors_path: str = ""
    # Search-based combination resolution (SBCR): AFTER the
    # structural resolver declines and BEFORE the LLM, search order-preserving
    # interleavings of the two sides for the best combination (mean similarity
    # to both parents). ACTIVATION lives in [features] combination_search.
    # Tuning: the sbcr_* knobs below.
    # EXTEND-96: deterministic resolution when exactly one marker-block
    # side is EMPTY (a deletion vs a modification/insertion) — the
    # empty-side fragment rule (see orchestrator
    # _try_empty_side_fragment).
    enable_empty_side_rule: bool = True
    # EXTEND-08: deterministic docs-union for changelog-shaped
    # conflicts (both sides' entries additive; oracle = union in
    # 48/51 corpus cases). See capybase.docs_union.
    enable_docs_union: bool = True
    # S27-48: deterministic list-union for one-entry-per-line name
    # lists (AUTHORS/.mailmap/CONTRIBUTORS): current side's lines +
    # replayed additions, target dedups respected. See
    # capybase.list_union.
    enable_list_union: bool = True
    # S27-54: deterministic convergence seed — on paths the scenario
    # registered as CONVERGED (target tip == source tip content), the
    # tips' content IS the final file (census: 1080/1080 oracle
    # agreement). Registered by the scenario harness like the race
    # seeds; dormant without evidence.
    enable_convergence_seed: bool = False
    # NOTE: wired but DORMANT — the unit-level shape gate is
    # vacuous (any modify/modify conflict passes it); the true
    # move-race evidence needs cross-file context (the def
    # duplicated at a different path in the replayed tree),
    # which belongs to the scenario-level consumer.
    enable_def_site_race: bool = False
    # SBCR (combination-search) tuning. The fitness is character-level Gestalt
    # similarity, mean-aggregated over both parents (arXiv:2605.16646 §4.1).
    # These knobs make the research-tuned parameters configurable without code
    # changes; the defaults are prior work's recommended values.
    # Minimum fitness for SBCR to accept a candidate. Below this the candidate
    # is essentially one-sided and is left to the LLM.
    sbcr_floor: float = 0.6
    # fitness evaluations (§2.2 stagnation; §4.1 tunes to 10).
    sbcr_stagnation_limit: int = 10
    # Wall-clock budget (seconds) for hill climbing on large blocks (§4.1 tunes
    # to 15s). The exhaustive path (≤ EXHAUSTIVE_THRESHOLD candidates) is
    # already bounded and ignores this.
    sbcr_max_time_seconds: float = 15.0
    # Hard budget on total fitness evaluations for hill climbing (§2.2).
    sbcr_max_iterations: int = 2000
    # Refined-block search (S27): when the marker sides over-include shared
    # context, interleave the diff3 conflict blocks and fill the winners into
    # the clean-merge skeleton instead of interleaving the raw sides (whose
    # space duplicates the context in every candidate — see sbcr.py's
    # refined-block note). Falls back to the raw path per-unit whenever the
    # blocks aren't clean add/add or fall below the floor.
    sbcr_use_refined: bool = True
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
    # Codegen-banner takeover (S28-147 engine half): extends the
    # generated-file take's signature beyond the php arginfo BLOCK
    # pattern to any codegen BANNER in the file's first 40 lines
    # ("GENERATED", "@generated", "DO NOT EDIT", "automatically generated
    # by"). Class census: 50/1,502 corpus cases; measured conversions
    # php-0094/0033 (WORKING 0.69 -> churn-winner presence 1.000) and
    # duckdb-0107 (ESCALATE 0.0 -> 0.998). Lockfile paths are excluded
    # (their dedicated arm owns them). Side-take of the churn winner,
    # validator-gated like every whole-file resolution.
    enable_generated_banner_takeover: bool = True
    # Source-derived candidate portfolio: before calling the LLM, try a small
    # set of candidates assembled from exact source lines (current-only,
    # replayed-only, both concatenated). Research shows 87% of resolutions
    # contain only input-side lines. Zero LLM calls when a candidate passes.
    enable_source_portfolio: bool = True
    # Minimum non-blank lines in the kept block for block-capture to engage.
    # Below this the full-LLM path reproduces the block fine; above it
    # reproduction becomes unreliable and the decision-style prompt takes over.
    block_capture_min_lines: int = 50
    # Churn-asymmetry decline for block capture (S28-143, the scikit-learn-
    # 0002 class: replayed deleted 747 lines while current's edits were 42
    # — 17.8x — and capture's keep_block verdict resurrected the block the
    # oracle deleted). When the DELETING side's fragment churn exceeds this
    # multiple of the keeper's, the deletion is a wholesale rewrite and the
    # decision exceeds a binary keep/delete: capture declines (no model
    # call) and the churn-aware whole-file machinery (wholesale-winner
    # floor) owns the choice. 0 disables the gate.
    block_capture_max_churn_asym: float = 5.0
    # Entity-boundary sub-conflict splitting (C/C++): when a single marker block
    # on an oversized file spans multiple top-level entities (functions, structs,
    # globals), split it into one sub-conflict per entity. Each sub-unit becomes
    # a small, self-contained prompt that fits the model window, where the whole-
    # file prompt would blow it (98/133 sqlite cases are rejected today by the
    # 48K-char guard for exactly this reason). The sub-spans partition the parent
    # marker_span exactly, so the existing multi-hunk splice layer reassembles
    # them with no change. Sibling resolutions are fed forward one-way into each
    # later sub-unit's prompt. Splitting happens at unit construction (extractor),
    # so every downstream stage (Phase 1 loop, prompt builder, per-unit verify,
    # splice, Phase 2 blame) treats sub-units uniformly.
    #
    # Always-on and ADAPTIVE (like the history-aware features): there is no
    # master on/off flag. Splitting fires only where its own data says it is
    # appropriate — a splittable language, a marker block whose region exceeds
    # ``entity_split_min_lines``, and >1 top-level entity inside it. The two
    # knobs below tune where that line is drawn, not whether splitting exists.
    # Only split a marker block whose region exceeds this many lines. Below it the
    # whole-block prompt is already small and splitting just adds overhead.
    entity_split_min_lines: int = 40
    # Minimum sub-region size (lines) to emit as its own sub-unit. Prevents
    # shredding a 45-line block into one-line slivers at dense entity boundaries
    # (e.g. a block of typedefs). Sub-regions smaller than this are merged into
    # the preceding sub-unit.
    entity_split_min_sub_lines: int = 8
    # Deferred Comment Reconciliation: after the code-resolution pass produces
    # test-passing code, a second CEGIS pass reconciles comments. Comments are
    # classified (machine/legal/generated/doctest vs deferred-prose), deferred
    # comments are masked from the code model (more context, less confusion),
    # then reconciled in a structured pass that enforces executable-token
    # equality (the comment pass can NEVER corrupt code). Skipped entirely when
    # no deferred comments overlap the conflict region (zero overhead). Default
    # ON (always-on integral part), but the skip-when-empty gate means files
    # with no affected comments pay no cost.
    enable_comment_reconciliation: bool = True
    # Common-span factoring (S28-136, default OFF pending the targeted
    # oversized-case pilot): when the three conflict sides share large
    # identical line runs, the prompt renders the sides as differing
    # segments + ordered @An references, and the model's resolution may
    # reference them; capybase re-expands verbatim and the full validation
    # pipeline gates the reconstruction. Oversized units (sides alone
    # exceeding the window) get an LLM chance they otherwise never had.
    enable_common_span_factoring: bool = False
    # Commit-intent context (S28-138 Fix B): surface the replaying commit's
    # non-conflicting hunks for the conflict's file in the prompt — the
    # commit's own diff is the cheapest evidence of what the edit was trying
    # to accomplish (deterministic, zero model calls). Budget-trimmed between
    # the structural anchor and the near-miss draft. Default ON after the
    # 12-case A/B: verdict parity (10 PASS both arms), no regressions, sim
    # 0.99→1.00 on protobuf-0057 (treatment), block present in 3/12 cases'
    # prompts (the multi-hunk population it serves).
    enable_commit_intent_context: bool = True
    # Ordered splice selection (S28-139): for additive conflicts SBCR declined
    # with fitness in the ambiguity band, the model returns a tiny
    # selection/order answer over the sides' blocks and capybase materializes
    # verbatim. Default ON after the A/B: verdict parity in both placements,
    # a verdict conversion as a post-failure rescue (libuv-0056 ESCALATE 0.42
    # -> NEAR_MATCH 0.84), the pre-LLM placement's one regression (libuv-0089)
    # eliminated by generation-first ordering, zero protocol failures, and
    # bounded downside (it only fires where the loop already failed and the
    # alternative is escalation).
    enable_ordered_splice: bool = True
    # Lower edge of the ambiguity band: below this the composition itself is
    # implausible (SBCR's decline is a capability signal, not an ordering
    # signal) — defer to normal generation.
    splice_fitness_low: float = 0.40
    # Upper edge is sbcr_floor (0.60): at/above it SBCR already accepted.
    splice_max_chunks: int = 16
    splice_max_glue_insertions: int = 4
    splice_max_glue_lines: int = 3
    # Near-miss seeding (S28-128; default ON after the paired targeted A/B
    # won: 12/13 slice cases identical-or-better, no attributable regression,
    # seeded cases resolved in fewer LLM attempts with 27-51% less wall time
    # — ledger S28-128 for the table): when the structural resolver or SBCR
    # already produced a candidate and validation REJECTED it, the draft +
    # its validator diagnostic seed the model's FIRST resolution instead of
    # being discarded (CEGIS one stage earlier, zero extra model calls).
    # EPHEMERAL: the seed drops out of prompts once the model has produced a
    # usable candidate of its own; all-or-nothing under the token budget
    # (below obligations in priority). Scope: the standard LLM resolution
    # path only (repair/two-pass/shatter/block-capture prompts do not
    # receive it).
    enable_near_miss_seeding: bool = True
    # Deterministic import-union editor (Rust): after the model produces a
    # candidate, if change-accounting detects it copied one side verbatim and
    # thereby dropped an additive ``use`` import leaf from the other side,
    # insert the missing leaf mechanically — no second model call. This is the
    # first Tier-A primitive of an obligation-driven structural merge layer:
    # deterministic, idempotent, transactional, and conservative (AMBIGUOUS on
    # any doubt). The existing cargo/rustc gauntlet remains the authoritative
    # check AFTER the edit. Default ON (it is a strict no-op on every non-
    # APPLIED path — no imports missing, or no compatible destination, or a
    # safety precondition fails → the candidate is untouched and the normal
    # preservation → repair flow proceeds unchanged). Flip off to force the
    # model to handle every import merge itself.
    enable_import_union: bool = True
    # Deterministic deletion-application editor (Rust): when change-accounting
    # detects the model copied a side that KEPT lines the other side intended to
    # DELETE (a DROPPED_DELETION obligation), remove those lines mechanically.
    # Tier-A primitive: deterministic, idempotent, transactional (brace-balance
    # checked after removal). Symmetric to import-union. Default ON.
    enable_deletion_union: bool = True
    # Deterministic block-insertion editor (Rust): when change-accounting
    # detects the model dropped a contiguous additive block (macro-gated code,
    # re-exports, doc comments) from the other side, transplant the block
    # verbatim at its correct position (determined by anchor lines). Tier-A
    # primitive: deterministic, idempotent, transactional (brace-balance
    # checked), conservative (AMBIGUOUS when anchors can't be uniquely located).
    # Default ON.
    enable_block_insertion: bool = True
    # Deterministic manifest-union editor (TOML/Cargo.toml): unions feature
    # arrays, workspace member arrays, and transplants new dependency entries.
    # Tier-A primitive: deterministic, idempotent, transactional (bracket-balance
    # checked), conservative. Version bumps are NOT unioned (exclusive choices).
    # Default ON.
    enable_manifest_union: bool = True
    # Deterministic attribute/meta-list union (Rust): unions #[derive(...)]
    # trait lists and #[allow(...)] / #[warn(...)] lint lists. Policy-driven:
    # cfg/repr/serde are opaque, deny/forbid are never unioned. Tier-A for
    # built-in derives, Tier-B risk_flags for external derives. Default ON.
    enable_attribute_meta_union: bool = True
    # Deterministic named-field union (Rust): inserts struct fields that the
    # model dropped. Same field name → exclusive; tuple structs → AMBIGUOUS;
    # repr(C)/repr(packed)/serde → Tier-B risk_flags. Default ON.
    enable_named_field_union: bool = True
    # Deterministic keyed-item union (Rust): inserts methods, functions,
    # associated items into impl/mod/trait blocks. Same item name → exclusive;
    # macro_rules! → refused. Default ON.
    enable_keyed_item_union: bool = True
    # Whole-file import deduplication linker (Phase 9): after per-unit
    # resolution is spliced into the full file, remove duplicate `use`
    # statements before whole-file cargo validation. The #1 cause of
    # WHOLE_FILE_FAILED is duplicate imports at the file level. Default ON.
    enable_file_linker: bool = True
    # True-side asymmetry takeover (protobuf-0073 class): when one side
    # rewrote the file wholesale (full-file churn ratio >= 0.90, dominant
    # churn) and the per-unit merge is >= 15% stale content absent from
    # that winning side, swap the merge for the winner's pristine stage file
    # (verified + adjudicated like the duplicate-definition path). Enabled
    # after live calibration: true positives 0073 (stale 0.3475) and 0067
    # (stale 0.2918), zero false positives across the >= 0.90 band and
    # mid-band controls (worst good-merge stale 0.021 vs threshold 0.15).
    enable_true_side_asymmetry_takeover: bool = True
    # Lockfile generated-file takeover (sprint-20 S20.5): a conflict whose
    # file is Cargo.lock resolves to the CURRENT side's pristine stage file
    # before the per-unit cascade — lockfiles are @generated regeneration
    # artifacts (the meaningful merge happens in the manifest), and the
    # real-world lockfile oracle IS the current side's regeneration in
    # practice (measured on both corpus Cargo.lock cases: 21/21 current-only
    # pins kept, 0/38 replayed-only, ~99.7% of divergent package keys take
    # current's block; axum-0017 otherwise burns 103 LLM units for a WORKING
    # at sim 0.625). Verified like every whole-file swap; a failed verify
    # declines to the per-unit path. Name-scoped, not suffix-scoped — extend
    # only with per-format oracle evidence.
    enable_lockfile_takeover: bool = True
    # Micro-CEGIS at the compiler-authority gate (sprint-20 S20.6): when the
    # pre_continue build fails with errors positively attributed to merged
    # files (the P4 override shape, protobuf-0065: buffer at sim 0.996 to the
    # oracle), attempt a bounded micro-repair before escalating —
    # deterministic duplicate deletion for 'redefinition of X' (provenance:
    # the base-verbatim copy a parent side deleted) and a tiny LLM
    # SEARCH/REPLACE patch for missing-symbol errors. Compiler-gated: every
    # patch re-runs the same gate; no gate progress escalates exactly as
    # before. One round, <=3 patches.
    enable_micro_cegis: bool = True
    # R3 (sprint-23): within-session best-of-N — on compile-gate failure,
    # generate up to 2 additional diverse-temperature candidates and accept
    # the first that passes all hard gates. Default False: the extra model
    # calls are a cost-benefit decision per deployment (the eval enables it).
    enable_best_of_n: bool = False
    # Move-and-edit transposition (sprint-20 S20.8): one side moved a base
    # block while the other edited it in place — a deterministic transpose
    # of the editor's delta onto the moved block (compiler-gated) is the
    # planned enable. JOURNAL-ONLY stage: the shape is detected and
    # journaled (move_edit_candidate) on every unit; enabling waits for
    # live distribution data (sprint-18 measure-first discipline).
    enable_move_edit_transposition: bool = False
    # Mid-band subsumption takeover (jsonc-0004 class): one side's churn
    # dominates the other's >= 2.5x while the normalized asymmetry sits in
    # [0.55, 0.90) — below the wholesale band where taking the winner is
    # safe on numbers alone. In this band 100/116 corpus oracles equal the
    # winner, but the 16 counter-examples (jsonc-0015, clickhouse-0015/
    # 0021/0043, ...) are genuine both-sides merges indistinguishable on
    # every shape metric, so the takeover fires ONLY when the LLM
    # subsumption adjudication confirms the winner's rewrite covers the
    # loser's intent (confidence >= 0.70); otherwise the per-unit merge
    # proceeds unchanged. Offline validation on the 45 active-corpus
    # mid-band cases: zero false-superseded on the risky set.
    enable_midband_subsumption_takeover: bool = True
    # Side-collapse guard (sea-orm-0027 class): when BOTH sides rewrote
    # >= 25% of the file (churn ratio < 0.90 — outside the side-pick
    # regimes) but the merged buffer is one side verbatim, the other
    # side's entire rewrite was silently dropped. The rejection is
    # LLM-gated (subsumption adjudication): escalate only when the dropped
    # rewrite is adjudicated not-superseded. A superseded verdict, an
    # unparseable/absent response, or no endpoint accepts the merge — the
    # conservative direction, mirroring the takeover's own gating.
    enable_side_collapse_guard: bool = True
    # Empty-response oversized floor (WS5): when a FRESH unit's first LLM
    # response is empty and its prompt is at/above this token estimate
    # (or >= 90% of a configured context window), skip the retries — the
    # endpoint returns empty for prompts past its effective limit, so
    # every retry of the same oversized prompt is a guaranteed 30-60s
    # dead burn. Recovery goes straight to the deterministic fallback /
    # portfolio; escalation if that declines. The propose-time oversized
    # check only catches the extreme class (> window AND > 10K tokens) —
    # this floor covers the 6K gray zone the pre-check tolerates.
    empty_oversized_token_floor: int = 6000
    # Wholesale winner floor: a wholesale-band file (churn_ratio >= 0.90,
    # dominant churn) must never END with its dominant rewrite wiped. The
    # fast path normally installs the winner, but it declines on an
    # adjudication "keep" or a winner that fails standalone verification —
    # and the per-unit cascade's catastrophic mode on such files is keeping
    # the loser's small edit and dropping the rewrite (sea-orm-0010:
    # winner preservation 0.01, sim 0.15 vs the winner's 0.99). The floor
    # is last-resort: it fires only when the final output preserves < 0.5
    # of the winner's churn, or when the cascade is about to escalate with
    # markers unresolved. Woven merges (sea-orm-0009) preserve the winner
    # and never floor.
    enable_wholesale_winner_floor: bool = True
    # First-empty fast-fail (7b6ae57): on the model's FIRST empty response,
    # skip the retry ladder and try the two verified single-side candidates.
    # Disabled by the escalation-path unit tests so they still exercise
    # their target mechanisms (no-progress guard, transient-failure
    # escalation, whole-file repair) instead of being rescued here.
    enable_empty_fast_fail: bool = True
    # Whole-side repair rung (sprint-19 P1, the tokio-0109/0037 class):
    # when the spliced buffer fails a whole-file COMPILE gate (cargo check,
    # the Phase-2 build test, an attributed build failure), probe both
    # pristine merge-index stage sides as whole-file candidates. Decision
    # matrix: neither side verifies → decline (repair proceeds as before);
    # exactly one verifies → take it only when the subsumption adjudication
    # confirms the failing side's work is superseded (confidence >= 0.70);
    # both verify → the repair adjudication must pick a side with
    # confidence >= 0.70 ("neither" or a low-confidence answer declines —
    # the woven class keeps its CEGIS repair). NEVER pre-emptive: churn
    # numbers cannot separate one-side oracles from woven merges (79
    # corpus counter-examples), so the rung only fires on an actual
    # compile failure of the reconstruction. Every probe is journaled as
    # whole_side_probe; the swap as whole_side_repair.
    enable_whole_side_repair_rung: bool = True
    # File-level drift rescue (S28-149, the libuv-0056 class): when the
    # whole-file validation fails AFTER unit acceptances AND a repair
    # round has also failed (drift persisting through the CEGIS repair —
    # 0056: failed twice, the loop kept accepting units, final 0.85 with
    # the correct side verbatim at a stage), re-evaluate the pristine
    # stage sides AS THE FILE against the same validation; a side that
    # file-validates is taken over the drifted assembly. Never fires on
    # first failure — the repair loop owns that shape. The validator is
    # the acceptance authority (no LLM adjudication — the wsr rung owns
    # the compile-flavored, adjudication-gated shapes; this arm
    # partition-skips when that rung already ran). When both sides
    # validate, the churn winner breaks the tie (the wholesale-winner
    # heuristic). Journaled as side_takeover_rescue /
    # side_takeover_rescue_declined.
    enable_side_takeover_rescue: bool = True
    # Same-signature repetition stop (S28-140, the nlohmann-json-0038
    # class): when the whole-file validation fails with the IDENTICAL
    # hard-failure signature for N consecutive repair rounds
    # (cegis_convergence_threshold, floor 2) AND at least one of those
    # rounds drew a model candidate, the loop is producing zero new
    # information — stop retrying and fall to the exhaustion endgame
    # (wholesale floor, F1 arms, drift rescue) immediately. A
    # deterministic-only repeated window does NOT stop the loop: the
    # model re-resolve it precedes has not been given its chance yet.
    # Journaled as same_signature_stop.
    enable_same_signature_stop: bool = True
    # No-clear-progress stop (S28-158, the REPAIR_FAILURE class — 21
    # harvest rows, 13 at sim >= 0.89, php-0148 at 0.998): N model-drawn
    # repair rounds that only REDUCE (or leave UNCHANGED) the failure set
    # without ever CLEARING it end the loop into the exhaustion endgame —
    # the whack-a-mole tail is not converging and the drift rescue can
    # still land a validating side. CLEARED resets the counter;
    # deterministic-only rounds never count (the model keeps its chance).
    # Journaled as no_clear_progress_stop.
    enable_no_clear_progress_stop: bool = True
    no_clear_stop_rounds: int = 3
    # Best-of-N preservation recovery (sprint-19 P2): when the
    # preservation heuristic rejects an otherwise-validation-passing
    # candidate and EVERY heuristic-forced retry then validates strictly
    # worse (hard failures — syntax errors, empty output), restore the
    # rejected candidate instead of escalating. The restored candidate is
    # tagged flagged_by_preservation_heuristic so the file-level
    # side-collapse guard and post-hoc analysis can see the unit's
    # acceptance went against the unit-level heuristic's judgment. It is
    # a RECOVERY mechanism, not a policy change: the heuristic still
    # fires, retries still run, and an equal-or-better retry is used (the
    # rescue never preempts a real acceptance).
    enable_preservation_bestof_n: bool = True
    # Class-with-methods splitting (sprint-19 P5): the v3 entity splitter
    # sees a C++ class as ONE top-level entity, so an oversized region
    # dominated by a single class yields one giant fragment and the unit
    # escalates as oversized (protobuf-0055: 16.3K-token prompt vs an 8K
    # window before any candidate exists). Member-function boundaries
    # inside the class body are measurable (depth-2 entities, access-
    # specifier aware); this flag is JOURNAL-ONLY until the corpus
    # calibration confirms the split is splice-safe and brings such
    # regions under the window — the measurement stamps
    # class_member_split_candidate metadata and journals it at the
    # oversized-skip sites.
    enable_class_member_splitting: bool = True
    # Phase 4 comment jury (design §5). An untrusted semantic sensor that
    # evaluates comment claims produced by the comment pass. Three operating
    # modes:
    #
    #   ``off``    — the jury never runs (the default; zero overhead).
    #   ``shadow`` — the design's JURY_SHADOW setting: the jury atomizes the
    #                final plan's rewritten comments into claims, builds evidence
    #                packets, runs the contradiction + provenance jurors, and the
    #                deterministic chair routes — but EVERY route becomes
    #                ``shadow_record`` (no merge effect). The data is journaled
    #                as ``jury_shadow_*`` events + stored as ``jury_verdict``
    #                artifacts for offline analysis. This is also the one-action
    #                kill switch for ``enforce`` (set ``jury_mode = "shadow"`` to
    #                return a live canary to no-merge-effect observation).
    #   ``enforce``— the jury runs AFTER deterministic gates + comment
    #                reconciliation + executable-fingerprint check, and its
    #                routed outcomes are ACTED ON: accept only when no blocking
    #                finding; comment_counterexample feeds a bounded jury-driven
    #                comment CEGIS re-loop; human_review stops and preserves a
    #                review bundle; code_reopen is gated by
    #                ``enable_jury_code_reopen`` (default off → satisfied reopen
    #                becomes human_review, never accept/suppression).
    #
    # The jury may NEVER override parsing, compilation, testing, fingerprint,
    # policy, or other deterministic failures. All unknown/degraded states fail
    # closed to human_review.
    jury_mode: Literal["off", "shadow", "enforce"] = "off"
    # Autonomous jury-driven code reopen. The shadow corpus contains no positive
    # ``code_reopen`` example, so this is separately gated: default OFF. When OFF
    # and a reopen request would otherwise be satisfied (full evidence quorum),
    # the route becomes ``human_review`` — never ``accept`` and never silently
    # suppressed. Turn on ONLY when positive-path evidence exists outside the
    # shadow run (tracked as a residual risk, not part of the Python canary).
    enable_jury_code_reopen: bool = False
    # Jury-driven comment CEGIS loop budget (enforce mode). After the jury emits
    # a ``comment_counterexample`` for a claim, the comment pass is re-run from
    # the SAME frozen code + authoritative ledger with the counterexample as a
    # seed failure. This bounds the re-loop; on exhaustion / no progress / a
    # repeated counterexample, the case routes to ``human_review``. 0 disables
    # jury-driven re-opening entirely (counterexamples route to human_review).
    jury_comment_cegis_budget: int = 2
    # Eligibility allowlist for the enforcement canary. The jury runs in
    # ``enforce`` ONLY when the conflict's language is in
    # ``jury_eligible_languages`` — the orchestrator-enforceable gate (it knows
    # the file's language but not its dataset origin). Everything else stays in
    # shadow/off regardless of ``jury_mode``.
    #
    # The default ``["python"]`` restricts enforce to the validated envelope
    # (the shadow corpus is Python-only). Expand only after a target language
    # has its own shadow run + golden replay.
    jury_eligible_languages: list[str] = Field(
        default_factory=lambda: ["python"])
    # Whether an enforce-mode ``human_review`` outcome BLOCKS the merge (returns
    # None → the file keeps its frozen code + a review bundle is written, the
    # rebase stops for that file) or is advisory (records + writes a bundle but
    # lets the merge proceed). The brief's contract is block=True (the safe
    # default); set False only for an observe-and-flag deployment.
    jury_human_review_blocks: bool = True
    # Configuration + prompt version stamps recorded in the flight recorder so
    # a replay is reconstructable and a config-version mismatch is detectable.
    # Bumping these invalidates the replay cache (forces re-evaluation).
    jury_config_version: str = "jury-cfg-v1"
    jury_prompt_version: str = "jury-prompt-v1"


# --- [mechanisms] / [experimental] partition -------------------------------
# FutureConfig is WRITTEN as two toml sections: [mechanisms] (stable wired
# toggles + their tuning) and [experimental] (dormant/planned seams and the
# jury canary knobs). One python model backs both; the loader folds both
# tables onto the `future` attribute. This partition is the single source
# of truth for the loader diagnostics, the v1 migration, and
# `capybase config explain`.
FUTURE_EXPERIMENTAL_FIELDS = frozenset({
    "enable_convergence_seed",
    "enable_def_site_race",
    "enable_move_edit_transposition",
    "enable_best_of_n",
    "jury_mode",
    "enable_jury_code_reopen",
    "jury_comment_cegis_budget",
    "jury_eligible_languages",
    "jury_human_review_blocks",
    "jury_config_version",
    "jury_prompt_version",
})
FUTURE_MECHANISMS_FIELDS = (
    frozenset(FutureConfig.model_fields) - FUTURE_EXPERIMENTAL_FIELDS
)

# The config schema version this capybase loads and writes. Version 1 is the
# pre-features layout ([future] holding everything; activation gates scattered
# across [memory]/[structural]/[validation]); version 2 introduced
# schema_version itself, [features], and the mechanisms/experimental split.
SCHEMA_VERSION = 2

# v1-only keys, mapped to their v2 replacement (display form). In a v1 file
# they are MIGRATED; in a v2 file they are IGNORED with this hint.
DEPRECATED_V1_KEYS = {
    "future.enable_structural_resolver": "features.structural_resolution",
    "future.enable_combination_search": "features.combination_search",
    "future.enable_rag": "features.rag",
    "future.enable_self_consistency": "model.enable_self_consistency",
    "future.enable_shadow_jury": 'future.jury_mode = "shadow"',
    "memory.enabled": "features.rag",
    "structural.enabled": "features.structural_context",
    "validation.enable_verifier_model": "features.llm_critic",
}


class StructuralConfig(BaseModel):
    """Tree-sitter AST parsing for structural context + preservation checks.

    When enabled and the ``structural`` optional deps are installed, the
    conflict extractor populates ``ConflictUnit.structural_metadata`` with the
    lowest enclosing AST node (e.g. the specific ``def``/``impl``) so the
    resolver and validators see a logical block rather than an arbitrary line
    window. The abstract parser is imported lazily; when the language is
    unrecognized or parsing fails, capybase silently degrades to the
    line-window behavior.

    ACTIVATION moved to [features] structural_context (FeaturesConfig) —
    the flag that used to live here. This section carries only the
    context-shaping knobs.
    """

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
    # Mask DEFERRED comments (prose, TODOs, invariants, narration) from the
    # conflict sides + primary context shown to the code-resolution model, while
    # leaving MACHINE/LEGAL/GENERATED/DOCTEST comments visible. The upstream
    # half of the two-level comment architecture (design §4): the code model
    # sees the executable code + machine-significant directives, but not stale
    # prose that could confuse it. The reconciliation pass (Phase 3) then
    # rewrites the deferred comments from provenance.
    #
    # Length-preserving and offset-correct: the executable-token stream is
    # IDENTICAL before/after masking (verified by the round-trip invariant test).
    # Zero overhead for files with no deferred comments — mask_deferable_comments
    # returns the original text unchanged. Default True (always-on per design).
    mask_deferred_comments: bool = True
    # Refine conflict boundaries with `git merge-file --diff3` to get the
    # tightest possible marker span (git may auto-resolve adjacent lines).
    refine_with_diff3: bool = True
    # xdiff backend for ``git merge-file`` alignment. Histogram
    # anchors on rare lines → tighter, more stable conflict regions than Myers
    # on noisy code (conflict-size reduction in ~10% of conflicting merges).
    # One of "histogram" (default), "patience", "minimal", "myers". Unknown
    # values fall back to histogram silently; refinement is advisory only.
    diff_algorithm: Literal["histogram", "patience", "minimal", "myers"] = "histogram"
    # Sesame-style separator projection: for brace/semicolon
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
    few-shot demonstrations.

    ACTIVATION moved to [features] rag (FeaturesConfig) — this section
    carries only the store/retriever knobs.
    """

    store_path: str = ".rebase-agent/memory/experiences.jsonl"
    # "lexical" (dependency-free BM25, the default) or "embedding" (semantic
    # retrieval via the /v1/embeddings endpoint, The embedding
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
    # transform and use the calibrated red_threshold floor. Empty
    # until ``calibrate-embeddings`` runs; the profile overrides this at runtime.
    embedding_calibration: dict[str, Any] = Field(default_factory=dict)
    # Hybrid-retrieval fusion method, read only when ``retriever == "hybrid"``
    #. "rrf" (default, rank-only, scale-agnostic) or "dbsf"
    # (min-max normalized score sum). The profile may override this.
    fusion_method: str = "rrf"
    # Persisted vector cache for the embedding retriever.
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
    # RAG into the repair/retry path. The repair prompt
    # previously carried NO few-shot — the A/B failure site where the model
    # reproduces the same dropped-side merge across retries. Quality filter:
    # only surface examples that converged within this many retries:
    # index-quality rule — merges that took many retries may have converged by
    # luck, not a generalizable strategy. -1 disables the filter.
    repair_retrieval_max_retries: int = 2
    # Higher floor than fresh-generation for the repair path (the cost of a
    # misleading example is higher when the model is already fixing a specific
    # error). Applied in addition to the per-retriever min_similarity.
    repair_retrieval_min_similarity: float = 0.55
    # Session-level drift detection (behavioral-regression redesign). Advisory
    # only — never blocks a merge. The first-gen detector embedded a prose
    # anchor and cosine-compared it to merged code; an external review showed
    # that cross-modal comparison has no operating point, so it was scrapped
    # (see docs/drift-detector-review.md). The replacement gates on resolution
    # mechanism (deterministic resolutions emit nothing) and fires only when an
    # LLM resolution introduces a test regression (baseline-passing test that
    # now fails — the 0%-FPR behavioral signal). No threshold to calibrate.
    enable_drift_detection: bool = False


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
    # Empty by default: there is NO ambient calibration. The provider-named
    # profile (apply_to_config) is the canonical source; a live run without
    # one is an error at the entry points. An explicit non-empty path is a
    # deliberate override (tests, experiments) — never repo-local by default.
    model_profile_path: str = ""
    escalate_threshold: float = 0.7
    # Consensus entropy above this → escalate (high-entropy splits mean no
    # candidate is trustworthy). 0=unanimous, 1=maximally split. Set high
    # (0.8) because even a 2-of-3 majority produces non-trivial entropy; we
    # only want to escalate when samples are *maximally* split.
    entropy_escalate_threshold: float = 0.8


class RoutingConfig(BaseModel):
    """Difficulty-aware routing (ICoT/RoutingGen pattern).

    Classifies a conflict as ``simple`` or ``complex`` *before* any LLM call
    using structural signals already on the ConflictUnit. Simple conflicts (a
    single isolated hunk) take a fast path (one low-temp sample, no two-pass,
    no consensus); complex ones (multi-hunk, large nodes) get the full
    test-time pipeline. Concentrates compute where a 3B model struggles and
    cuts ~half the tokens on easy cases. Disabled by default (opt-in).
    """

    enabled: bool = False
    # Minimum conflict balance for SBCR to ACCEPT outright. Balance
    # = min/max of the two sides' non-blank line counts (1.0 = equal, →0 =
    # heavily imbalanced). SBCR wins on balanced conflicts and loses to the LLM
    # on imbalanced ones (arXiv:2605.16646 §4.2), so below this threshold an SBCR
    # result is NOT short-circuited — the LLM runs instead. 0.15 is a slightly
    # conservative floor vs the research's ~0.2 crossover; 0.0 disables the guard
    # (always accept SBCR when it resolves).
    min_balance_for_sbcr_accept: float = 0.15


class FeaturesConfig(BaseModel):
    """Section-level feature switches — ONE controlling key per feature.

    These are the only activation gates; the per-mechanism detail toggles
    live in [mechanisms]/[experimental] (FutureConfig) and the tuning knobs
    in their domain sections ([structural], [memory]). Schema version 2.
    """

    # Deterministic structural pre-resolution (provably-safe model-free
    # rules before the LLM). Default ON — safe by construction: every
    # resolution still runs the full validation pipeline.
    structural_resolution: bool = True
    # Search-based combination resolution (SBCR) after the structural
    # resolver declines. Default ON.
    combination_search: bool = True
    # Structural AST context injection: populate ConflictUnit.structural_
    # metadata (enclosing node, signatures) so context/validators see
    # logical blocks. Default OFF (opt-in; parser availability permitting).
    structural_context: bool = False
    # RAG experience replay (few-shot retrieval from past accepted merges).
    # Default ON (sprint-21 golden path); retrieval is inert until the
    # store holds min_examples_for_retrieval examples. Single gate: the old
    # memory.enabled AND future.enable_rag conjunction is gone.
    rag: bool = True
    # Verifier-model critic: the LLM judge for silently-dropped intent.
    # Default ON (opt-out) — the only check for that failure mode.
    llm_critic: bool = True


class Config(BaseModel):
    features: FeaturesConfig = Field(default_factory=FeaturesConfig)
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

    # Loader diagnostics (NOT a toml key; excluded from model_dump): applied
    # migrations, ignored/deprecated keys, unknown-key suggestions. Populated
    # only by Config.load from files; programmatic Config() stays silent.
    _load_diagnostics: list[str] = PrivateAttr(default_factory=list)
    # The schema version the loader found (after migration) — for
    # `capybase config explain`.
    _loaded_schema_version: int = PrivateAttr(default=2)
    # Dotted key path -> human-readable source layer for every value that
    # came from somewhere OTHER than the built-in default (files, migration,
    # calibration profile, provider config, CLI flag). For
    # `capybase config explain`.
    _value_sources: dict[str, str] = PrivateAttr(default_factory=dict)

    def record_source(self, dotted_path: str, source: str) -> None:
        """Record where a post-load mutation set a value (explain output)."""
        self._value_sources[dotted_path] = source

    @property
    def value_sources(self) -> dict[str, str]:
        """Where each non-default value came from (dotted path -> source)."""
        return dict(self._value_sources)

    @property
    def load_diagnostics(self) -> list[str]:
        """Diagnostics from the last Config.load (migrations, ignored keys)."""
        return self._load_diagnostics

    @property
    def loaded_schema_version(self) -> int:
        return self._loaded_schema_version

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

        ``calibration.model_path`` pointing at the legacy repo-relative
        default is rewritten to live in ``config_dir`` (machine/user-specific,
        shared across repos). An explicit absolute path set in the toml is
        always respected (a deliberate override). The RAG experience store
        stays repo-relative (repo-specific merge patterns). Model profiles
        are NOT loaded from any ambient path — provider configs are the
        canonical profile source (``calibration.model_profile_path`` is only
        the output path ``capybase calibrate`` writes).
        """
        cdir = Path(config_dir).expanduser() if config_dir else default_config_dir()
        # STRICT (s27-extend-41): --config naming an existing FILE must not
        # silently fall back to defaults (the live tier-B smoke lost its
        # [future] overrides exactly this way — a file path made the
        # <dir>/capybase.toml lookup miss and the run used defaults). The
        # contract is a DIRECTORY; an absent dir stays "no config" (first
        # run), but a present non-directory is a user error — name it.
        if config_dir is not None and cdir.exists() and not cdir.is_dir():
            raise NotADirectoryError(
                f"--config expects a DIRECTORY containing capybase.toml, "
                f"got the file: {cdir} — pass the file's directory "
                f"(or use a repo-local ./capybase.toml)")
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
            diags: list[str] = []
            sources: dict[str, str] = {}
            if repo_local is not None and dir_toml.is_file() and resolved != dir_toml:
                # EACH file migrates to the current schema BEFORE merging: a
                # v1 config-dir file under a v2 repo-local override must get
                # its v1 keys MIGRATED, not silently ignored.
                with open(dir_toml, "rb") as fh:
                    base_data = tomllib.load(fh)
                with open(resolved, "rb") as fh:
                    override_data = tomllib.load(fh)
                base_sources: dict[str, str] = {}
                override_sources: dict[str, str] = {}
                _collect_leaf_sources(
                    base_sources, base_data, f"config-dir file ({dir_toml})")
                base_data, base_ver = _normalize_config_dict(
                    base_data, diags, base_sources)
                _collect_leaf_sources(
                    override_sources, override_data,
                    f"repo-local file ({resolved})")
                override_data, override_ver = _normalize_config_dict(
                    override_data, diags, override_sources)
                sources.update(base_sources)
                sources.update(override_sources)
                data = _deep_merge_toml(base_data, override_data)
                version = max(base_ver, override_ver)
                cfg = cls.model_validate(data)
            else:
                with open(resolved, "rb") as fh:
                    data = tomllib.load(fh)
                _collect_leaf_sources(sources, data, f"file ({resolved})")
                data, version = _normalize_config_dict(data, diags, sources)
                cfg = cls.model_validate(data)
            cfg._load_diagnostics.extend(diags)
            cfg._value_sources.update(sources)
            cfg._loaded_schema_version = version
            cfg.source_path = str(resolved)
        # Rewrite the calibration artifacts to the config dir. A user repo has
        # no business carrying calibration data — it's the model endpoint's
        # capability profile, shared across every repo on this machine. Only the
        # repo-relative defaults are rewritten; an explicit path in the toml is a
        # deliberate override and is left alone.
        _relocate_calibration_paths(cfg, cdir)
        return cfg


def _collect_leaf_sources(
    sources: dict[str, str], table: dict, source: str,
    *, skip_existing: bool = False,
) -> None:
    """Record dotted paths -> ``source`` for every leaf key in a raw toml
    table (depth 2: section.key — the schema's fixed shape). Precedence:
    with ``skip_existing`` the caller's earlier (higher-precedence) layer
    wins; used for the config-dir layer under a repo-local override."""
    for section_name, section in table.items():
        if not isinstance(section, dict):
            sources[section_name] = source
            continue
        for key in section:
            dotted = f"{section_name}.{key}"
            if skip_existing and dotted in sources:
                continue
            sources[dotted] = source


def _normalize_config_dict(
    data: dict, diags: list[str], sources: dict[str, str] | None = None,
) -> tuple[dict, int]:
    """Normalize a raw parsed-toml dict to the current schema (in place).

    Returns ``(data, version)``. Version 1 files (``schema_version`` absent)
    get the v1->v2 moves applied and every applied rename recorded in
    ``diags``; version 2 files keep v1-only keys OUT (reported as
    deprecated+ignored). ``[future]``/``[mechanisms]``/``[experimental]``
    tables are folded onto the single ``future`` attribute; unknown sections
    and keys are reported with a nearest-match hint.
    """
    import difflib

    declared = data.pop("schema_version", None)
    if declared is None:
        version = 1
        diags.append(
            "schema_version absent — treated as 1 and migrated to "
            f"{SCHEMA_VERSION} (add schema_version = {SCHEMA_VERSION} to "
            "silence this notice)")
    else:
        version = int(declared)
        if version > SCHEMA_VERSION:
            diags.append(
                f"schema_version {version} is newer than this capybase "
                f"understands ({SCHEMA_VERSION}) — loading as {SCHEMA_VERSION}")
            version = SCHEMA_VERSION

    future_table = dict(data.pop("future", None) or {})
    mechanisms = dict(data.pop("mechanisms", None) or {})
    experimental = dict(data.pop("experimental", None) or {})
    features = dict(data.get("features", None) or {})
    model_table = dict(data.get("model", None) or {})
    memory_table = dict(data.get("memory", None) or {})
    structural_table = dict(data.get("structural", None) or {})
    validation_table = dict(data.get("validation", None) or {})

    if version < 2:
        # v1 -> v2 moves. Only keys actually present move; each move is
        # recorded so the run shows what the migration did.
        def _moved(old: str, new: str, value) -> None:
            diags.append(f"migrated v1 key {old} -> {new} = {value!r}")
            if sources is not None:
                sources[new] = f"migration from {old}"

        if "enable_structural_resolver" in future_table:
            value = future_table.pop("enable_structural_resolver")
            features.setdefault("structural_resolution", value)
            _moved("future.enable_structural_resolver",
                   "features.structural_resolution", value)
        if "enable_combination_search" in future_table:
            value = future_table.pop("enable_combination_search")
            features.setdefault("combination_search", value)
            _moved("future.enable_combination_search",
                   "features.combination_search", value)
        if "enable_self_consistency" in future_table:
            # v1 semantics: model OR future (the orchestrator OR'd the twins).
            legacy = bool(future_table.pop("enable_self_consistency"))
            model_table["enable_self_consistency"] = (
                bool(model_table.get("enable_self_consistency", False)) or legacy)
            _moved("future.enable_self_consistency",
                   "model.enable_self_consistency",
                   model_table["enable_self_consistency"])
        if "enable_shadow_jury" in future_table:
            legacy = bool(future_table.pop("enable_shadow_jury"))
            if legacy and future_table.get("jury_mode", "off") == "off":
                future_table["jury_mode"] = "shadow"
                diags.append(
                    'migrated v1 key future.enable_shadow_jury -> '
                    'future.jury_mode = "shadow"')
                if sources is not None:
                    sources["future.jury_mode"] = (
                        "migration from future.enable_shadow_jury")
            elif legacy:
                diags.append(
                    "v1 key future.enable_shadow_jury=true ignored — explicit "
                    "jury_mode wins")
        rag_conjuncts: list[bool] = []
        if "enable_rag" in future_table:
            rag_conjuncts.append(bool(future_table.pop("enable_rag")))
        if "enabled" in memory_table:
            rag_conjuncts.append(bool(memory_table.pop("enabled")))
        if rag_conjuncts:
            # v1 semantics: rag required BOTH flags.
            value = all(rag_conjuncts)
            features.setdefault("rag", value)
            diags.append(
                "migrated v1 keys future.enable_rag AND memory.enabled -> "
                f"features.rag = {value!r}")
            if sources is not None:
                sources["features.rag"] = (
                    "migration from future.enable_rag AND memory.enabled")
        if "enabled" in structural_table:
            value = structural_table.pop("enabled")
            features.setdefault("structural_context", value)
            _moved("structural.enabled", "features.structural_context", value)
        if "enable_verifier_model" in validation_table:
            value = validation_table.pop("enable_verifier_model")
            features.setdefault("llm_critic", value)
            _moved("validation.enable_verifier_model",
                   "features.llm_critic", value)

        data["model"] = model_table
        data["memory"] = memory_table
        data["structural"] = structural_table
        data["validation"] = validation_table
    else:
        # v2 file: v1-only keys are honored NOT at all.
        for table_name, table in (("future", future_table),
                                  ("memory", memory_table),
                                  ("structural", structural_table),
                                  ("validation", validation_table)):
            for key in list(table):
                dep = DEPRECATED_V1_KEYS.get(f"{table_name}.{key}")
                if dep is not None:
                    table.pop(key)
                    diags.append(
                        f"deprecated v1 key {table_name}.{key} ignored — "
                        f"use {dep}")

    if future_table and version >= 2:
        diags.append(
            "section [future] is deprecated in schema_version 2 — use "
            "[mechanisms] / [experimental]; its keys were applied")
    combined_future = dict(future_table)
    for key, value in mechanisms.items():
        if version >= 2 and key in FUTURE_EXPERIMENTAL_FIELDS:
            diags.append(
                f"[mechanisms].{key} belongs in [experimental] — applied "
                "anyway")
        combined_future[key] = value
    for key, value in experimental.items():
        if version >= 2 and key not in FUTURE_EXPERIMENTAL_FIELDS:
            diags.append(
                f"[experimental].{key} belongs in [mechanisms] — applied "
                "anyway")
        combined_future[key] = value
    if combined_future:
        data["future"] = combined_future
    if features:
        data["features"] = features

    # Unknown sections/keys: pydantic would silently drop them — surface that.
    known_sections = set(Config.model_fields) - {"source_path"}
    known_sections |= {"mechanisms", "experimental"}
    for section_name, table in list(data.items()):
        if section_name not in known_sections:
            hint = difflib.get_close_matches(
                section_name, sorted(known_sections), n=1)
            suffix = f" (did you mean '{hint[0]}'?)" if hint else ""
            diags.append(f"ignored unknown section [{section_name}]{suffix}")
            continue
        if not isinstance(table, dict):
            continue
        if section_name in ("mechanisms", "experimental"):
            valid = frozenset(FutureConfig.model_fields)
        else:
            valid = frozenset(
                type(getattr(Config(), section_name)).model_fields)
        for key in table:
            if key in valid:
                continue
            hint = difflib.get_close_matches(key, sorted(valid), n=1)
            suffix = f" (did you mean '{hint[0]}'?)" if hint else ""
            diags.append(
                f"ignored unknown key {section_name}.{key}{suffix}")
    return data, version


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
    # CONSTRAINTS #3: no remap, no discovery. The legacy repo-relative
    # default is left untouched (explicit is explicit); the default is ""
    # and nothing rewrites it.
    if cfg.calibration.model_path == _REPO_DEFAULT_CALIBRATION_PATH:
        cfg.calibration.model_path = str(config_dir / _CALIBRATION_FILENAME)


#: The three jury operating modes, as a frozenset for validation.
JURY_MODES = frozenset({"off", "shadow", "enforce"})


def effective_jury_mode(future: "FutureConfig") -> str:
    """The jury operating mode (kept as an accessor for orchestrator/replay).

    The legacy ``enable_shadow_jury`` back-compat flag was folded away: the
    config migration maps ``enable_shadow_jury = true`` to
    ``jury_mode = "shadow"`` at load time, so ``jury_mode`` is the only
    knob.
    """
    mode = getattr(future, "jury_mode", "off")
    return mode if mode in JURY_MODES else "off"


def jury_eligible(future: "FutureConfig", language: str | None) -> bool:
    """Whether the jury may run in ``enforce`` for a conflict of ``language``.

    The orchestrator-enforceable eligibility gate. ``shadow`` mode is always
    eligible (it's merge-neutral observation); ``enforce`` is restricted to the
    languages in ``jury_eligible_languages`` (default Python — the validated
    envelope). An empty allowlist means "all languages eligible" (opt-out of
    the gate). This is the single place the orchestrator checks eligibility, so
    the canary scope is enforced in one spot.
    """
    mode = effective_jury_mode(future)
    if mode != "enforce":
        return True  # shadow/off: no eligibility restriction (shadow is safe)
    allowed = getattr(future, "jury_eligible_languages", []) or []
    if not allowed:
        return True  # empty allowlist = gate inert
    from capybase.langs import canonical_language
    lang = canonical_language(language)
    # Aliases resolve through the ONE canonical map (reuse-design
    # stage 1: this gate previously re-spelled its own alias dict).
    allowed_norm = {canonical_language(a) for a in allowed}
    return lang in allowed_norm
