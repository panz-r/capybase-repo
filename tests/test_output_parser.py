"""Tests for the test-output parser (cargo / pytest verdict classification).

The orchestrator used to treat a test run as ``returncode == 0``, conflating
transient lock contention, real compile errors, test failures, and passes. These
exercise the pure parser over real-shaped stdout/stderr samples for each signal.
"""

from __future__ import annotations

from capybase.test_output import classify_test_output


# ---------------------------------------------------------------------------
# Cargo: lock contention (the transient case that shouldn't abort)
# ---------------------------------------------------------------------------


def test_cargo_lock_contention_is_transient():
    """``Blocking waiting for file lock`` → lock_contention, retryable."""
    v = classify_test_output(
        "cargo test", "",
        "   Blocking waiting for file lock on build directory\n",
        returncode=-1, timed_out=True,
    )
    assert v.kind == "lock_contention"
    assert v.is_transient is True
    assert "lock" in v.summary.lower()


def test_cargo_lock_contention_wins_over_timeout():
    """Even a timed-out run reads as lock_contention if the lock message is
    present — the timeout was just the wall clock while blocked on the lock."""
    v = classify_test_output(
        "cargo test", "",
        "   Blocking waiting for file lock on build directory\n",
        returncode=-1, timed_out=True,
    )
    assert v.kind == "lock_contention"


# ---------------------------------------------------------------------------
# Cargo: compile error (the merge doesn't compile)
# ---------------------------------------------------------------------------


def test_cargo_compile_error():
    """``error[E0433]`` → compile_error, not a generic failure."""
    stderr = (
        "error[E0433]: failed to resolve: could not find `tools` in the crate root\n"
        "  --> src/lib.rs:5:13\n"
        "error: could not compile `di-core` due to previous error\n"
    )
    v = classify_test_output("cargo test", "", stderr, returncode=101)
    assert v.kind == "compile_error"
    assert v.tool == "cargo"
    assert any("E0433" in d for d in v.diagnostics)


def test_cargo_could_not_compile_without_code():
    """A bare ``could not compile`` (no error code) still reads as compile_error."""
    v = classify_test_output(
        "cargo test", "", "error: could not compile `x` due to 2 previous errors\n",
        returncode=101,
    )
    assert v.kind == "compile_error"


# ---------------------------------------------------------------------------
# Cargo: test failure vs pass
# ---------------------------------------------------------------------------


def test_cargo_test_failed():
    """``test result: FAILED.`` → failed (a real test regression)."""
    stdout = (
        "running 3 tests\n"
        "test tests::it_works ... ok\n"
        "test tests::it_breaks ... FAILED\n"
        "test result: FAILED. 1 passed; 1 failed; 0 ignored\n"
    )
    v = classify_test_output("cargo test", stdout, "", returncode=101)
    assert v.kind == "failed"
    assert any("FAILED" in d or "panicked" in d for d in v.diagnostics)


def test_cargo_test_passed():
    stdout = (
        "test result: ok. 5 passed; 0 failed; 0 ignored\n"
    )
    v = classify_test_output("cargo test", stdout, "", returncode=0)
    assert v.kind == "passed"


def test_cargo_failed_dominates_over_ok():
    """A failing target dominates a passing one (multi-target: lib + bin)."""
    stdout = (
        "test result: ok. 5 passed; 0 failed\n"     # lib target
        "test result: FAILED. 0 passed; 1 failed\n" # bin target
    )
    v = classify_test_output("cargo test", stdout, "", returncode=101)
    assert v.kind == "failed"


# ---------------------------------------------------------------------------
# Cargo: no test-result line (check-only / zero tests)
# ---------------------------------------------------------------------------


def test_cargo_no_tests_clean():
    """Ran but no ``test result:`` line, rc=0 → no_tests (inconclusive-but-clean)."""
    v = classify_test_output("cargo test", "   Compiling x v0.1.0\n", "", returncode=0)
    assert v.kind == "no_tests"


