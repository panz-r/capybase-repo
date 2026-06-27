"""Tests for the ``capybase calibrate`` / ``recalibrate`` CLI commands.

These exercise the CLI wiring via the ``_run_calibrate`` seam (which accepts an
injectable ``client_factory``) so no network is needed. The probe logic itself
is covered by ``tests/test_probes.py``; here we assert the command-level
contract: profile is written on success, NOT written on unreachable/dry-run,
JSON mode emits JSON, exit codes reflect reachability.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from capybase.adapters.llm_openai import LLMResponse
from capybase.calibration_profile import ModelProfile
from capybase.cli import DEFAULT_PROFILE_PATH, _run_calibrate
from capybase.config import Config

from tests.conftest import real_profile_loader  # noqa: F401

_VALID = '{"resolved_text": "x = 3", "needs_human": false}'

# The preservation regression below reads a prior profile back via
# ``ModelProfile.load`` (the same path ``_run_calibrate``'s preservation step
# uses), so opt back into the real loader — the suite-wide conftest fixture
# otherwise neuters ``load`` to keep the unit suite hermetic.
@pytest.fixture(autouse=True)
def _exercise_profile_io(real_profile_loader) -> None:
    pass


def _resp(text: str, finish: str = "stop", entropy: float | None = None) -> LLMResponse:
    return LLMResponse(
        text=text,
        raw={"_accumulated": {"finish_reason": finish}},
        mean_token_entropy=entropy,
    )


class CalibClient:
    """Fake LLMClient for the CLI seam — decides behavior from call kwargs so
    it needs no call ordering. Mirrors the one in tests/test_probes.py."""

    def __init__(
        self,
        *,
        truncate_below: int = 0,
        text: str = _VALID,
        finish: str = "stop",
        entropy: float | None = None,
        reject_json_mode: bool = False,
        reachable: bool = True,
    ) -> None:
        self.truncate_below = truncate_below
        self.text = text
        self.finish = finish
        self.entropy = entropy
        self.reject_json_mode = reject_json_mode
        self.reachable = reachable
        self.calls: list[dict] = []

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        self.calls.append({"max_tokens": max_tokens, "json_mode": json_mode})
        if not self.reachable:
            raise RuntimeError("server down")
        if json_mode and self.reject_json_mode:
            raise RuntimeError("400 response_format unsupported")
        if max_tokens < self.truncate_below:
            return _resp(self.text, finish="length", entropy=self.entropy)
        return _resp(self.text, finish=self.finish, entropy=self.entropy)


def _factory(client: CalibClient):
    return lambda _model_cfg: client


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# success path
# ---------------------------------------------------------------------------


def test_calibrate_writes_profile_and_returns_zero(tmp_path: Path):
    client = CalibClient(truncate_below=8192, entropy=0.5)
    profile_path = tmp_path / "model_profile.json"
    rc = _run_calibrate(
        Config(),
        repo=str(tmp_path),
        profile_path=str(profile_path),
        client_factory=_factory(client),
        out=io.StringIO(),
    )
    assert rc == 0
    assert profile_path.is_file()
    data = _load_json(profile_path)
    assert data["model"] == "vibethink"
    assert data["max_tokens"] == 16384  # 8192 first success -> 1.5x headroom -> snap to 16384
    assert data["capture_token_entropy"] is True


def test_calibrate_profile_path_resolves_relative_to_repo(tmp_path: Path):
    # Relative path should land inside the repo root.
    client = CalibClient(entropy=0.5)
    repo = tmp_path / "myrepo"
    repo.mkdir()
    rc = _run_calibrate(
        Config(),
        repo=str(repo),
        profile_path=DEFAULT_PROFILE_PATH,
        client_factory=_factory(client),
        out=io.StringIO(),
    )
    assert rc == 0
    assert (repo / DEFAULT_PROFILE_PATH).is_file()


def test_calibrate_dry_run_does_not_write(tmp_path: Path):
    client = CalibClient(entropy=0.5)
    profile_path = tmp_path / "model_profile.json"
    out = io.StringIO()
    rc = _run_calibrate(
        Config(),
        repo=str(tmp_path),
        profile_path=str(profile_path),
        dry_run=True,
        client_factory=_factory(client),
        out=out,
    )
    assert rc == 0
    assert not profile_path.is_file()
    assert "dry-run" in out.getvalue()


def test_calibrate_json_output_emits_valid_json(tmp_path: Path):
    client = CalibClient(entropy=0.5)
    out = io.StringIO()
    rc = _run_calibrate(
        Config(),
        repo=str(tmp_path),
        profile_path=str(tmp_path / "p.json"),
        json_output=True,
        client_factory=_factory(client),
        out=out,
    )
    assert rc == 0
    payload = json.loads(out.getvalue())
    assert payload["model"] == "vibethink"
    assert payload["_ok"] is True
    assert payload["_written"] is True


# ---------------------------------------------------------------------------
# failure path
# ---------------------------------------------------------------------------


def test_calibrate_unreachable_returns_one_and_does_not_write(tmp_path: Path):
    client = CalibClient(reachable=False)
    profile_path = tmp_path / "model_profile.json"
    out = io.StringIO()
    rc = _run_calibrate(
        Config(),
        repo=str(tmp_path),
        profile_path=str(profile_path),
        client_factory=_factory(client),
        out=out,
    )
    assert rc == 1
    assert not profile_path.is_file()
    assert "unreachable" in out.getvalue().lower()


def test_calibrate_overwrites_existing_profile(tmp_path: Path, monkeypatch):
    # First calibration writes a profile.
    profile_path = tmp_path / "model_profile.json"
    _run_calibrate(
        Config(),
        repo=str(tmp_path),
        profile_path=str(profile_path),
        client_factory=_factory(CalibClient(entropy=0.5)),
        out=io.StringIO(),
    )
    first = _load_json(profile_path)
    assert first["max_tokens"] == 2048  # 1024 first success -> 1.5x headroom -> snap to 2048

    # A model that needs more tokens → recalibrate overwrites the same file.
    _run_calibrate(
        Config(),
        repo=str(tmp_path),
        profile_path=str(profile_path),
        client_factory=_factory(CalibClient(truncate_below=16384, entropy=0.5)),
        out=io.StringIO(),
    )
    second = _load_json(profile_path)
    assert second["max_tokens"] == 32768  # 16384 first success -> 1.5x -> snap to 32768
    assert first["probed_at"] != second["probed_at"] or first["max_tokens"] != second["max_tokens"]


# ---------------------------------------------------------------------------
# recalibrate subcommand wiring (just argparse → _run_calibrate)
# ---------------------------------------------------------------------------


def test_recalibrate_subcommand_uses_default_profile_path(tmp_path: Path, monkeypatch):
    """``recalibrate`` is a bare alias for ``calibrate`` with the default path.
    Verify the CLI routes it through ``_run_calibrate`` and writes the profile."""
    from capybase.cli import main

    # Run from inside the repo dir so the relative default path lands here.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "capybase.cli._real_client", lambda _cfg: CalibClient(entropy=0.5)
    )
    rc = main(["--repo", str(tmp_path), "recalibrate"])
    assert rc == 0
    assert (tmp_path / DEFAULT_PROFILE_PATH).is_file()


# ---------------------------------------------------------------------------
# global --profile flag (shared by all commands: read + write location)
# ---------------------------------------------------------------------------


def test_global_profile_flag_directs_calibrate_write(tmp_path: Path, monkeypatch):
    """``--profile PATH`` (top-level) tells calibrate WHERE to write, overriding
    the default memory path."""
    from capybase.cli import main

    custom = tmp_path / "elsewhere" / "my-profile.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("capybase.cli._real_client", lambda _cfg: CalibClient(entropy=0.5))
    rc = main(["--repo", str(tmp_path), "--profile", str(custom), "calibrate"])
    assert rc == 0
    # Written to the EXPLICIT path, not the default.
    assert custom.is_file()
    assert not (tmp_path / DEFAULT_PROFILE_PATH).is_file()


def test_global_profile_flag_default_unchanged(tmp_path: Path, monkeypatch):
    """Without --profile, calibrate writes to the default memory path."""
    from capybase.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("capybase.cli._real_client", lambda _cfg: CalibClient(entropy=0.5))
    rc = main(["--repo", str(tmp_path), "calibrate"])
    assert rc == 0
    assert (tmp_path / DEFAULT_PROFILE_PATH).is_file()


# ---------------------------------------------------------------------------
# Embeddings-calibration preservation across an LLM re-tune
# ---------------------------------------------------------------------------
#
# The two commands co-own the profile file. A fresh ``calibrate`` rebuilds the
# whole profile, so without a carry-over it silently reset the model-specific
# ``embedding_min_similarity`` (+ envelope) that ``calibrate-embeddings`` had
# derived back to the 0.35 default. Regression for the run-order hazard.

# A representative envelope as ``calibrate-embeddings`` would write it.
_EMB_ENV = {
    "model": "embed",
    "min_similarity": 0.71,
    "estimates": {"quantile_gap": 0.71, "related_p10": 0.83, "unrelated_p90": 0.40},
    "related": {"count": 8, "min": 0.7, "max": 0.99, "mean": 0.88},
    "unrelated": {"count": 8, "min": 0.05, "max": 0.41, "mean": 0.22},
    "ok": True,
    "probed_at": "2026-06-27T00:00:00+00:00",
    "notes": [],
}


def _seed_embeddings_profile(path: Path, *, model: str = "vibethink") -> None:
    """Write a profile as if ``calibrate-embeddings`` had just run."""
    ModelProfile(
        model=model,
        max_tokens=8192,
        json_mode=True,
        capture_token_entropy=False,
        generation_timeout_seconds=60,
        embedding_min_similarity=0.71,
        embedding_calibration=_EMB_ENV,
    ).save(path)


def test_calibrate_preserves_embeddings_floor_across_retune(tmp_path: Path):
    """``calibrate`` (LLM re-tune) must NOT wipe the calibrated embeddings floor
    when the model is unchanged — the two commands co-own the profile."""
    profile_path = tmp_path / "model_profile.json"
    _seed_embeddings_profile(profile_path)

    rc = _run_calibrate(
        Config(),  # model "vibethink" — matches the seeded profile
        repo=str(tmp_path),
        profile_path=str(profile_path),
        client_factory=_factory(CalibClient(entropy=0.5)),
        out=io.StringIO(),
    )
    assert rc == 0
    data = _load_json(profile_path)
    # LLM knobs freshly re-tuned...
    assert data["max_tokens"] == 2048  # 1024 first success -> 1.5x -> snap 2048
    # ...but the embeddings floor + envelope carried over intact.
    assert data["embedding_min_similarity"] == 0.71
    assert data["embedding_calibration"]["min_similarity"] == 0.71
    assert data["embedding_calibration"]["estimates"]["quantile_gap"] == 0.71


def test_calibrate_drops_embeddings_floor_on_model_swap(tmp_path: Path):
    """A model swap correctly discards the calibrated floor — it was fit for the
    old model and would be wrong now. The fresh profile's default (0.35) wins."""
    profile_path = tmp_path / "model_profile.json"
    _seed_embeddings_profile(profile_path, model="old-model")

    cfg = Config()
    cfg.model.model = "new-model"  # different model → preservation skipped
    rc = _run_calibrate(
        cfg,
        repo=str(tmp_path),
        profile_path=str(profile_path),
        client_factory=_factory(CalibClient(entropy=0.5)),
        out=io.StringIO(),
    )
    assert rc == 0
    data = _load_json(profile_path)
    assert data["model"] == "new-model"
    assert data["embedding_min_similarity"] == 0.35  # default, not carried over
    assert data["embedding_calibration"] == {}


def test_calibrate_preserves_floor_first_run_has_default(tmp_path: Path):
    """No prior profile at all: ``calibrate`` writes the default floor (nothing
    to carry over). Confirms the carry-over is a no-op when there's no prior."""
    profile_path = tmp_path / "model_profile.json"
    rc = _run_calibrate(
        Config(),
        repo=str(tmp_path),
        profile_path=str(profile_path),
        client_factory=_factory(CalibClient(entropy=0.5)),
        out=io.StringIO(),
    )
    assert rc == 0
    data = _load_json(profile_path)
    assert data["embedding_min_similarity"] == 0.35
    assert data["embedding_calibration"] == {}
