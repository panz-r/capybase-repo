"""A tracked catalog of synthetic C/C++ rebase conflicts.

Mirrors ``tests/rust_conflict_catalog.py``: each row is a single-hunk file
conflict carrying both a known-good merge (``expected_resolved``) and a
known-broken merge (``broken_resolved``). ``build_markers`` runs real
``git merge-file`` over the three sides to produce authentic conflict markers —
the exact text the parser and the agent see.

Why this is half the size of the rust catalog: C/C++ has no analog of rust's
borrow-check / lifetimes / traits / async-await / derive-macro / move-semantics
axes (those are ~15 of rust's 25 cases). This catalog focuses on the
language-agnostic resolver mechanisms the system already proves on rust/python
(``one_sided_change``, ``disjoint_edits``, ``zealous_merge``, ``token_disjoint``,
``insertion_union``) plus C/C++-specific reject shapes the compile floor catches
and two C++ cases exercising the g++ path.

Every case is a single-hunk conflict producing exactly one conflict block (the
``build_markers`` + ``_span`` contract). ``expected_resolved`` / ``broken_resolved``
are block-interior text — exactly what replaces the marker span, not the whole
file. Cases are standalone ``-fsyntax-only`` compilations (no build system, no
scaffold) so they need no ``Cargo.toml``/``lib.rs`` analog — the ``needs_cargo``
/ ``scaffold`` / ``edition`` / ``shadow_test`` fields of ``RustConflict`` are
omitted (they would always be False/empty).

Complemented later by repo-mined C/C++ conflicts (the
``test_realworld_conflicts.py`` / external-dataset path), as was done for rust.
"""

from __future__ import annotations

from dataclasses import dataclass

# ``build_markers`` is language-agnostic (pure ``git merge-file``), so reuse the
# rust catalog's tested implementation rather than duplicate it.
from tests.rust_conflict_catalog import build_markers  # noqa: F401


@dataclass(frozen=True)
class CConflict:
    """One synthetic C/C++ rebase conflict.

    ``expected_resolved`` / ``broken_resolved`` are block-interior text: exactly
    the lines that replace the single conflict marker span (NOT the whole file).
    The test splices them into ``build_markers`` output via the marker span.
    """

    id: str
    path: str
    language: str  # "c" or "cpp"
    base: str
    current: str
    replayed: str
    expected_resolved: str
    broken_resolved: str
    taxonomy: tuple[str, ...]
    notes: str = ""


def _conflict(
    *,
    id: str,
    path: str,
    language: str,
    base: str,
    current: str,
    replayed: str,
    expected_resolved: str,
    broken_resolved: str,
    taxonomy: tuple[str, ...],
    notes: str = "",
) -> CConflict:
    return CConflict(
        id=id, path=path, language=language, base=base, current=current,
        replayed=replayed, expected_resolved=expected_resolved,
        broken_resolved=broken_resolved, taxonomy=taxonomy, notes=notes,
    )


# ---------------------------------------------------------------------------
# The catalog. Each row exercises one cell of the taxonomy matrix.
# ---------------------------------------------------------------------------

