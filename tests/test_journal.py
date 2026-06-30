import json

from capybase.config import Config
from capybase.journal import Journal
from capybase.session import SessionPaths


def test_journal_appends_events(tmp_path):
    paths = SessionPaths("sess1", tmp_path)
    j = Journal(paths)
    j.emit("session_started", {"mode": "test"})
    j.emit("conflict_detected", {"path": "a.py"}, step_index=1, path="a.py")
    events = j.read_events()
    assert len(events) == 2
    assert events[0].event_type == "session_started"
    assert events[0].seq == 1
    assert events[1].seq == 2
    assert events[1].step_index == 1
    assert events[1].path == "a.py"


def test_journal_artifacts(tmp_path):
    paths = SessionPaths("sess2", tmp_path)
    j = Journal(paths)
    p = j.write_artifact(paths.prompts, "u1.txt", "hello")
    assert p.exists()
    assert p.read_text() == "hello"


def test_config_load_defaults():
    cfg = Config()
    assert cfg.model.model == "vibethink"
    assert cfg.policy.supported_conflict_types == ["UU", "AU", "UA"]
    assert cfg.tests.pre_continue == "pytest"
    assert cfg.journal.enabled is True


def test_config_load_from_file(tmp_path):
    toml = tmp_path / "capybase.toml"
    toml.write_text(
        '[model]\nmodel = "custom"\nsamples = 3\n\n[policy]\nmax_retries_per_unit = 5\n'
    )
    cfg = Config.load(toml)
    assert cfg.model.model == "custom"
    assert cfg.model.samples == 3
    assert cfg.policy.max_retries_per_unit == 5
    assert cfg.source_path == str(toml)


def test_journal_events_are_json_serializable(tmp_path):
    paths = SessionPaths("sess3", tmp_path)
    j = Journal(paths)
    j.emit("tests_finished", {"stdout_tail": "abc"})
    line = paths.journal.read_text().splitlines()[0]
    obj = json.loads(line)
    assert obj["event_type"] == "tests_finished"
    assert "timestamp" in obj
