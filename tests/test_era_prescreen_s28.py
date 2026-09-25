"""S28-243.2 (queue item 6) — the era-header pre-screen.

The duckdb near-oracle class: the conflict file's TU needs APIs absent
from the tree (era-lost headers), and the ORACLE's own text uses them
— the pass criterion is unachievable in-place. The pre-screen composes
with the toolchain probe's already-held side-build failures: extract
the named symbols, grep the tree, and classify the GU door BEFORE any
model budget (the trial's 8 duckdb rows burned 7 draws + 1200s walls
discovering exactly this).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import importlib.util
import sys

from capybase.conflict_model import ConflictSide, ConflictUnit


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "live_eval_realworld_eps",
        Path(__file__).resolve().parent.parent / "scripts" / "live_eval_realworld.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["live_eval_realworld_eps"] = mod
    spec.loader.exec_module(mod)  # type: ignore[arg-type]
    return mod


_M = _load_module()


def _git_init(tmp_path: Path, files: dict[str, str]) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name, text in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "x"], check=True)
    return tmp_path


def _probe(sig_cur: list[str], sig_rep: list[str]) -> dict:
    return {
        "toolchain_dead": False,
        "gate": "make",
        "probes": {
            "current": {"rc": 2, "sig": sig_cur},
            "replayed": {"rc": 2, "sig": sig_rep},
            "oracle": {"rc": 2, "sig": []},
        },
    }


def _case(tmp_path: Path, expected_resolved: str):
    return SimpleNamespace(
        id="duckdb-history-0113", language="cpp", dataset="duckdb-history",
        path="src/parser/peg/autocomplete_core.cpp",
        expected_resolved=expected_resolved)


# the 0113 shape: the conflict TU's errors name GetTokenizer; the tree
# lacks it; the oracle's own text uses it
_0113_SIG = ["error: 'class duckdb::shared_ptr<duckdb::CompiledGrammar>' "
             "has no member named 'GetTokenizer'"]


def test_screen_fires_when_the_oracle_uses_the_missing_api(tmp_path):
    repo = _git_init(tmp_path, {"src/other.cpp": "int main() { return 0; }\n"})
    case = _case(tmp_path, expected_resolved=(
        "if (!compiled_grammar->GetTokenizer().TokenizeInput(b)) {\n"))
    screen = _M._era_header_screen(repo, case, _probe(_0113_SIG, _0113_SIG))
    assert screen["era_header_dead"] is True
    assert screen["missing_symbols"] == ["GetTokenizer"]
    assert screen["oracle_uses_missing"] == ["GetTokenizer"]


def test_screen_declines_when_the_symbol_exists_in_the_tree(tmp_path):
    repo = _git_init(tmp_path, {
        "include/peg.h": "struct CompiledGrammar { Tokenizer GetTokenizer(); };\n",
        "src/other.cpp": "int main() { return 0; }\n",
    })
    case = _case(tmp_path, expected_resolved="GetTokenizer();\n")
    screen = _M._era_header_screen(repo, case, _probe(_0113_SIG, _0113_SIG))
    assert screen["era_header_dead"] is False
    assert screen["missing_symbols"] == []


def test_screen_declines_when_the_oracle_dropped_the_api(tmp_path):
    """A missing symbol the ORACLE never uses is a fixable class — the
    resolution can drop the use; not the GU door."""
    repo = _git_init(tmp_path, {"src/other.cpp": "int main() { return 0; }\n"})
    case = _case(tmp_path, expected_resolved="return {};\n")
    screen = _M._era_header_screen(repo, case, _probe(_0113_SIG, _0113_SIG))
    assert screen["era_header_dead"] is False
    assert screen["missing_symbols"] == ["GetTokenizer"]


def test_screen_ignores_non_conflict_symbols_and_short_names():
    probe = _probe(
        ["error: 'class Foo' has no member named 'ab'"],  # too short
        [])
    case = _case(Path("/tmp"), expected_resolved="ab();\n")
    out = _M._era_header_screen(Path("/tmp"), case, probe)
    assert out is not None and out["era_header_dead"] is False


def test_toolchain_dead_probe_never_screens():
    case = _case(Path("/tmp"), expected_resolved="GetTokenizer();\n")
    assert _M._era_header_screen(
        Path("/tmp"), case, {"toolchain_dead": True, "probes": {}}) is None


def test_verdict_chain_reads_the_gu_door():
    r = _M.CaseResult(id="x", language="cpp", dataset="d")
    r.escalated = True
    r.era_header_dead = True
    assert _M._verdict_chain(r) == "GATE_UNAVAILABLE"
    # toolchain-dead still wins (the stricter, pre-existing class)
    r2 = _M.CaseResult(id="y", language="cpp", dataset="d")
    r2.escalated = True
    r2.toolchain_dead = True
    r2.era_header_dead = True
    assert _M._verdict_chain(r2) == "ESCALATE_TOOLCHAIN"


# ---------------------------------------------------------------------------
# S28-261: the v2 discriminator — compiler-evidence-backed
# ---------------------------------------------------------------------------

def test_v2_fires_on_signature_level_drift(tmp_path):
    """The 0113 shape: the symbol NAME exists elsewhere in the tree (the
    v1 name-grep declines) but the compiler proved THIS use invalid on
    its type — and the ORACLE repeats the same use spelling."""
    repo = _git_init(tmp_path, {
        "include/peg.h": "struct Tokenizer { void TokenizeInput(); };\n",
        "src/other.cpp": "int main() { return 0; }\n",
    })
    case = _case(tmp_path, expected_resolved=(
        "if (!compiled_grammar->GetTokenizer().TokenizeInput(b)) {\n"))
    screen = _M._era_header_screen(repo, case, _probe(_0113_SIG, _0113_SIG))
    assert screen["era_header_dead"] is True
    assert screen["oracle_repeats_invalid"] == ["GetTokenizer"]


def test_v2_declines_when_the_oracle_calls_a_bare_name(tmp_path):
    """No member-use spelling in the oracle (a bare call is a different
    binding) — the v2 discriminator declines even though the sides'
    errors name the symbol (the tree defines the name, so v1 declines
    too)."""
    repo = _git_init(tmp_path, {
        "include/peg.h": "GetTokenizer();\n",
        "src/other.cpp": "int main() { return 0; }\n",
    })
    case = _case(tmp_path, expected_resolved="GetTokenizer();\n")
    screen = _M._era_header_screen(repo, case, _probe(_0113_SIG, _0113_SIG))
    assert screen["era_header_dead"] is False
    assert screen["oracle_repeats_invalid"] == []
