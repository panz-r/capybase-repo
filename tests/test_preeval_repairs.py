"""Sprint-22 pre-eval repairs — unused-function deletion + string-literal fix.

Item 1: jsonc-0016's root cause — a -Werror=unused-function dead-code
error whose function has no call sites; deterministic deletion.
Item 2: protobuf-0034's exposed defect — an unterminated quote literal;
conservative single-line terminator append.
"""

from __future__ import annotations

from capybase.orchestrator import _micro_delete_unused_function
from capybase.verification import _try_repair_string_literal


class TestUnusedFunctionDelete:
    DEAD = (
        "int used_fn(int x) { return x + 1; }\n"
        "\n"
        "static int json_parse_double(const char *s) {\n"
        "    return 0;\n"
        "}\n"
        "\n"
        "int main() { return used_fn(1); }\n"
    )

    def test_dead_function_deleted(self):
        out = _micro_delete_unused_function(self.DEAD, "json_parse_double", 3)
        assert out is not None
        assert "json_parse_double" not in out
        assert "used_fn" in out  # the live function survives
        assert "main" in out

    def test_referenced_function_not_deleted(self):
        # a call site elsewhere -> decline (may be used outside the region)
        live = self.DEAD.replace(
            "int main() { return used_fn(1); }",
            "int main() { return json_parse_double(\"1\"); }")
        out = _micro_delete_unused_function(live, "json_parse_double", 3)
        assert out is None

    def test_mention_in_comment_declines(self):
        commented = self.DEAD + "/* see json_parse_double */\n"
        out = _micro_delete_unused_function(commented, "json_parse_double", 3)
        assert out is None

    def test_no_block_found_declines(self):
        out = _micro_delete_unused_function(
            "int x = 1;\n", "nonexistent", 1)
        assert out is None


class TestStringLiteralRepair:
    def test_unterminated_char_literal_fixed(self):
        text = (
            "int f() {\n"
            "    char c = 'a;\n"  # missing closing '
            "    return 0;\n"
            "}\n"
        )
        out = _try_repair_string_literal(text)
        assert out is not None
        assert out.count("'") % 2 == 0  # parity restored
        assert "'a;'" in out  # the terminator appended after the content

    def test_unterminated_string_literal_fixed(self):
        text = 'int f() {\n    char *s = "hello;\n    return 0;\n}\n'
        out = _try_repair_string_literal(text)
        assert out is not None
        assert out.count('"') % 2 == 0  # parity restored
        assert '"hello;"' in out  # the terminator appended after the content

    def test_balanced_text_untouched(self):
        text = "int f() {\n    char c = 'a';\n    return 0;\n}\n"
        assert _try_repair_string_literal(text) is None

    def test_multiple_bad_lines_decline(self):
        text = "char a = 'x;\nchar b = 'y;\n"
        assert _try_repair_string_literal(text) is None

    def test_escaped_quotes_not_counted(self):
        # escaped double-quotes inside a string: parity must see them as
        # escaped (not unescaped openers); the line is balanced -> None
        text = 'int f() {\n    char *s = "say \\"hi\\"";\n    return 0;\n}\n'
        assert _try_repair_string_literal(text) is None
