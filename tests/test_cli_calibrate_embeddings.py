"""Tests for the ``capybase calibrate-embeddings`` CLI command.

These exercise the command via the ``_run_calibrate_embeddings`` seam (which
accepts an injectable ``client_factory``) so no network is needed. The
calibrator's statistics are covered by ``tests/test_embeddings_calibration.py``;
here we assert the command-level contract: the profile is written on success
(with ONLY the embedding fields touched, the LLM-calibration knobs preserved),
NOT written on unreachable/dry-run, JSON mode emits JSON, and exit codes reflect
reachability.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from capybase.calibration_profile import ModelProfile
from capybase.cli import DEFAULT_PROFILE_PATH, _run_calibrate_embeddings
from capybase.config import Config

from tests.conftest import real_profile_loader  # noqa: F401


@pytest.fixture(autouse=True)
def _exercise_profile_io(real_profile_loader) -> None:
    """This module writes profiles and reads them back via ``ModelProfile.load``,
    so opt back into the real loader (the suite-wide conftest fixture otherwise
    disables it to keep the unit suite hermetic)."""


class _DomainFakeClient:
    """Maps texts to 2D vectors by domain so related pairs land close together
    and unrelated pairs far apart — a well-separated calibration result.

    Mirrors the fake in ``tests/test_embeddings_calibration.py`` so the command
    observes a realistic ``ok=True`` envelope.
    """

    def embed(self, texts):
        if isinstance(texts, str):
            texts = [texts]
        vecs = []
        for t in texts:
            if any(k in t for k in ["rust", "fn ", "impl", "const", "enum", "struct"]):
                base = [0.9, 0.1]
            else:
                base = [0.1, 0.9]
            noise = (len(t) % 7) * 0.01
            vecs.append([base[0] + noise, base[1] - noise])
        return vecs


class _FailingClient:
    """An unreachable embeddings endpoint."""

    def embed(self, texts):
        raise RuntimeError("server down")


def _factory(client):
    return lambda _model_cfg, _emb_model: client


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _config_with_model(model: str = "vibethink", embeddings_model: str = "qwen-embed") -> Config:
    cfg = Config()
    cfg.model.model = model
    cfg.memory.embeddings_model = embeddings_model
    return cfg


# ---------------------------------------------------------------------------
# success path
# ---------------------------------------------------------------------------


def test_calibrate_embeddings_writes_profile_and_returns_zero(tmp_path: Path):
    profile_path = tmp_path / "model_profile.json"
    rc = _run_calibrate_embeddings(
        _config_with_model(),
        repo=str(tmp_path),
        profile_path=str(profile_path),
        client_factory=_factory(_DomainFakeClient()),
        out=io.StringIO(),
    )
    assert rc == 0
    assert profile_path.is_file()
    data = _load_json(profile_path)
    # The calibrated floor is written, within the valid similarity range.
    assert 0.0 < data["embedding_min_similarity"] <= 1.0
    # The full calibration envelope is recorded for transparency.
    env = data["embedding_calibration"]
    assert env["ok"] is True
    assert env["model"] == "qwen-embed"
    assert "estimates" in env and "quantile_gap" in env["estimates"]


def test_calibrate_embeddings_preserves_llm_calibration_knobs(tmp_path: Path):
    """A prior ``calibrate`` run wrote LLM-knobs; calibrate-embeddings must
    preserve them and touch only the embedding fields."""
    profile_path = tmp_path / "model_profile.json"
    pre = ModelProfile(
        model="vibethink",
        max_tokens=16384,
        json_mode=False,
        capture_token_entropy=True,
        generation_timeout_seconds=240,
    )
    pre.save(profile_path)

    _run_calibrate_embeddings(
        _config_with_model(),
        repo=str(tmp_path),
        profile_path=str(profile_path),
        client_factory=_factory(_DomainFakeClient()),
        out=io.StringIO(),
    )
    data = _load_json(profile_path)
    # LLM-calibration knobs untouched.
    assert data["max_tokens"] == 16384
    assert data["capture_token_entropy"] is True
    assert data["generation_timeout_seconds"] == 240
    # Embedding fields now populated.
    assert data["embedding_min_similarity"] != 0.35
    assert data["embedding_calibration"]["ok"] is True


def test_calibrate_embeddings_creates_profile_when_absent(tmp_path: Path):
    """No prior profile: a fresh one is created with safe defaults for the LLM
    knobs and the calibrated embedding floor set."""
    profile_path = tmp_path / "model_profile.json"
    rc = _run_calibrate_embeddings(
        _config_with_model(),
        repo=str(tmp_path),
        profile_path=str(profile_path),
        client_factory=_factory(_DomainFakeClient()),
        out=io.StringIO(),
    )
    assert rc == 0
    p = ModelProfile.load(profile_path)
    assert p is not None
    assert p.model == "vibethink"  # match key set from active config
    assert 0.0 < p.embedding_min_similarity <= 1.0


def test_calibrate_embeddings_keeps_model_match_key_current(tmp_path: Path):
    """The stored profile's ``model`` is rewritten to the active model name so
    the overlay's name-match still applies after calibration."""
    profile_path = tmp_path / "model_profile.json"
    pre = ModelProfile(
        model="old-model",
        max_tokens=4096,
        json_mode=True,
        capture_token_entropy=False,
        generation_timeout_seconds=60,
    )
    pre.save(profile_path)

    _run_calibrate_embeddings(
        _config_with_model(model="vibethink"),
        repo=str(tmp_path),
        profile_path=str(profile_path),
        client_factory=_factory(_DomainFakeClient()),
        out=io.StringIO(),
    )
    p = ModelProfile.load(profile_path)
    assert p is not None
    assert p.model == "vibethink"


