"""R2 (sprint-22): exact-duplicate `use` statement dedup (rust).

Union-merged re-export lists can carry the same use line twice; rustc
rejects each duplicate with "defined multiple times" (sea-orm-0021: 17
errors). The sweep removes later exact duplicates, scope-aware, and
rides the coherence_repair_applied feature so R1's propagation and
fail-closed guard cover the deduped text.
"""

from capybase.config import ValidationConfig
from capybase.verification import (
    VerificationEngine,
    _dedup_rust_use_statements,
)


def test_exact_duplicates_removed():
    text = (
        "use sea_orm::entity::prelude::*;\n"
        "use crate::entity::EntityName;\n"
        "use crate::entity::EntityName;\n"
        "use crate::entity::EntityTrait;\n"
        "\n"
        "pub struct Foo;\n"
    )
    out = _dedup_rust_use_statements(text)
    assert out is not None
    assert out.count("use crate::entity::EntityName;") == 1
    # order preserved, other lines untouched
    assert out.splitlines()[0] == "use sea_orm::entity::prelude::*;"
    assert out.splitlines()[-1] == "pub struct Foo;"


def test_scope_aware_function_body_use_kept():
    text = (
        "use a::b;\n"
        "fn f() {\n"
        "    use a::b;\n"
        "}\n"
    )
    out = _dedup_rust_use_statements(text)
    assert out is None  # different scopes — nothing to remove


def test_no_duplicates_returns_none():
    assert _dedup_rust_use_statements("use a::b;\nfn f() {}\n") is None
    assert _dedup_rust_use_statements("fn f() {}\n") is None


def test_dedup_marks_repair_and_propagates(tmp_path):
    """verify_file: a spliced buffer with duplicate use lines is deduped,
    the repair is flagged, and the deduped text comes back on
    resolved_text (R1 propagation) for the caller to write."""
    original = (
        "mod m {\n"
        "<<<<<<< H\n"
        "    use crate::entity::EntityName;\n"
        "=======\n"
        "    use crate::entity::EntityName;\n"
        ">>>>>>> b\n"
        "}\n"
    )
    lines = original.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("<<<<<<<"))
    end = next(i for i, ln in enumerate(lines) if ln.startswith(">>>>>>>"))
    resolved = (
        "    use crate::entity::EntityName;\n"
        "    use crate::entity::EntityName;\n"
    )
    eng = VerificationEngine.default(ValidationConfig())
    res = eng.verify_file(
        "src/prelude.rs", "rust", original, [((start, end), resolved)],
        repo_root=str(tmp_path),
    )
    assert res.features.get("coherence_repair_applied"), res.features
    assert res.resolved_text is not None
    assert _dedup_rust_use_statements(res.resolved_text) is None  # deduped
    # the duplicate is gone from the returned text
    assert res.resolved_text.count("use crate::entity::EntityName;") == 1
