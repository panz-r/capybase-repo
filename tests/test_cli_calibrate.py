"""Tests for the ``capybase calibrate`` CLI commands.

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


def test_calibrate_writes_new_unique_profile(tmp_path: Path):
    """s27-92: calibrate writes a NEW, UNIQUE profile and never overwrites
    an existing one — activation is an explicit separate step."""
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
    assert not profile_path.exists(), "the active path must stay untouched"
    written = sorted(tmp_path.glob("model_profile.*.json"))
    assert len(written) == 1, written
    data = _load_json(written[0])
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
    written = sorted(repo.glob(str(Path(DEFAULT_PROFILE_PATH).parent) + "/model_profile*.json"))
    assert len(written) == 1, written


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


def test_calibrate_never_overwrites_writes_new_unique(tmp_path: Path):
    """s27-92: each calibrate writes its OWN unique profile; the previous
    one stays byte-identical (nothing overwrites an existing profile)."""
    profile_path = tmp_path / "model_profile.json"
    _run_calibrate(
        Config(),
        repo=str(tmp_path),
        profile_path=str(profile_path),
        client_factory=_factory(CalibClient(entropy=0.5)),
        out=io.StringIO(),
    )
    first_files = sorted(tmp_path.glob("model_profile*.json"))
    assert len(first_files) == 1
    first_bytes = first_files[0].read_bytes()

    _run_calibrate(
        Config(),
        repo=str(tmp_path),
        profile_path=str(profile_path),
        client_factory=_factory(CalibClient(truncate_below=16384, entropy=0.5)),
        out=io.StringIO(),
    )
    second_files = sorted(tmp_path.glob("model_profile*.json"))
    assert len(second_files) == 2, second_files
    untouched = [f for f in second_files if f.read_bytes() == first_bytes]
    assert len(untouched) == 1, "the first profile must stay byte-identical"


# ---------------------------------------------------------------------------
# calibrate subcommand wiring (just argparse → _run_calibrate)
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
    # Written BESIDE the explicit path with a unique name — never at (or
    # over) the explicit path itself.
    written = sorted(custom.parent.glob(custom.stem + ".*.json"))
    assert written and not custom.exists(), (written, custom)


def test_global_profile_flag_default_unchanged(tmp_path: Path, monkeypatch):
    """Without --profile, calibrate writes to the default path in the config dir."""
    from capybase.cli import main

    cdir = tmp_path / "cfg"
    cdir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setattr("capybase.cli._real_client", lambda _cfg: CalibClient(entropy=0.5))
    rc = main(["--config", str(cdir), "--repo", str(repo), "calibrate"])
    assert rc == 0
    # s27-92: a unique new file per run — the active model_profile.json is
    # never created/touched by calibrate.
    written = sorted(cdir.glob("model_profile.*.json"))
    assert len(written) == 1, written
    assert not (cdir / "model_profile.json").exists()


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
    # s27-92: the run writes a NEW unique profile beside the seeded one —
    # the seeded active profile must stay byte-identical...
    active = _load_json(profile_path)
    assert active["embedding_min_similarity"] == 0.71
    # ...and the WRITTEN profile carries the floor + envelope intact.
    written = sorted(tmp_path.glob("model_profile.*.json"))
    assert len(written) == 1, written
    data = _load_json(written[0])
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
    written = sorted(tmp_path.glob("model_profile.*.json"))
    assert len(written) == 1, written
    data = _load_json(written[0])
    assert data["model"] == "new-model"
    assert data["embedding_min_similarity"] == 0.35  # default, not carried over
    assert data["embedding_calibration"] == {}
    # the seeded active profile stays untouched
    assert _load_json(profile_path)["model"] == "old-model"


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
    written = sorted(tmp_path.glob("model_profile.*.json"))
    assert len(written) == 1, written
    data = _load_json(written[0])
    assert data["embedding_min_similarity"] == 0.35
    assert data["embedding_calibration"] == {}
