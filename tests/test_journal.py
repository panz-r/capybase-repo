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


# ---------------------------------------------------------------------------
# FR1a — store_comment_artifact (content-addressed comment-pass artifacts)
# ---------------------------------------------------------------------------


def test_store_comment_artifact_is_content_addressed(tmp_path):
    """Identical content → same key (dedup); different content → different key."""
    from capybase.session import SessionPaths
    from capybase.journal import Journal
    p = SessionPaths("fr1a", tmp_path)
    p.mkdirs()
    j = Journal(p)
    key1, path1 = j.store_comment_artifact("prompt", "hello world")
    key2, path2 = j.store_comment_artifact("prompt", "hello world")
    key3, path3 = j.store_comment_artifact("prompt", "different content")
    assert key1 == key2  # same content → same key
    assert path1 == path2  # same path (dedup)
    assert key1 != key3  # different content → different key
    assert path1.exists() and path3.exists()


def test_store_comment_artifact_kind_subdir(tmp_path):
    """Each kind gets its own subdir under comment_artifacts/."""
    from capybase.session import SessionPaths
    from capybase.journal import Journal
    p = SessionPaths("fr1a2", tmp_path)
    p.mkdirs()
    j = Journal(p)
    j.store_comment_artifact("prompt", "a")
    j.store_comment_artifact("response", "b")
    j.store_comment_artifact("ledger", "c")
    assert (p.comment_artifacts / "prompt").is_dir()
    assert (p.comment_artifacts / "response").is_dir()
    assert (p.comment_artifacts / "ledger").is_dir()


def test_store_comment_artifact_custom_ext(tmp_path):
    """JSON artifacts use .json ext; text artifacts use .txt."""
    from capybase.session import SessionPaths
    from capybase.journal import Journal
    p = SessionPaths("fr1a3", tmp_path)
    p.mkdirs()
    j = Journal(p)
    key, path = j.store_comment_artifact("jury_verdict", '{"v":"ok"}', ext="json")
    assert path.suffix == ".json"
    assert path.read_text() == '{"v":"ok"}'


def test_store_comment_artifact_returns_key_for_replay(tmp_path):
    """The returned key is the sha256 prefix — the replay key for the jury."""
    import hashlib
    from capybase.session import SessionPaths
    from capybase.journal import Journal
    p = SessionPaths("fr1a4", tmp_path)
    p.mkdirs()
    j = Journal(p)
    content = "the frozen code buffer"
    expected = hashlib.sha256(content.encode()).hexdigest()[:16]
    key, _ = j.store_comment_artifact("frozen_code", content)
    assert key == expected


def test_session_paths_has_comment_artifacts(tmp_path):
    """SessionPaths exposes comment_artifacts alongside prompts/responses."""
    from capybase.session import SessionPaths
    p = SessionPaths("fr1a5", tmp_path)
    assert hasattr(p, "comment_artifacts")
    assert p.comment_artifacts.name == "comment_artifacts"
    p.mkdirs()
    assert p.comment_artifacts.exists()