def test_calibrate_embeddings_dry_run_does_not_write(tmp_path: Path):
    profile_path = tmp_path / "model_profile.json"
    out = io.StringIO()
    rc = _run_calibrate_embeddings(
        _config_with_model(),
        repo=str(tmp_path),
        profile_path=str(profile_path),
        dry_run=True,
        client_factory=_factory(_DomainFakeClient()),
        out=out,
    )
    assert rc == 0
    assert not profile_path.is_file()
    assert "dry-run" in out.getvalue()


def test_calibrate_embeddings_json_output_emits_valid_json(tmp_path: Path):
    out = io.StringIO()
    rc = _run_calibrate_embeddings(
        _config_with_model(),
        repo=str(tmp_path),
        profile_path=str(tmp_path / "p.json"),
        json_output=True,
        client_factory=_factory(_DomainFakeClient()),
        out=out,
    )
    assert rc == 0
    payload = json.loads(out.getvalue())
    assert payload["ok"] is True
    assert payload["_written"] is True
    assert "estimates" in payload


# ---------------------------------------------------------------------------
# failure path
# ---------------------------------------------------------------------------


def test_calibrate_embeddings_unreachable_returns_one_and_does_not_write(tmp_path: Path):
    profile_path = tmp_path / "model_profile.json"
    out = io.StringIO()
    rc = _run_calibrate_embeddings(
        _config_with_model(),
        repo=str(tmp_path),
        profile_path=str(profile_path),
        client_factory=_factory(_FailingClient()),
        out=out,
    )
    assert rc == 1
    assert not profile_path.is_file()
    assert "unreachable" in out.getvalue().lower()


def test_calibrate_embeddings_json_unreachable_reports_not_written(tmp_path: Path):
    out = io.StringIO()
    rc = _run_calibrate_embeddings(
        _config_with_model(),
        repo=str(tmp_path),
        profile_path=str(tmp_path / "p.json"),
        json_output=True,
        client_factory=_factory(_FailingClient()),
        out=out,
    )
    assert rc == 1
    payload = json.loads(out.getvalue())
    assert payload["ok"] is False
    assert payload["_written"] is False


# ---------------------------------------------------------------------------
# report formatting
# ---------------------------------------------------------------------------


def test_calibrate_embeddings_report_shows_distributions_and_estimates(tmp_path: Path):
    """The human-readable report surfaces the measured distributions and all
    three threshold estimates (the transparency data for manual re-tuning)."""
    out = io.StringIO()
    _run_calibrate_embeddings(
        _config_with_model(),
        repo=str(tmp_path),
        profile_path=str(tmp_path / "p.json"),
        client_factory=_factory(_DomainFakeClient()),
        out=out,
    )
    text = out.getvalue()
    assert "related" in text and "unrelated" in text  # both distributions
    assert "quantile_gap" in text
    assert "related_p10" in text
    assert "unrelated_p90" in text


def test_calibrate_embeddings_report_shows_zones_and_fit_quality(tmp_path: Path):
    """A well-separated model yields an isotonic fit, so the report surfaces the
    three-zone thresholds (green/amber/red) and the KS fit-quality line."""
    out = io.StringIO()
    _run_calibrate_embeddings(
        _config_with_model(),
        repo=str(tmp_path),
        profile_path=str(tmp_path / "p.json"),
        client_factory=_factory(_DomainFakeClient()),
        out=out,
    )
    text = out.getvalue()
    assert "score calibration" in text
    assert "isotonic transform" in text
    assert "KS separation" in text
    # All three zones appear on the calibrated scale.
    assert "green" in text and "amber" in text and "red" in text
    assert "calibrated scale" in text


