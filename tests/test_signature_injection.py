"""S28-54: sibling-signature injection — the weave-band repair lever.

The duckdb weave band fails 1-2 compile errors from passing; 5/8 of the
error identifiers have declaration-shaped lines findable in a conflict
side (S28-54 census, exact vectors below taken verbatim from the s28
harvest + testdata sides). The injection is a candidate generator: the
guards decline (never guess), the whole-file gate decides.
"""

from __future__ import annotations

from capybase.conflict_model import VerificationFailure
from capybase.orchestrator import _try_signature_injection
from capybase.signature_repair import (
    extract_signature_gaps,
    find_declaration_line,
    has_signature_gap,
    inject_declaration,
)

# --- live harvest vectors (S28-54) -----------------------------------------

_0125 = ("../../../src/main/parse_iterator.cpp:155:17: error: "
         "\u2018parser_cache\u2019 was not declared in this scope; "
         "did you mean \u2018ParserCache\u2019?")
_0126 = ("../../../src/parser/parser.cpp:375:9: error: "
         "\u2018cache\u2019 was not declared in this scope")
_0129 = ("error: \u2018class duckdb::TokenizerBehavior\u2019 has no member "
         "named \u2018keyword_helper\u2019")

# Real side lines (duckdb-history-0125..0128 testdata sides, verbatim).
_0125_CURRENT = (
    "\tauto &parser_cache = DatabaseInstance::GetDatabase(context).GetParserCache();\n"
    "\tparser_cache.GetTokenizer().TokenizeInput(behavior);\n"
)
_0126_CURRENT = (
    "\tauto &cache = GetCache();\n"
    "\tcache.GetMatcher();\n"
    "\tParserCache cache;\n"
)
_0126_REPLAYED = (
    "\tauto &cache = GetCache();\n"
    "\tcompiled_grammar = cache.GetMatcher();\n"
)


def test_extract_undeclared_identifiers_with_lines():
    gaps = extract_signature_gaps([_0125, _0126, _0129])
    assert ("parser_cache", 155) in gaps
    assert ("cache", 375) in gaps
    assert ("keyword_helper", None) in gaps
    assert has_signature_gap([_0126])
    assert not has_signature_gap(["no matching function for call to f()"])


def test_declaration_shape_accepts_real_side_lines_and_rejects_usages():
    # census hits — all findable
    assert find_declaration_line([_0125_CURRENT], "parser_cache") == (
        "auto &parser_cache = DatabaseInstance::GetDatabase(context)"
        ".GetParserCache();")
    # self-contained-first ordering (S28-82): `ParserCache cache;` (no
    # call) precedes the context-dependent `auto &cache = GetCache();`
    assert find_declaration_line(
        [_0126_CURRENT, _0126_REPLAYED], "cache") == "ParserCache cache;"
    assert find_declaration_line(
        ["\tParserCache cache;\n\tcache.GetTokenizer().TokenizeInput(b);"],
        "cache") == "ParserCache cache;"
    assert find_declaration_line(
        ["\tTokenizer tokenizer(behavior, compiled_grammar.GetKeywordHelper());"],
        "tokenizer") == ("Tokenizer tokenizer(behavior, "
                         "compiled_grammar.GetKeywordHelper());")
    # census misses — usage-only shapes must NOT count as declarations
    assert find_declaration_line(
        ["\tcompiled_grammar = cache.GetMatcher();"], "cache") is None
    assert find_declaration_line(
        ["\tparser_cache.GetTokenizer().TokenizeInput(behavior);"],
        "parser_cache") is None
    assert find_declaration_line(
        ["\tstate = tokenizer.keyword_helper.IsKeyword(w) ? a : b;"],
        "keyword_helper") is None
    # absent from both sides -> decline (the license's direction 2)
    assert find_declaration_line([_0126_REPLAYED], "tokenizer") is None


def test_injection_inserts_before_reported_usage_reindented():
    buffer = (
        "void step() {\n"
        "\tif (ready) {\n"
        "\t\tconsume(cache);\n"          # line 3 — the reported usage
        "\t}\n"
        "}\n"
    )
    out = inject_declaration(buffer, "cache", 3, "ParserCache cache;")
    assert out is not None
    lines = out.splitlines()
    assert lines[2] == "\t\tParserCache cache;"     # re-indented to usage
    assert lines[3] == "\t\tconsume(cache);"
    assert lines[2].endswith(";") and "ParserCache" in lines[2]


def test_injection_declines_when_identifier_already_in_resolution():
    # the license's direction 1: present-in-resolution -> NO injection
    buffer = (
        "void step() {\n"
        "\tParserCache cache;\n"
        "\tconsume(cache);\n"
        "}\n"
    )
    assert inject_declaration(buffer, "cache", 3, "ParserCache cache;") is None


def test_injection_declines_when_usage_line_unlocatable():
    assert inject_declaration(
        "void step() {\n\tconsume(other);\n}\n",
        "cache", 2, "ParserCache cache;") is None
    assert inject_declaration(
        "void step() {\n\tconsume(cache);\n}\n",
        "cache", 99, "ParserCache cache;") is not None  # fallback finds it
    assert inject_declaration(
        "void step() {\n\tconsume(other);\n}\n",
        "cache", None, "ParserCache cache;") is None


def _fail(msg: str) -> VerificationFailure:
    return VerificationFailure(validator="syntax", severity="error",
                               message=msg)


