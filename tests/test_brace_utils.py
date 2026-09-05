"""Direct tests for brace_utils' shared primitives.

The naive `brackets_balanced` pre-filter had five verbatim copies across the
union/insertion primitives until EXTEND-94 lifted them; this pins the ONE
canonical implementation's contract (naive: string/comment-unaware, strict
nesting) so the consumers' shared dependency can't silently drift.
"""

from capybase.brace_utils import brackets_balanced


def test_brackets_balanced_accepts_balanced():
    assert brackets_balanced("") is True
    assert brackets_balanced("fn f() { vec![1, 2] }") is True
    assert brackets_balanced("(((())))") is True
    assert brackets_balanced("{a: [1, (2, 3)]}") is True


def test_brackets_balanced_rejects_imbalance():
    assert brackets_balanced("(]") is False
    assert brackets_balanced(")(") is False          # closer before opener
    assert brackets_balanced("fn f() {") is False    # unclosed
    assert brackets_balanced("}") is False


def test_brackets_balanced_is_naive_by_contract():
    """The documented pre-filter contract: brackets inside strings/comments
    COUNT (no masking) — the authoritative gates mask first. Pins that the
    lift didn't silently upgrade the semantics."""
    assert brackets_balanced('s = "}"') is False     # brace in a string counts
    assert brackets_balanced("// }") is False        # brace in a comment counts
