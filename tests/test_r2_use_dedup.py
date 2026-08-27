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


# ---------------------------------------------------------------------------
# P1c (sprint-24): canonical-form dedup — pub use, multi-line groups,
# reordered items, cfg-attribute context
# ---------------------------------------------------------------------------

def test_pub_use_exact_duplicate_removed():
    """The original sweep only matched ``use `` — pub re-exports (the
    sea-orm class) were invisible to it."""
    text = (
        "pub use serde_json::Value as Json;\n"
        "pub use chrono::NaiveDateTime as DateTime;\n"
        "pub use serde_json::Value as Json;\n"
    )
    out = _dedup_rust_use_statements(text)
    assert out is not None
    assert out.count("pub use serde_json::Value as Json;") == 1


def test_reordered_group_items_dedup():
    """{b, c} ≡ {c, b} — same bindings, different order. The later copy
    goes."""
    text = (
        "pub use crate::{Alpha, Beta};\n"
        "fn f() {}\n"
        "pub use crate::{ Beta, Alpha };\n"
    )
    out = _dedup_rust_use_statements(text)
    assert out is not None
    assert out.count("pub use crate::") == 1
    assert "fn f() {}" in out


def test_multiline_group_vs_singleline_dedup():
    """A multi-line grouped re-export ≡ its single-line form after
    whitespace collapse + item sort."""
    text = (
        "pub use crate::{\n"
        "    error::*, ActiveModelTrait, ColumnTrait,\n"
        "};\n"
        "fn f() {}\n"
        "pub use crate::{ ColumnTrait, error::*, ActiveModelTrait };\n"
    )
    out = _dedup_rust_use_statements(text)
    assert out is not None
    assert out.count("pub use crate::{") == 1
    assert "fn f() {}" in out


def test_cfg_gated_groups_do_not_collide():
    """The sea-orm-0021 oracle shape: two ``pub use crate::{...}`` groups
    distinguished ONLY by #[cfg(feature)] — different bindings, both must
    survive."""
    text = (
        "#[cfg(feature = \"macros\")]\n"
        "pub use crate::{\n"
        "    DeriveActiveModel, DeriveColumn,\n"
        "};\n"
        "\n"
        "pub use crate::{\n"
        "    error::*, ActiveModelTrait, ColumnTrait,\n"
        "};\n"
    )
    assert _dedup_rust_use_statements(text) is None


def test_same_cfg_same_items_dedup():
    """Two cfg-gated groups with IDENTICAL attributes and items — the later
    copy is redundant."""
    text = (
        "#[cfg(feature = \"with-json\")]\n"
        "pub use serde_json::Value as Json;\n"
        "\n"
        "#[cfg(feature = \"with-json\")]\n"
        "pub use serde_json::Value as Json;\n"
    )
    out = _dedup_rust_use_statements(text)
    assert out is not None
    assert out.count("pub use serde_json::Value as Json;") == 1
    assert out.count('#[cfg(feature = "with-json")]') == 1


def test_sea_orm_oracle_untouched():
    """The full sea-orm-0021 oracle prelude: no dedup (all groups are
    cfg-distinct or bind different names)."""
    text = (
        "pub use crate::{\n"
        "    error::*, ActiveModelBehavior, ActiveModelTrait, ColumnDef, ColumnTrait, ColumnType,\n"
        "    EntityName, EntityTrait, EnumIter, ForeignKeyAction, Iden, IdenStatic, Linked, ModelTrait,\n"
        "    PrimaryKeyToColumn, PrimaryKeyTrait, PrimaryKeyValue, QueryFilter, QueryResult, Related,\n"
        "    RelationDef, RelationTrait, Select, Value,\n"
        "};\n"
        "\n"
        "#[cfg(feature = \"macros\")]\n"
        "pub use crate::{\n"
        "    DeriveActiveModel, DeriveActiveModelBehavior, DeriveColumn, DeriveCustomColumn, DeriveEntity,\n"
        "    DeriveEntityModel, DeriveModel, DerivePrimaryKey, DeriveRelation,\n"
        "};\n"
        "\n"
        "#[cfg(feature = \"with-json\")]\n"
        "pub use serde_json::Value as Json;\n"
        "\n"
        "#[cfg(feature = \"with-chrono\")]\n"
        "pub use chrono::NaiveDateTime as DateTime;\n"
    )
    assert _dedup_rust_use_statements(text) is None


def test_union_merged_double_group_dedups():
    """The actual merge failure shape: a union splice emits the same
    cfg-distinct-free group twice with slightly different formatting."""
    text = (
        "pub use crate::{\n"
        "    error::*, ActiveModelTrait, ColumnTrait,\n"
        "};\n"
        "\n"
        "pub use crate::{ ColumnTrait, error::*, ActiveModelTrait };\n"
        "\n"
        "#[cfg(feature = \"with-json\")]\n"
        "pub use serde_json::Value as Json;\n"
    )
    out = _dedup_rust_use_statements(text)
    assert out is not None
    assert out.count("pub use crate::{") == 1
    assert out.count("Value as Json") == 1