# ---------------------------------------------------------------------------
# Timeout (slow compile, not a failure)
# ---------------------------------------------------------------------------


def test_timeout_mid_compile_flagged_as_compilation():
    """A timeout while still ``Compiling`` → timed_out with a 'during
    compilation' note (the build is slow, not the tests hanging)."""
    stderr = "   Compiling di-core v0.1.0 (/w/repo/di-core)\n"
    v = classify_test_output("cargo test", "", stderr, returncode=-1, timed_out=True)
    assert v.kind == "timed_out"
    assert "compilation" in v.summary


def test_timeout_after_compile():
    """A timeout with no lock and no compile line → bare timed_out."""
    v = classify_test_output("cargo test", "running tests...\n", "", returncode=-1, timed_out=True)
    assert v.kind == "timed_out"
    assert "compilation" not in v.summary


# ---------------------------------------------------------------------------
# pytest
# ---------------------------------------------------------------------------


def test_pytest_failed():
    stdout = "===== 1 failed, 4 passed in 0.5s =====\n"
    v = classify_test_output("pytest", stdout, "", returncode=1)
    assert v.kind == "failed"
    assert "1 test(s) failed" in v.summary


def test_pytest_passed():
    stdout = "===== 5 passed in 0.3s =====\n"
    v = classify_test_output("pytest", stdout, "", returncode=0)
    assert v.kind == "passed"


def test_pytest_no_tests_collected():
    v = classify_test_output("pytest", "no tests ran\n", "", returncode=5)
    assert v.kind == "no_tests"


def test_pytest_collection_error():
    """An import/collection error (not a test failure) → compile_error."""
    stdout = "===== ERROR collecting tests/test_x.py =====\nImportError: no module\n"
    v = classify_test_output("pytest", stdout, "ImportError", returncode=2)
    assert v.kind == "compile_error"


# ---------------------------------------------------------------------------
# Unknown command fallback
# ---------------------------------------------------------------------------


def test_unknown_command_pass():
    v = classify_test_output("make test", "all good\n", "", returncode=0)
    assert v.kind == "passed"


def test_unknown_command_fail():
    v = classify_test_output("make test", "", "boom", returncode=2)
    assert v.kind == "unknown"


# ---------------------------------------------------------------------------
# parse_passing_node_ids: the test-continuity baseline substrate.
# ---------------------------------------------------------------------------

from capybase.test_output import parse_passing_node_ids  # noqa: E402


def test_parse_passing_node_ids_pytest():
    """pytest -v: ``node PASSED`` lines → the passing node-ID set."""
    out = (
        "tests/test_auth.py::test_login PASSED                              [ 33%]\n"
        "tests/test_auth.py::test_logout PASSED                             [ 66%]\n"
        "tests/test_auth.py::test_signup FAILED                             [100%]\n"
        "========================= 2 passed, 1 failed in 0.1s =========================\n"
    )
    p = parse_passing_node_ids(out, "pytest")
    assert p == {"tests/test_auth.py::test_login", "tests/test_auth.py::test_logout"}


def test_parse_passing_node_ids_cargo():
    """cargo: ``test name ... ok`` lines → the passing test-name set."""
    out = (
        "running 3 tests\n"
        "test config::tests::test_new ... ok\n"
        "test config::tests::test_port ... ok\n"
        "test config::tests::test_bad ... FAILED\n"
        "test result: FAILED. 2 passed; 1 failed\n"
    )
    p = parse_passing_node_ids(out, "cargo")
    assert p == {"config::tests::test_new", "config::tests::test_port"}


def test_parse_passing_node_ids_non_verbose_pytest_is_empty():
    """Without -v, pytest emits no per-test lines → empty set (inert baseline)."""
    assert parse_passing_node_ids("=== 2 passed in 0.1s ===", "pytest") == set()


def test_parse_passing_node_ids_unknown_tool_is_empty():
    assert parse_passing_node_ids("test foo ... ok", "jest") == set()


def test_parse_passing_node_ids_empty_stdout():
    assert parse_passing_node_ids("", "pytest") == set()

