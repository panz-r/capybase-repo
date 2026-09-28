"""S28-345 sequenced syntax preflight — the offline probe that caught the
twin-class bridge drop + the fail-open retirement (S28-349).

Two defects, both probe-proven:

1. BRIDGE DROP: the S28-345 field lived only on config.py's pydantic
   ValidationConfig; the orchestrator's bridge (``ValidationConfig.from_dict``
   filters by ``__dataclass_fields__``) silently dropped it, so the engine's
   ``getattr`` read the default False forever. Trial41 armed the flag (the
   flight config.toml proves it) yet duckdb-0053 burned its full 300s window
   with zero preflight probes. The twin field + the parity test
   (``test_validation_bridge_parity_s28.py``) close this.

2. FAIL-OPEN RETIREMENT: the preflight emulates a failed proc whose stderr
   names the /tmp copy gcc compiled — and the failed-proc tail localizes
   errors by file stem, so the tmp stem classified as a SIBLING error and
   the verdict flipped to PASS on a buffer that does not parse. The fix
   rewrites the tmp location to the conflict file before the emulation.

The A/B here builds the engine through the REAL orchestrator bridge and
proves both halves: flag ON retires the build window (sentinel untouched)
with a fail-closed verdict carrying the conflict-file location; flag OFF
runs the build (the pre-fix world). The fixture's parse failure (a dangling
``else``) deliberately survives the verifier's repair battery — the battery
fixes simple brace/paren/literal defects before the preflight ever runs.
"""

from __future__ import annotations

import pytest

from capybase.config import ValidationConfig as PydanticValidationConfig
from capybase.verification import BuildStateTracker, VerificationEngine

# A PARSE error the repair battery cannot fix (braces/parens/literals all
# balance) and that gcc classifies as a parse error, not a semantic one.
BROKEN_C = (
    "int add(int a, int b) { return a + b; }\n"
    "int main(void) { return add(1, 2); }\n"
    "else { }\n"
)


@pytest.fixture()
def mini_repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.c").write_text("/* original */\n")
    return tmp_path


def _build_engine(mini_repo, *, flag: bool, events: list):
    # The orchestrator's exact construction path: pydantic config ->
    # from_dict(model_dump()) -> VerificationEngine (the bridge that ate
    # the flag before S28-349).
    pyd = PydanticValidationConfig(
        cc_build_command="sh -c 'touch build-ran.sentinel'",
        enable_syntax_preflight=flag,
    )
    from capybase.verification import ValidationConfig as DataVC
    bridged = DataVC.from_dict(pyd.model_dump())
    assert bridged.enable_syntax_preflight is flag
    engine = VerificationEngine.default(bridged)
    engine.build_state = BuildStateTracker(
        event_sink=lambda ev, payload: events.append((ev, payload)))
    return engine


def _verify(mini_repo, engine):
    return engine.verify_file(
        path="src/main.c",
        language="c",
        original="",  # no conflicted original -> no identical-failure excuse
        resolutions=[],
        repo_root=str(mini_repo),
        whole_text=BROKEN_C,
    )


def test_preflight_retires_the_window_fail_closed(mini_repo):
    events: list = []
    engine = _build_engine(mini_repo, flag=True, events=events)
    result = _verify(mini_repo, engine)

    probes = [p for ev, p in events if ev == "build_probe"]
    preflight = [p for p in probes if p.get("note") == "S28-345 preflight"]
    assert preflight, f"preflight probe missing: {probes}"
    assert preflight[0]["outcome"] == "skip_syntax_failed"
    # fail CLOSED: the parse failure is a hard error, localized to the
    # conflict file (not the /tmp copy gcc compiled)
    assert not result.passed
    assert result.hard_failures
    assert "main.c" in result.hard_failures[0].message
    assert "else" in result.hard_failures[0].message
    # the window is retired: the build command never executed
    assert not (mini_repo / "build-ran.sentinel").exists()


def test_flag_off_runs_the_build(mini_repo):
    events: list = []
    engine = _build_engine(mini_repo, flag=False, events=events)
    result = _verify(mini_repo, engine)

    probes = [p for ev, p in events if ev == "build_probe"]
    assert probes, "flag off: the build path should have run"
    assert not any(p.get("note") == "S28-345 preflight" for p in probes)
    # the fake build exits 0, so the verification passes on the build's word
    assert result.passed
    assert (mini_repo / "build-ran.sentinel").exists()