CC_CONFLICTS: list[CConflict] = [
    # --- A. Proven language-agnostic mechanisms (gcc / c_std) ---

    _conflict(
        id="one_sided_body_change",
        path="src/compute.c",
        language="c",
        base=(
            "int compute(int n) {\n"
            "    return n + 8080;\n"
            "}\n"
        ),
        current=(
            "int compute(int n) {\n"
            "    return n + 9090;\n"
            "}\n"
        ),
        replayed=(
            "int compute(int n) {\n"
            "    return n + 7070;\n"
            "}\n"
        ),
        expected_resolved="    return n + 9090;",
        broken_resolved="    return n + 9090",  # missing semicolon
        taxonomy=("textual", "same-line", "one-sided-wins"),
        notes="Classic same-line value conflict (both sides bump the constant).",
    ),
    _conflict(
        id="disjoint_body_edits",
        path="src/compute.c",
        language="c",
        base=(
            "int compute(int n) {\n"
            "    int a = n + 1;\n"
            "    int b = n + 2;\n"
            "    return a + b;\n"
            "}\n"
        ),
        current=(
            "int compute(int n) {\n"
            "    int a = n + 10;\n"
            "    int b = n + 2;\n"
            "    return a + b;\n"
            "}\n"
        ),
        replayed=(
            "int compute(int n) {\n"
            "    int a = n + 1;\n"
            "    int b = n + 20;\n"
            "    return a + b;\n"
            "}\n"
        ),
        expected_resolved=(
            "    int a = n + 10;\n"
            "    int b = n + 20;\n"
            "    return a + b;"
        ),
        broken_resolved=(
            "    int a = n + 10\n"      # missing semicolon
            "    int b = n + 20\n"
            "    return a + b;"
        ),
        taxonomy=("textual", "disjoint-edits"),
        notes="Both sides edit non-overlapping lines of the same function body.",
    ),
    _conflict(
        id="adjacent_include",
        path="src/main.c",
        language="c",
        base=(
            "#include <stdio.h>\n"
            "\n"
            "int main(void) { return 0; }\n"
        ),
        current=(
            "#include <stdio.h>\n"
            "#include <string.h>\n"
            "\n"
            "int main(void) { return 0; }\n"
        ),
        replayed=(
            "#include <stdio.h>\n"
            "#include <stdlib.h>\n"
            "\n"
            "int main(void) { return 0; }\n"
        ),
        expected_resolved=(
            "#include <stdlib.h>\n"
            "#include <string.h>"
        ),
        broken_resolved=(
            "#include <stdlib.h>\n"
            "#include string.h>"   # missing opening '<' — a hard gcc error
        ),
        taxonomy=("textual", "insertion-union", "include"),
        notes="Both sides add a different #include at the same anchor.",
    ),
    _conflict(
        id="adjacent_define",
        path="src/config.c",
        language="c",
        base=(
            "#define BASE 1\n"
            "int main(void) { return BASE; }\n"
        ),
        current=(
            "#define BASE 1\n"
            "#define EXTRA_A 2\n"
            "int main(void) { return BASE + EXTRA_A; }\n"
        ),
        replayed=(
            "#define BASE 1\n"
            "#define EXTRA_B 3\n"
            "int main(void) { return BASE + EXTRA_B; }\n"
        ),
        expected_resolved=(
            "#define EXTRA_A 2\n"
            "#define EXTRA_B 3\n"
            "int main(void) { return BASE + EXTRA_A + EXTRA_B; }"
        ),
        broken_resolved=(
            "#define EXTRA_A 2\n"
            "#define EXTRA_B 3\n"
            "int main(void) { return BASE + EXTRA_A + EXTRA_B }"  # missing ';'
        ),
        taxonomy=("textual", "insertion-union", "macro"),
        notes="Both sides add a different #define macro at the same anchor.",
    ),
    _conflict(
        id="enum_variant_union",
        path="src/color.c",
        language="c",
        base=(
            "enum Color { RED, GREEN, BLUE };\n"
            "int main(void) { return RED; }\n"
        ),
        current=(
            "enum Color { RED, GREEN, BLUE, PURPLE };\n"
            "int main(void) { return RED; }\n"
        ),
        replayed=(
            "enum Color { RED, GREEN, BLUE, YELLOW };\n"
            "int main(void) { return RED; }\n"
        ),
        expected_resolved="enum Color { RED, GREEN, BLUE, PURPLE, YELLOW };",
        broken_resolved="enum Color { RED, GREEN, BLUE, PURPLE, YELLOW",  # no ';'
        taxonomy=("textual", "insertion-union", "enum"),
        notes="Both sides append a distinct enum variant to the same enum body.",
    ),
    _conflict(
        id="function_body_value_zealous",
        path="src/square.c",
        language="c",
        base=(
            "int square(int n) {\n"
            "    return n * 1;\n"
            "}\n"
        ),
        current=(
            "int square(int n) {\n"
            "    return n * n;\n"
            "}\n"
        ),
        replayed=(
            "int square(int n) {\n"
            "    return n + n;\n"
            "}\n"
        ),
        expected_resolved="    return n * n;",
        broken_resolved="    return n * n",  # missing semicolon
        taxonomy=("textual", "zealous-merge", "same-line"),
        notes="Both sides change the same return-expression line differently.",
    ),
    _conflict(
        id="token_disjoint_signature",
        path="src/process.c",
        language="c",
        base=(
            "int process(int n) {\n"
            "    return n;\n"
            "}\n"
        ),
        current=(
            "long process(int n) {\n"          # return type changed
            "    return n;\n"
            "}\n"
        ),
        replayed=(
            "int process(int n, int m) {\n"    # param added
            "    return n;\n"
            "}\n"
        ),
        expected_resolved="long process(int n, int m) {",
        broken_resolved="long process(int n int m) {",  # missing ',' — hard error
        taxonomy=("textual", "token-disjoint", "signature"),
        notes="Return-type change + param added at different token positions.",
    ),

    # --- A2. brace_union — single-line {...} additions (the gap-closing rule) ---

    _conflict(
        id="enum_single_line_union",
        path="src/color.c",
        language="c",
        base=(
            "enum Color { RED, GREEN, BLUE };\n"
            "int main(void) { return RED; }\n"
        ),
        current=(
            "enum Color { RED, GREEN, BLUE, PURPLE };\n"
            "int main(void) { return RED; }\n"
        ),
        replayed=(
            "enum Color { RED, GREEN, BLUE, YELLOW };\n"
            "int main(void) { return RED; }\n"
        ),
        expected_resolved="enum Color { RED, GREEN, BLUE, PURPLE, YELLOW };",
        broken_resolved="enum Color { RED, GREEN, BLUE, PURPLE, YELLOW",  # no ';'
        taxonomy=("textual", "brace-union", "enum", "single-line"),
        notes=(
            "Both sides append a distinct variant to a single-line enum. "
            "list_union matches only [...], dict_union needs key:value, "
            "insertion_union needs whole lines — this shape fell through all "
            "three until brace_union."
        ),
    ),

    # --- B. C-specific reject shapes the compile floor catches (gcc / c_std) ---

    _conflict(
        id="struct_field_broken",
        path="src/point.c",
        language="c",
        base=(
            "struct Point { int x; int y; };\n"
            "int main(void) { struct Point p = {1, 2}; return p.x; }\n"
        ),
        current=(
            "struct Point { int x; int y; int z; };\n"
            "int main(void) { struct Point p = {1, 2, 3}; return p.x; }\n"
        ),
        replayed=(
            "struct Point { int x; int y; int w; };\n"
            "int main(void) { struct Point p = {1, 2, 5}; return p.x; }\n"
        ),
        expected_resolved=(
            "struct Point { int x; int y; int z; int w; };\n"
            "int main(void) { struct Point p = {1, 2, 3, 5}; return p.x; }"
        ),
        broken_resolved=(
            "struct Point { int x; int y; z; int w; };\n"   # field with no type
            "int main(void) { struct Point p = {1, 2, 3, 5}; return p.x; }"
        ),
        taxonomy=("reject", "struct-field", "parse-error"),
        notes="Both sides add a struct field; broken merge has a malformed field.",
    ),
    _conflict(
        id="return_expr_stray_brace",
        path="src/compute2.c",
        language="c",
        base=(
            "int compute(int n) {\n"
            "    return n * 2;\n"
            "}\n"
        ),
        current=(
            "int compute(int n) {\n"
            "    return n * 3;\n"
            "}\n"
        ),
        replayed=(
            "int compute(int n) {\n"
            "    return n * 2 + 1;\n"
            "}\n"
        ),
        expected_resolved="    return n * 3;",
        broken_resolved=(
            "    return n * 3;\n"
            "}"   # stray extra closing brace → hard gcc error
        ),
        taxonomy=("reject", "stray-brace", "parse-error"),
        notes="Broken merge introduces a stray closing brace.",
    ),

    # --- C. C++ cases exercising the g++ / cpp_std path ---

    _conflict(
        id="cpp_class_method_disjoint",
        path="src/calc.cpp",
        language="cpp",
        base=(
            "class Calculator {\n"
            "public:\n"
            "    int base_val() { return 0; }\n"
            "};\n"
        ),
        current=(
            "class Calculator {\n"
            "public:\n"
            "    int base_val() { return 0; }\n"
            "    int add(int a, int b) { return a + b; }\n"
            "};\n"
        ),
        replayed=(
            "class Calculator {\n"
            "public:\n"
            "    int base_val() { return 0; }\n"
            "    int sub(int a, int b) { return a - b; }\n"
            "};\n"
        ),
        expected_resolved=(
            "    int sub(int a, int b) { return a - b; }\n"
            "    int add(int a, int b) { return a + b; }"
        ),
        broken_resolved=(
            "    int sub(int a, int b) { return a - b;\n"   # missing '}'
            "    int add(int a, int b) { return a + b; }"
        ),
        taxonomy=("semantic", "entity-disjoint", "class-method", "cpp"),
        notes="Both sides add a distinct method to the same class (g++ path).",
    ),
    _conflict(
        id="cpp_template_body",
        path="src/identity.cpp",
        language="cpp",
        base=(
            "template <typename T>\n"
            "T identity(T x) {\n"
            "    return x;\n"
            "}\n"
        ),
        current=(
            "template <typename T>\n"
            "T identity(T x) {\n"
            "    T y = x;\n"
            "    return y;\n"
            "}\n"
        ),
        replayed=(
            "template <typename T>\n"
            "T identity(T x) {\n"
            "    return x + T();\n"
            "}\n"
        ),
        expected_resolved=(
            "    T y = x;\n"
            "    return x + T();"
        ),
        broken_resolved=(
            "    T y = x;\n"
            "    return x + T()"   # missing ';'
        ),
        taxonomy=("textual", "disjoint-edits", "template", "cpp"),
        notes="Both sides edit a template function body (g++ path).",
    ),
]


CONFLICT_BY_ID: dict[str, CConflict] = {c.id: c for c in CC_CONFLICTS}


def c_conflicts() -> list[CConflict]:
    """The C (gcc) cases."""
    return [c for c in CC_CONFLICTS if c.language == "c"]


def cpp_conflicts() -> list[CConflict]:
    """The C++ (g++) cases."""
    return [c for c in CC_CONFLICTS if c.language == "cpp"]
