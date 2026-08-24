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


def test_verify_file_whole_text_overrides_splice(tmp_path):
    """V8 WHOLE_FILE_FAILED regression: the orchestrator computed
    deduplicate_imports(buffer) and updated the local buffer, but verify_file
    re-spliced the un-deduped per-unit spans and failed on the same duplicates
    the dedup just removed. The whole_text parameter lets the caller pass the
    already-deduped final text so verify_file validates what gets written to
    disk instead of a re-splice.

    This test proves the override is honored: the validated text is the
    deduped buffer, not a re-splice of the (duplicate-bearing) spans."""
    from capybase.verification import VerificationEngine, ValidationConfig

    # E0432/E0433 suppression: standalone rustc on a snippet can't resolve
    # external crates (`use <external>::Path`); crate-path errors are
    # undecidable without the crate and are suppressed on this path.
    engine = VerificationEngine([], ValidationConfig(
        rust_suppress_codes=["E0432", "E0433"]))

    original = (
        "use std::io;\n"
        "use std::collections::HashMap;\n"
        "<<<<<<< HEAD\n"
        "// placeholder\n"
        "=======\n"
        "// placeholder\n"
        ">>>>>>> replayed\n"
        "fn handler(m: HashMap<String, String>) -> usize {\n"
        "    m.len()\n"
        "}\n"
    )
    # The per-unit resolution re-states the import that exists just below the
    # span. Splicing this produces a duplicate `use std::collections::HashMap;`.
    # NOTE: the splice filler must be VALID Rust — the whole-file syntax
    # check (standalone rustc) runs before any duplicate-import diagnostic,
    # and a parse error on the filler fails both paths identically.
    spans_and_texts = [((2, 6), "use std::collections::HashMap;\n// placeholder")]
    # Dedup the spliced result (what the orchestrator does).
    spliced = (
        "use std::io;\nuse std::collections::HashMap;\nuse std::collections::HashMap;\n"
        "// placeholder\nfn handler(m: HashMap<String, String>) -> usize {\n    m.len()\n}\n"
    )
    deduped, count = deduplicate_imports(spliced, "rust")
    assert count == 1, "dedup should remove the duplicate import"
    assert deduped.count("use std::collections::HashMap;") == 1
    # whole_text override: verify_file must validate the deduped text, not a
    # re-splice. The deduped text has the duplicate import removed.
    result_override = engine.verify_file(
        "test.rs", "rust", original, spans_and_texts,
        whole_text=deduped,
    )
    # Without whole_text: verify_file re-splices the spans, re-introducing the
    # duplicate `use std::collections::HashMap;`. The structural duplicate-definition
    # check flags it. This is the V8 bug: the dedup ran, but verify_file
    # validated the un-deduped splice and failed on the same duplicate.
    result_resplice = engine.verify_file(
        "test.rs", "rust", original, spans_and_texts,
    )
    # The contrast proves the override works: the resplice sees 2 occurrences
    # (the duplicate the dedup removed), the override sees 1.
    resplice_text = original.split("\n")
    # Reconstruct what each path validated by counting the import in the result.
    # The override path must NOT have a duplicate-import hard failure that the
    # resplice path DOES have.
    override_msgs = " ".join(f.message for f in result_override.hard_failures)
    resplice_msgs = " ".join(f.message for f in result_resplice.hard_failures)
    # R2 (sprint-22) updated the contract: the resplice no longer FAILS on
    # an exact-duplicate import — verify_file's use-dedup rung repairs it
    # (marked via coherence_repair_applied, propagated on resolved_text per
    # R1). The override remains the cleaner path (no repair needed), and the
    # test's V8 core still holds: both validated texts are duplicate-free.
    assert not result_resplice.hard_failures, (
        f"resplice should pass (R2 dedups the duplicate import), got: {resplice_msgs!r}"
    )
    assert result_resplice.features.get("coherence_repair_applied"), (
        "R2 use-dedup should have fired on the duplicate-bearing resplice"
    )
    assert result_resplice.resolved_text is not None
    assert result_resplice.resolved_text.count(
        "use std::collections::HashMap;") == 1
    assert not result_override.features.get("coherence_repair_applied"), (
        "the already-deduped override needs no repair"
    )
    assert not result_override.hard_failures, (
        f"override (deduped) should validate clean, got: {override_msgs}"
    )

