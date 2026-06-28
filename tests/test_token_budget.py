"""Tests for the live TokenBudget + estimate_tokens helper.

TokenBudget is the input-token budget carrier for the resolve prompt. When
enabled (total > 0), the prompt builder trims augmentation sections to fit;
when disabled (total == 0, the default), enforcement is a no-op (current
behavior). estimate_tokens is the ~4-chars/token heuristic shared by the
prompt builder's fit logic.
"""

from __future__ import annotations

from capybase.conflict_model import TokenBudget, estimate_tokens
from capybase.config import ModelConfig


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------


def test_estimate_tokens_empty_is_zero():
    assert estimate_tokens("") == 0


def test_estimate_tokens_roughly_four_chars_per_token():
    assert estimate_tokens("a" * 100) == 25
    assert estimate_tokens("abcd") == 1


def test_estimate_tokens_minimum_one_for_nonempty():
    # Any non-empty text is at least 1 token (max(1, len//4)).
    assert estimate_tokens("a") == 1
    assert estimate_tokens("abc") == 1
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 1  # 5//4 == 1


# ---------------------------------------------------------------------------
# TokenBudget defaults + available
# ---------------------------------------------------------------------------


def test_default_budget_is_disabled():
    b = TokenBudget()
    assert b.total == 0
    assert not b.enabled
    assert b.available == 0  # disabled → 0 available → no enforcement


def test_enabled_budget_available_is_total_minus_reserve():
    b = TokenBudget(total=8192, reserved_for_completion=2048)
    assert b.enabled
    assert b.available == 6144


def test_available_never_negative():
    b = TokenBudget(total=500, reserved_for_completion=2000)
    assert b.available == 0  # clamped


# ---------------------------------------------------------------------------
# from_config
# ---------------------------------------------------------------------------


def test_from_config_disabled_when_context_window_zero():
    cfg = ModelConfig()  # default context_window = 0
    b = TokenBudget.from_config(cfg)
    assert not b.enabled
    assert b.available == 0


def test_from_config_reads_window_and_reserve():
    cfg = ModelConfig(context_window=32768, completion_reserve=4096)
    b = TokenBudget.from_config(cfg)
    assert b.enabled
    assert b.total == 32768
    assert b.reserved_for_completion == 4096
    assert b.available == 28672


def test_from_config_handles_missing_attrs_defensively():
    class Bare:
        pass
    # No context_window/completion_reserve attrs → disabled with sane reserve.
    b = TokenBudget.from_config(Bare())
    assert not b.enabled
    assert b.reserved_for_completion == 1024
