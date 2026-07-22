"""Tests for the jury on-by-default prerequisites:

- :func:`config.jury_eligible` — the orchestrator-enforceable eligibility gate
  (enforce restricted to jury_eligible_languages; shadow always eligible).
- the CLI ``--jury-mode`` / ``--no-jury`` flags overriding the config.
- the ``human_review`` contract: when ``jury_human_review_blocks`` is True
  (the default), an enforce human_review outcome BLOCKS the merge (the
  orchestrator returns None); when False it's advisory.
"""

from __future__ import annotations

import pytest

from capybase.config import Config, FutureConfig, effective_jury_mode, jury_eligible


# ---------------------------------------------------------------------------
# jury_eligible — the canary-envelope gate
# ---------------------------------------------------------------------------


class TestJuryEligible:
    def test_enforce_python_eligible_default(self):
        """The default envelope is Python-only (the validated shadow corpus)."""
        f = FutureConfig(jury_mode="enforce")
        assert jury_eligible(f, "python") is True

    def test_enforce_python_alias_eligible(self):
        f = FutureConfig(jury_mode="enforce")
        assert jury_eligible(f, "py") is True

    def test_enforce_rust_INELIGIBLE_default(self):
        """Rust is outside the default envelope → enforce is NOT eligible.
        This is the canary-scope guard: enforce won't run on a language that
        has no validated shadow corpus."""
        f = FutureConfig(jury_mode="enforce")
        assert jury_eligible(f, "rust") is False

    def test_shadow_always_eligible_regardless_of_language(self):
        """Shadow mode is merge-neutral observation, so it's always eligible
        (no scope restriction — observing is always safe)."""
        f = FutureConfig(jury_mode="shadow")
        assert jury_eligible(f, "rust") is True
        assert jury_eligible(f, "javascript") is True

    def test_off_always_eligible(self):
        f = FutureConfig(jury_mode="off")
        assert jury_eligible(f, "rust") is True

    def test_empty_allowlist_means_all_eligible(self):
        """An empty jury_eligible_languages = the gate is inert (opt-out)."""
        f = FutureConfig(jury_mode="enforce", jury_eligible_languages=[])
        assert jury_eligible(f, "rust") is True

    def test_explicit_rust_allowlist(self):
        """Expanding the envelope to Rust makes it eligible for enforce."""
        f = FutureConfig(jury_mode="enforce",
                         jury_eligible_languages=["rust", "python"])
        assert jury_eligible(f, "rust") is True
        assert jury_eligible(f, "rs") is True
        assert jury_eligible(f, "python") is True
        assert jury_eligible(f, "javascript") is False

    def test_none_language_ineligible_when_allowlist_set(self):
        f = FutureConfig(jury_mode="enforce", jury_eligible_languages=["python"])
        assert jury_eligible(f, None) is False


# ---------------------------------------------------------------------------
# CLI flag override
# ---------------------------------------------------------------------------


class TestCliJuryFlags:
    def _parse(self, *flags):
        from capybase.cli import build_parser
        # inspect is a no-op subcommand that accepts the global flags.
        return build_parser().parse_args(list(flags) + ["inspect"])

    def test_no_jury_flag_sets_true(self):
        a = self._parse("--no-jury")
        assert a.no_jury is True
        assert a.jury_mode is None

    def test_jury_mode_flag_sets_mode(self):
        for mode in ("off", "shadow", "enforce"):
            a = self._parse("--jury-mode", mode)
            assert a.jury_mode == mode
            assert a.no_jury is False

    def test_no_jury_and_jury_mode_are_mutually_exclusive(self):
        """The two flags can't be combined (argparse mutual-exclusion group)."""
        with pytest.raises(SystemExit):
            self._parse("--no-jury", "--jury-mode", "shadow")

    def test_default_no_flag_is_none(self):
        a = self._parse()
        assert a.no_jury is False
        assert a.jury_mode is None

    def test_apply_override_no_jury_sets_off(self):
        """Simulate the main() override: --no-jury forces jury_mode off."""
        from capybase.config import effective_jury_mode
        cfg = Config()
        cfg.future.jury_mode = "enforce"  # pretend config had enforce
        args = self._parse("--no-jury")
        # Replicate the main() override logic.
        if getattr(args, "no_jury", False):
            cfg.future.jury_mode = "off"
            cfg.future.enable_shadow_jury = False
        assert effective_jury_mode(cfg.future) == "off"

    def test_apply_override_jury_mode_wins_over_config(self):
        from capybase.config import effective_jury_mode
        cfg = Config()
        cfg.future.jury_mode = "enforce"
        args = self._parse("--jury-mode", "shadow")
        if getattr(args, "jury_mode", None):
            cfg.future.jury_mode = args.jury_mode
            cfg.future.enable_shadow_jury = False
        assert effective_jury_mode(cfg.future) == "shadow"

    def test_apply_override_jury_mode_beats_legacy_shadow_flag(self):
        """An explicit --jury-mode wins over a legacy enable_shadow_jury."""
        from capybase.config import effective_jury_mode
        cfg = Config()
        cfg.future.enable_shadow_jury = True  # legacy → would be shadow
        args = self._parse("--jury-mode", "off")
        if getattr(args, "jury_mode", None):
            cfg.future.jury_mode = args.jury_mode
            cfg.future.enable_shadow_jury = False
        assert effective_jury_mode(cfg.future) == "off"


