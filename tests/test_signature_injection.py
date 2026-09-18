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
    # first declaration in search order wins: _0126_CURRENT opens with the
    # shared `auto &cache = GetCache();` (present on BOTH sides), and only
    # then `ParserCache cache;` — both are legal injection candidates.
    assert find_declaration_line(
        [_0126_CURRENT, _0126_REPLAYED], "cache") == "auto &cache = GetCache();"
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
    # happy: the 0126-shaped failure + a side carrying the declaration
    out = _try_signature_injection(
        buffer, [_fail(_0126)], [_0126_CURRENT, _0126_REPLAYED])
    assert out is not None and "auto &cache = GetCache();" in out
    # decline: identifier absent from both sides (direction 2)
    assert _try_signature_injection(
        buffer, [_fail(_0126)], ["\tint unrelated;\n"]) is None
    # decline: identifier already declared in the buffer (direction 1)
    declared = buffer.replace("consume(cache);", "ParserCache cache;\n\tconsume(cache);")
    assert _try_signature_injection(
        declared, [_fail(_0126)], [_0126_CURRENT]) is None
    # decline: failures carry no signature-gap shape at all
    assert _try_signature_injection(
        buffer, [_fail("error: expected ',' or '...' before '}' token")],
        [_0126_CURRENT]) is None
