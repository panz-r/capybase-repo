"""The non-code structural-gate exemption (sprint-17 WS1a).

Brace-balance (and py_compile) are code-file gates. Applied to prose —
markdown changelogs, lockfiles, LICENSE — they reject perfect merges: four
axum CHANGELOG.md merges at sim 1.000 were classified ORACLE_DIVERGENT
because a code fence or template placeholder had an unbalanced brace
(sprint-16 census). The single allowlist lives in verification.py and is
consumed by the live eval's post-hoc compiles check, the true-side
portfolio's brace sanity check, and the wholesale winner floor.
"""

from __future__ import annotations

from types import SimpleNamespace

from capybase.verification import structural_gate_applies


def test_code_files_get_structural_gates():
    assert structural_gate_applies("src/lib.rs") is True
    assert structural_gate_applies("src/x.cpp") is True
    assert structural_gate_applies("include/util.h") is True
    assert structural_gate_applies("app.py") is True
    assert structural_gate_applies("Dir/Sub/file.CC".lower()) is True


def test_prose_and_config_files_are_exempt():
    # The census cases: markdown changelogs whose perfect merges were
    # killed by brace balance.
    assert structural_gate_applies("axum/CHANGELOG.md") is False
    assert structural_gate_applies("README.md") is False
    assert structural_gate_applies("docs/method.md") is False
    # Lockfiles and manifests: TOML has no brace semantics.
    assert structural_gate_applies("Cargo.lock") is False
    assert structural_gate_applies("Cargo.toml") is False
    assert structural_gate_applies("package-lock.json") is False
    assert structural_gate_applies(".gitignore") is False


def test_extensionless_files_are_not_code():
    # LICENSE / README / CHANGELOG without an extension.
    assert structural_gate_applies("LICENSE") is False
    assert structural_gate_applies("CHANGELOG") is False


def test_empty_and_none_paths_are_exempt():
    assert structural_gate_applies(None) is False
    assert structural_gate_applies("") is False


def test_braces_balanced_still_applies_to_code():
    # The underlying check is unchanged for real code files.
    from capybase.verification import _braces_balanced
    assert _braces_balanced("fn f() { let x = 1; }\n", "rust") is True
    assert _braces_balanced("fn f() { let x = 1;\n", "rust") is False


# ---------------------------------------------------------------------------
# text_additive_union rule (sprint-17 WS2b)
# ---------------------------------------------------------------------------

def _tadd(path, base, cur, rep, hunk_cur=None, hunk_base=None, hunk_rep=None):
    """Call _try_text_additive_union with whole-file sides and (optionally)
    refined hunk sides — the resolver passes refined sides as current/base/
    replayed and reads whole files off the unit."""
    from types import SimpleNamespace
    from capybase.structural_resolver import _try_text_additive_union
    unit = SimpleNamespace(
        path=path,
        base=SimpleNamespace(text=base),
        current=SimpleNamespace(text=cur),
        replayed=SimpleNamespace(text=rep),
    )
    return _try_text_additive_union(
        unit, hunk_cur if hunk_cur is not None else cur,
        hunk_base if hunk_base is not None else base,
        hunk_rep if hunk_rep is not None else rep)


def test_text_union_append_shape():
    # The blessed text-combine shape: both sides append distinct bullets.
    got = _tadd("README.md", "- A\n", "- A\n- B\n", "- A\n- C\n")
    assert got == "- A\n- B\n- C\n"


def test_text_union_prepend_changelog_shape():
    # Each side prepends its entries above a shared tail (tokio CHANGELOG).
    base = "## Unreleased\n- old fix\n"
    cur = "## 1.51.1\n- cur fix\n\n## Unreleased\n- old fix\n"
    rep = "## 1.51.2\n- rep fix\n\n## Unreleased\n- old fix\n"
    got = _tadd("CHANGELOG.md", base, cur, rep)
    assert "- cur fix" in got and "- rep fix" in got
    assert got.index("- cur fix") < got.index("- rep fix")  # current first
    assert "## Unreleased" in got and "## 1.51.1" in got and "## 1.51.2" in got


def test_text_union_declines_code_files():
    got = _tadd("src/lib.rs", "fn a() {}\n", "fn a() {}\nfn b() {}\n",
                "fn a() {}\nfn c() {}\n")
    assert got is None


def test_text_union_declines_real_rewrites():
    # tokio-0105: current rewrote (+726/-370) — deletions over budget.
    base = "\n".join(f"line {i}" for i in range(100))
    cur = "\n".join(f"rewritten {i}" for i in range(50))  # ~50 del
    rep = base + "\nadded\n"
    assert _tadd("CHANGELOG.md", base, cur, rep) is None


def test_text_union_declines_when_nothing_added():
    base = "- A\n- B\n"
    cur = "- A\n"          # pure deletions on both sides
    rep = "- B\n"
    assert _tadd("NOTES.txt", base, cur, rep) is None


def test_text_union_uses_whole_file_sides_not_block_sides():
    # Live shape: the marker unit carries whole-file base but conflict-
    # block-only current/replayed. Diffing a block against the whole-file
    # base reads as a total rewrite (all deletions) and the rule declined
    # everything — the orchestrator stashes pristine merge-index texts in
    # structural_metadata["whole_file_sides"] for exactly this gate.
    base = "## Unreleased\n- old fix\n" + "filler\n" * 40
    cur_file = "## 1.1\n- cur fix\n\n## Unreleased\n- old fix\n" + "filler\n" * 40
    rep_file = "## 1.2\n- rep fix\n\n## Unreleased\n- old fix\n" + "filler\n" * 40
    # Block-only sides (what the extractor puts on the unit):
    block_cur, block_rep = "## 1.1\n- cur fix", "## 1.2\n- rep fix"
    unit_with_meta = SimpleNamespace(
        path="CHANGELOG.md",
        base=SimpleNamespace(text=base),
        current=SimpleNamespace(text=block_cur),
        replayed=SimpleNamespace(text=block_rep),
        structural_metadata={"whole_file_sides": {
            "base": base, "current": cur_file, "replayed": rep_file}},
    )
    from capybase.structural_resolver import _try_text_additive_union
    got = _try_text_additive_union(unit_with_meta, block_cur, base, block_rep)
    assert got is not None
    assert "- cur fix" in got and "- rep fix" in got

    # Without the metadata the same unit declines (block vs whole base =
    # apparent rewrite) — pinning WHY the metadata exists.
    unit_no_meta = SimpleNamespace(
        path="CHANGELOG.md",
        base=SimpleNamespace(text=base),
        current=SimpleNamespace(text=block_cur),
        replayed=SimpleNamespace(text=block_rep),
        structural_metadata={},
    )
    assert _try_text_additive_union(unit_no_meta, block_cur, base, block_rep) is None
