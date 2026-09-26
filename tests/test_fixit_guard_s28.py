"""S28-279/281 guard tests: the gcc fix-it applier's unescape + error
monotonicity, and the coherence-note channel (own validator class, excluded
from the CEGIS failure signature)."""

from capybase.orchestrator import (
    _count_gcc_errors,
    _hard_failure_signature,
    _unescape_fixit_text,
)
from capybase.verification import VerificationFailure


def test_unescape_fixit_text_newlines():
    # gcc's parseable-fixit text field escapes newlines as literal \n —
    # php-0116's include block arrived glued into physical line 1.
    raw = "#include <stdlib.h>\\n#include <stddef.h>\\n"
    out = _unescape_fixit_text(raw)
    assert "\n" in out
    assert "\\n" not in out
    assert out == "#include <stdlib.h>\n#include <stddef.h>\n"


def test_unescape_fixit_text_other_escapes_and_passthrough():
    assert _unescape_fixit_text("a\\tb") == "a\tb"
    assert _unescape_fixit_text('q\\"x') == 'q"x'
    assert _unescape_fixit_text("a\\\\b") == "a\\b"
    # a trailing lone backslash passes through untouched
    assert _unescape_fixit_text("ok\\") == "ok\\"
    # escape-free text is returned unchanged (fast path)
    assert _unescape_fixit_text("#endif  // plain") == "#endif  // plain"


def test_count_gcc_errors():
    stderr = (
        "config.c:375:9: error: 'cache' not declared\n"
        " ~~^~~\n"
        "make[1]: *** [Makefile:220: config.o] Error 1\n"
        "config.c:380:1: error: expected ';' before '}'\n"
    )
    # only real gcc diagnostics count; make driver lines carry "Error" but
    # not the lowercase "error:"... the driver line has "Error 1" which
    # lowercases to "error 1" — no colon, so it does not match "error:".
    assert _count_gcc_errors(stderr) == 2


def test_signature_excludes_coherence_validator():
    """S28-281: the coherence-unverified note must not flatten the signature —
    two candidates with different real errors normalize differently even when
    both carry the note; and a candidate whose only 'difference' is the note
    must not read as progress."""
    real_a = VerificationFailure(
        validator="syntax", severity="error", message="a.c:1: error: x")
    real_b = VerificationFailure(
        validator="syntax", severity="error", message="a.c:2: error: y")
    note = VerificationFailure(
        validator="coherence", severity="error",
        message="coherence repair applied without compiler verification",
        detail={"coherence_repair_unverified": True})

    sig_a = _hard_failure_signature([real_a, note])
    sig_a_no_note = _hard_failure_signature([real_a])
    sig_b = _hard_failure_signature([real_b, note])
    # the note is invisible to the signature
    assert sig_a == sig_a_no_note
    # different real errors still register as progress
    assert sig_a != sig_b