def test_calibrate_embeddings_report_no_zones_when_fit_dropped(tmp_path: Path):
    """A weak/overlapping model drops the isotonic fit — the zones/fit-quality
    block must NOT appear (graceful: report stays useful without it)."""
    from tests.test_embeddings_calibration import _ZeroVectorClient

    out = io.StringIO()
    _run_calibrate_embeddings(
        _config_with_model(),
        repo=str(tmp_path),
        profile_path=str(tmp_path / "p.json"),
        client_factory=_factory(_ZeroVectorClient()),
        out=out,
    )
    text = out.getvalue()
    assert "score calibration" not in text
    assert "green" not in text  # no zones block


def test_calibrate_embeddings_json_payload_carries_zones(tmp_path: Path):
    """The --json payload includes the zones + isotonic points + ks_separation."""
    out = io.StringIO()
    _run_calibrate_embeddings(
        _config_with_model(),
        repo=str(tmp_path),
        profile_path=str(tmp_path / "p.json"),
        json_output=True,
        client_factory=_factory(_DomainFakeClient()),
        out=out,
    )
    payload = json.loads(out.getvalue())
    env = payload  # the JSON output is the calibration envelope + flags
    assert "zones" in env and {"green", "amber", "red"} <= set(env["zones"])
    assert isinstance(env["isotonic_points"], list) and len(env["isotonic_points"]) > 0
    assert env["ks_separation"] > 0.0


# ---------------------------------------------------------------------------
# Offline drift detection (survey 2 §7) — drift block vs a prior run
# ---------------------------------------------------------------------------


def _seed_envelope(path: Path, *, model: str = "vibethink", related_mad: float = 0.02) -> None:
    """Write a prior profile carrying a full calibration envelope (as if a
    previous calibrate-embeddings had run), with a small related MAD so a later
    median shift reliably trips the drift threshold."""
    env = {
        "model": model,
        "min_similarity": 0.6,
        "estimates": {"quantile_gap": 0.6, "related_p10": 0.9, "unrelated_p90": 0.3},
        "related": {"count": 24, "min": 0.7, "max": 0.99, "mean": 0.9, "median": 0.9, "mad": related_mad},
        "unrelated": {"count": 24, "min": 0.05, "max": 0.4, "mean": 0.2, "median": 0.2, "mad": related_mad},
        "ok": True, "probed_at": "2026-06-01T00:00:00+00:00", "notes": [],
        "isotonic_points": [[0.1, 0.0], [0.9, 1.0]],
        "zones": {"green": 0.7, "amber": 0.6, "red": 0.5},
        "ks_separation": 0.8,
        "fit_loss": "l2", "zone_method": "mad",
        "related_mad": related_mad, "unrelated_mad": related_mad,
    }
    ModelProfile(
        model=model, max_tokens=8192, json_mode=True,
        capture_token_entropy=False, generation_timeout_seconds=60,
        embedding_min_similarity=0.6, embedding_calibration=env,
    ).save(path)


def test_calibrate_embeddings_report_shows_drift_block_on_rerun(tmp_path: Path):
    """On a re-run (prior envelope present), the report surfaces a drift-vs-last-
    calibration block — either 'DRIFT DETECTED' or 'no drift'."""
    profile_path = tmp_path / "model_profile.json"
    _seed_envelope(profile_path)
    out = io.StringIO()
    _run_calibrate_embeddings(
        _config_with_model(),
        repo=str(tmp_path),
        profile_path=str(profile_path),
        client_factory=_factory(_DomainFakeClient()),
        out=out,
    )
    text = out.getvalue()
    assert "drift vs last calibration" in text


def test_calibrate_embeddings_no_drift_block_on_first_run(tmp_path: Path):
    """First run (no prior envelope) → no drift block (nothing to compare)."""
    out = io.StringIO()
    _run_calibrate_embeddings(
        _config_with_model(),
        repo=str(tmp_path),
        profile_path=str(tmp_path / "p.json"),
        client_factory=_factory(_DomainFakeClient()),
        out=out,
    )
    assert "drift vs last calibration" not in out.getvalue()


