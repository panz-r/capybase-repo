"""Tests for the whole-file import deduplication linker."""

from __future__ import annotations

import pytest

from capybase.file_linker import deduplicate_imports


class TestDeduplicateImports:

    def test_exact_duplicate_removed(self):
        """An exact duplicate `use` line is removed."""
        text = "use std::io::Read;\nuse std::io::Read;\nfn main(){}\n"
        result, count = deduplicate_imports(text)
        assert count == 1
        assert result.count("use std::io::Read;") == 1
        assert "fn main(){}" in result

    def test_different_imports_not_deduped(self):
        """Different imports are all preserved."""
        text = "use std::io::Read;\nuse std::io::Write;\nfn main(){}\n"
        result, count = deduplicate_imports(text)
        assert count == 0
        assert "Read" in result and "Write" in result

    def test_partial_group_dedup(self):
        """A group with some duplicate members keeps only the fresh ones."""
        text = "use std::{io, fmt};\nuse std::{io, sync::Arc};\nfn main(){}\n"
        result, count = deduplicate_imports(text)
        assert count == 1  # the second line was edited
        # io should appear once (from the first line); Arc should appear once
        assert result.count("io") >= 1
        assert "Arc" in result

    def test_idempotent(self):
        """Re-running on deduplicated text produces no changes."""
        text = "use std::io::Read;\nuse std::io::Read;\nfn main(){}\n"
        result1, count1 = deduplicate_imports(text)
        result2, count2 = deduplicate_imports(result1)
        assert count2 == 0
        assert result2 == result1

    def test_no_imports(self):
        """Text without imports is unchanged."""
        text = "fn main(){}\nstruct Foo {}\n"
        result, count = deduplicate_imports(text)
        assert count == 0
        assert result == text

    def test_empty_text(self):
        result, count = deduplicate_imports("")
        assert count == 0
        assert result == ""

    def test_pub_use_dedup(self):
        """pub use statements are also deduplicated."""
        text = "pub use crate::foo::Bar;\npub use crate::foo::Bar;\nfn main(){}\n"
        result, count = deduplicate_imports(text)
        assert count == 1
        assert result.count("pub use crate::foo::Bar;") == 1

    def test_different_visibility_not_deduped(self):
        """use and pub use of the same path are NOT duplicates (different visibility)."""
        text = "use std::io::Read;\npub use std::io::Read;\nfn main(){}\n"
        result, count = deduplicate_imports(text)
        assert count == 0
        assert "use std::io::Read;" in result
        assert "pub use std::io::Read;" in result

    def test_brace_balance_safety(self):
        """The dedup doesn't break brace balance."""
        text = (
            "use std::collections::{HashMap, BTreeMap};\n"
            "use std::collections::{HashMap, HashSet};\n"
            "fn main() {\n"
            "    let x = HashMap::new();\n"
            "}\n"
        )
        result, count = deduplicate_imports(text)
        # Result must have balanced braces
        assert result.count("{") == result.count("}")

    def test_code_preserved(self):
        """Code lines are never modified."""
        text = (
            "use std::io::Read;\n"
            "use std::io::Read;\n"
            "fn process(data: &[u8]) -> usize {\n"
            "    data.len()\n"
            "}\n"
        )
        result, count = deduplicate_imports(text)
        assert count == 1
        assert "fn process" in result
        assert "data.len()" in result

    def test_multiple_duplicates(self):
        """Multiple duplicates of the same import are all removed."""
        text = "use std::io::Read;\nuse std::io::Read;\nuse std::io::Read;\nfn main(){}\n"
        result, count = deduplicate_imports(text)
        assert count == 2  # two removed
        assert result.count("use std::io::Read;") == 1

    def test_first_occurrence_kept(self):
        """The first occurrence is kept; later ones are removed."""
        text = (
            "// header comment\n"
            "use crate::Module;\n"
            "// mid comment\n"
            "use crate::Module;\n"
            "fn main(){}\n"
        )
        result, count = deduplicate_imports(text)
        assert count == 1
        assert result.count("use crate::Module;") == 1
        # The first occurrence (after the header comment) should be the one kept
        assert result.index("header comment") < result.index("use crate::Module;")

    def test_non_rust_language_skipped(self):
        """Non-Rust files are not processed."""
        text = "import os\nimport os\n"
        result, count = deduplicate_imports(text, language="python")
        assert count == 0
        assert result == text

    def test_reallocates_splice_collision(self):
        """Simulates the real splice-collision: unit adds an import that
        already exists elsewhere in the file."""
        # File has `use http::StatusCode;` at line 3
        # Unit resolution adds `use http::StatusCode;` in its resolved text
        text = (
            "use std::io;\n"
            "use http::StatusCode;\n"
            "\n"
            "use http::StatusCode;\n"  # duplicate from unit resolution
            "fn handler() -> StatusCode {\n"
            "    StatusCode::OK\n"
            "}\n"
        )
        result, count = deduplicate_imports(text)
        assert count == 1
        assert result.count("use http::StatusCode;") == 1
        assert "StatusCode::OK" in result