# ---------------------------------------------------------------------------
# human_review blocks-merge contract (default True)
# ---------------------------------------------------------------------------


class TestHumanReviewBlocksContract:
    def test_default_human_review_blocks_is_true(self):
        """The default contract: human_review BLOCKS the merge (the brief)."""
        f = FutureConfig()
        assert f.jury_human_review_blocks is True

    def test_apply_jury_enforcement_returns_none_on_human_review_when_blocking(self):
        """When jury_human_review_blocks=True, _apply_jury_enforcement returns
        None (block the merge) on a human_review outcome. Verified via the
        orchestrator method directly (stubbed journal)."""
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from tests.conftest import git
        from capybase.orchestrator import Orchestrator
        from capybase.resolution_engine import ResolutionEngine
        from capybase.jury_enforce import HumanReviewOutcome
        from capybase.shadow_jury import RoutingDecision

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            git(repo, "init", "-q", "-b", "main")
            (repo / "x.txt").write_text("init")
            git(repo, "add", "x.txt"); git(repo, "commit", "-q", "-m", "init")
            cfg = Config()
            cfg.future.jury_human_review_blocks = True
            e = ResolutionEngine(cfg.model)
            o = Orchestrator(cfg, repo=str(repo), resolution_engine=e,
                             out=lambda *_a, **_k: None)
            o.step = 0
            hr = HumanReviewOutcome(
                route="human_review", claim_id="LC1.1", lineage_id="LC1",
                effective_verdict="UNVERIFIABLE_INHERITED_CLAIM",
                reason="test human review",
                chair_decision=RoutingDecision(
                    claim_id="LC1.1", route="human_review", reason="test"),
            )
            result = o._apply_jury_enforcement(
                "m.py", "frozen_buffer", [], [], "python", "orig", [hr])
            assert result is None, (
                "human_review must BLOCK the merge (return None) when "
                "jury_human_review_blocks=True")

    def test_apply_jury_enforcement_advisory_when_not_blocking(self):
        """When jury_human_review_blocks=False, human_review is advisory: the
        merge proceeds (returns the buffer) and only the bundle is written."""
        import tempfile
        from pathlib import Path
        from tests.conftest import git
        from capybase.orchestrator import Orchestrator
        from capybase.resolution_engine import ResolutionEngine
        from capybase.jury_enforce import HumanReviewOutcome
        from capybase.shadow_jury import RoutingDecision

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            git(repo, "init", "-q", "-b", "main")
            (repo / "x.txt").write_text("init")
            git(repo, "add", "x.txt"); git(repo, "commit", "-q", "-m", "init")
            cfg = Config()
            cfg.future.jury_human_review_blocks = False  # advisory mode
            e = ResolutionEngine(cfg.model)
            o = Orchestrator(cfg, repo=str(repo), resolution_engine=e,
                             out=lambda *_a, **_k: None)
            o.step = 0
            hr = HumanReviewOutcome(
                route="human_review", claim_id="LC1.1", lineage_id="LC1",
                effective_verdict="UNVERIFIABLE_INHERITED_CLAIM",
                reason="test human review",
                chair_decision=RoutingDecision(
                    claim_id="LC1.1", route="human_review", reason="test"),
            )
            result = o._apply_jury_enforcement(
                "m.py", "frozen_buffer", [], [], "python", "orig", [hr])
            assert result == "frozen_buffer", (
                "advisory human_review must let the merge proceed (return buffer)")