def test_orchestrator_helper_end_to_end_and_decline_paths():
    buffer = (
        "void step() {\n"
        "\tif (ready) {\n"
        "\t\tconsume(cache);\n"
        "\t}\n"
        "}\n"
    )
    # happy: the 0126-shaped failure + a side carrying the declaration —
    # the self-contained variant is tried first (S28-82)
    out, applied = _try_signature_injection(
        buffer, [_fail(_0126)], [_0126_CURRENT, _0126_REPLAYED])
    assert out is not None and "ParserCache cache;" in out
    assert applied == [("cache", "ParserCache cache;")]
    # decline: identifier absent from both sides (direction 2)
    out2, applied2 = _try_signature_injection(
        buffer, [_fail(_0126)], ["\tint unrelated;\n"])
    assert out2 is None and applied2 == []
    # decline: identifier already declared NEAR the usage (the scope-local
    # present-in-resolution guard; the reported line matches this buffer)
    declared = buffer.replace("consume(cache);", "ParserCache cache;\n\tconsume(cache);")
    declared_fail = _fail(
        "parser.cpp:4:9: error: \u2018cache\u2019 was not declared in this scope")
    out3, _ = _try_signature_injection(
        declared, [declared_fail], [_0126_CURRENT])
    assert out3 is None
    # decline: failures carry no signature-gap shape at all
    out4, _ = _try_signature_injection(
        buffer, [_fail("error: expected ',' or '...' before '}' token")],
        [_0126_CURRENT])
    assert out4 is None


def test_gate_declined_pairs_are_memoized():
    """S28-74: a (identifier, declaration) pair the whole-file gate already
    declined must not re-fire on later loop iterations — each unmemoized
    retry re-derives the identical decline and burns a full build probe
    (observed on duckdb-0125/0126/0127)."""
    buffer = (
        "void step() {\n"
        "\tif (ready) {\n"
        "\t\tconsume(cache);\n"
        "\t}\n"
        "}\n"
    )
    out, applied = _try_signature_injection(
        buffer, [_fail(_0126)], [_0126_CURRENT])
    assert out is not None and applied
    # the same call with BOTH variants memoized declines without
    # re-injecting; declining the self-contained variant falls through to
    # the context-dependent one (S28-82 ordering: self-contained first)
    out2, applied2 = _try_signature_injection(
        buffer, [_fail(_0126)], [_0126_CURRENT],
        declined={("cache", "ParserCache cache;")})
    assert out2 is not None and applied2 == [
        ("cache", "auto &cache = GetCache();")]
    out3, applied3 = _try_signature_injection(
        buffer, [_fail(_0126)], [_0126_CURRENT],
        declined={("cache", "auto &cache = GetCache();"),
                  ("cache", "ParserCache cache;")})
    assert out3 is None and applied3 == []


def test_declaration_variants_self_contained_first():
    """S28-82: find_declaration_lines returns ALL variants, self-contained
    (no call) first — 0126's sides carry both `ParserCache cache;` and
    `auto &cache = GetCache();`, and only the self-contained one can
    compile at an arbitrary usage site."""
    from capybase.signature_repair import find_declaration_lines
    variants = find_declaration_lines(
        [_0126_CURRENT, _0126_REPLAYED], "cache")
    assert variants == [
        "ParserCache cache;",
        "auto &cache = GetCache();",
    ]
    # dedup: the shared declaration appears once even though both sides
    # carry it
    assert len([v for v in variants if "GetCache" in v]) == 1


def test_declined_first_variant_falls_through_to_the_next():
    """The gate declined the self-contained variant in an earlier loop
    iteration — the next attempt must try the SECOND variant rather than
    skipping the identifier entirely."""
    buffer = (
        "void step() {\n"
        "\tif (ready) {\n"
        "\t\tconsume(cache);\n"
        "\t}\n"
        "}\n"
    )
    declined = {("cache", "ParserCache cache;")}
    out, applied = _try_signature_injection(
        buffer, [_fail(_0126)], [_0126_CURRENT], declined=declined)
    assert out is not None
    # ...but this side has no second variant, so a declined single-variant
    # identifier still declines:
    out2, applied2 = _try_signature_injection(
        buffer, [_fail(_0126)], ["\tParserCache cache;\n"], declined=declined)
    assert out2 is None and applied2 == []


def test_declaration_guard_is_scope_local_not_file_global():
    """S28-82: a declaration in an UNRELATED earlier function must not
    decline the injection — gcc's not-declared error is scope-specific
    (duckdb-0126's blocker: `auto &cache = GetCache();` at ~line 30
    shadowed the guard while the usage at 375 sat in a different scope).
    Only a declaration NEAR the usage (the same local scope, plausibly)
    declines."""
    far_decl = "\n".join(
        ["auto &cache = GetCache();"] + ["int pad%d = 0;" % i for i in range(60)]
    )
    buffer = (
        "void other() {\n"
        + far_decl
        + "\n}\n\n"
        + "void step() {\n"
        + "\tif (ready) {\n"
        + "\t\tconsume(cache);\n"
        + "\t}\n"
        + "}\n"
    )
    out = inject_declaration(buffer, "cache", None, "ParserCache cache;")
    assert out is not None, (
        "a declaration 60+ lines away is in a different scope — the "
        "injection must be allowed")
    # a declaration immediately before the usage still declines
    near = (
        "void step() {\n"
        "\tParserCache cache;\n"
        "\tconsume(cache);\n"
        "}\n"
    )
    assert inject_declaration(near, "cache", 3, "ParserCache cache;") is None