def test_calibrate_embeddings_drift_in_json_payload_on_rerun(tmp_path: Path):
    """The --json payload carries the drift comparison against the prior run."""
    profile_path = tmp_path / "model_profile.json"
    _seed_envelope(profile_path)
    out = io.StringIO()
    _run_calibrate_embeddings(
        _config_with_model(),
        repo=str(tmp_path),
        profile_path=str(profile_path),
        json_output=True,
        client_factory=_factory(_DomainFakeClient()),
        out=out,
    )
    payload = json.loads(out.getvalue())
    assert "drift" in payload
    assert "drifted" in payload["drift"]
    assert "reasons" in payload["drift"]


def _seed_floor(path: Path, *, model: str = "vibethink", floor: float = 0.71) -> None:
    """Write a prior profile carrying a known calibrated floor (as if a previous
    ``calibrate-embeddings`` had run)."""
    ModelProfile(
        model=model,
        max_tokens=8192,
        json_mode=True,
        capture_token_entropy=False,
        generation_timeout_seconds=60,
        embedding_min_similarity=floor,
    ).save(path)


def test_calibrate_embeddings_report_shows_prior_floor_on_rerun(tmp_path: Path):
    """A re-run reports the delta against the STORED floor, not the 0.35 default
    — the 'chosen ... (was X.XXX)' line must reflect the previous run."""
    profile_path = tmp_path / "model_profile.json"
    _seed_floor(profile_path, floor=0.71)
    out = io.StringIO()
    _run_calibrate_embeddings(
        _config_with_model(),
        repo=str(tmp_path),
        profile_path=str(profile_path),
        client_factory=_factory(_DomainFakeClient()),
        out=out,
    )
    text = out.getvalue()
    assert "(was 0.710)" in text  # the seeded prior floor, not 0.350


def test_calibrate_embeddings_dry_run_shows_prior_floor(tmp_path: Path):
    """``--dry-run`` exists to preview what would change, so the 'was X.XXX'
    delta must reflect the stored floor even though nothing is written."""
    profile_path = tmp_path / "model_profile.json"
    _seed_floor(profile_path, floor=0.71)
    out = io.StringIO()
    rc = _run_calibrate_embeddings(
        _config_with_model(),
        repo=str(tmp_path),
        profile_path=str(profile_path),
        dry_run=True,
        client_factory=_factory(_DomainFakeClient()),
        out=out,
    )
    assert rc == 0
    # The stored floor is unchanged (dry-run wrote nothing)...
    assert _load_json(profile_path)["embedding_min_similarity"] == 0.71
    # ...yet the report shows the real prior floor, not the 0.35 default.
    assert "(was 0.710)" in out.getvalue()


def test_calibrate_embeddings_first_run_shows_default_prior(tmp_path: Path):
    """No prior profile: the 'was' value is the 0.35 default (nothing to delta
    against). Confirms the prior-floor read is a no-op when there's no profile."""
    out = io.StringIO()
    _run_calibrate_embeddings(
        _config_with_model(),
        repo=str(tmp_path),
        profile_path=str(tmp_path / "p.json"),
        client_factory=_factory(_DomainFakeClient()),
        out=out,
    )
    assert "(was 0.350)" in out.getvalue()


# ---------------------------------------------------------------------------
# subcommand wiring (argparse → _run_calibrate_embeddings)
# ---------------------------------------------------------------------------


def test_calibrate_embeddings_subcommand_writes_default_path(tmp_path: Path, monkeypatch):
    """The ``calibrate-embeddings`` subcommand routes through the real client
    builder; inject a fake so no network is touched and the default path is used."""
    from capybase.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "capybase.cli._real_embeddings_client", lambda _cfg, _emb: _DomainFakeClient()
    )
    rc = main(["--repo", str(tmp_path), "calibrate-embeddings"])
    assert rc == 0
    assert (tmp_path / DEFAULT_PROFILE_PATH).is_file()


def test_calibrate_embeddings_global_profile_flag_directs_write(tmp_path: Path, monkeypatch):
    """``--profile PATH`` tells calibrate-embeddings WHERE to write."""
    from capybase.cli import main

    custom = tmp_path / "elsewhere" / "emb-profile.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "capybase.cli._real_embeddings_client", lambda _cfg, _emb: _DomainFakeClient()
    )
    rc = main(["--repo", str(tmp_path), "--profile", str(custom), "calibrate-embeddings"])
    assert rc == 0
    assert custom.is_file()
    assert not (tmp_path / DEFAULT_PROFILE_PATH).is_file()


def test_calibrate_embeddings_json_flag_emits_json_on_stdout(tmp_path: Path, monkeypatch):
    from capybase.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "capybase.cli._real_embeddings_client", lambda _cfg, _emb: _DomainFakeClient()
    )
    rc = main(["--repo", str(tmp_path), "calibrate-embeddings", "--json"])
    assert rc == 0
