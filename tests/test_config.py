import json

import pytest

from qa_util.config import PROJECT_ROOT, load_config


def test_example_loads_and_redacts():
    config = load_config(PROJECT_ROOT / "config.example.json")
    public = config.public_dict()
    assert public["accounts"][0]["password"] == "***"
    assert "llm" not in public
    assert "填写密码" not in repr(config)
    assert config.publish_interval_seconds == 10


@pytest.mark.parametrize("field,value", [
    ("max_concurrency", 0), ("max_concurrency", True),
    ("questions_per_account", -2), ("accounts", []), ("forum_url", "invalid"),
    ("publish_interval_seconds", 0), ("publish_interval_seconds", True),
    ("publish_interval_seconds", "10"), ("publish_interval_seconds", float("inf")),
])
def test_invalid_config(tmp_path, field, value):
    raw = json.loads((PROJECT_ROOT / "config.example.json").read_text())
    raw[field] = value
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        load_config(path)


def test_repeated_username(tmp_path):
    raw = json.loads((PROJECT_ROOT / "config.example.json").read_text())
    raw["accounts"][1] = raw["accounts"][0]
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="重复"):
        load_config(path)


def test_old_config_uses_default_publish_interval(tmp_path):
    raw = json.loads((PROJECT_ROOT / "config.example.json").read_text())
    raw.pop("publish_interval_seconds")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    assert load_config(path).publish_interval_seconds == 10
    raw["publish_interval_seconds"] = 15.5
    path.write_text(json.dumps(raw))
    assert load_config(path).publish_interval_seconds == 15.5


def test_llm_is_loaded_from_dotenv_and_reload_reads_edits(tmp_path):
    env = tmp_path / ".env"
    env.write_text('LLM_BASE_URL=https://llm.example.test/v1/\nLLM_MODEL=model-one\nLLM_API_KEY="env-key"\n')
    config = load_config(PROJECT_ROOT / "config.example.json")
    assert config.llm.base_url == "https://llm.example.test/v1"
    assert config.llm.model == "model-one" and config.llm.api_key == "env-key"
    env.write_text('LLM_BASE_URL=https://other.example.test/v1\nLLM_MODEL=model-two\nLLM_API_KEY=changed-key\n')
    assert load_config(PROJECT_ROOT / "config.example.json").llm.model == "model-two"


@pytest.mark.parametrize("missing", ["LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"])
def test_missing_llm_env_is_reported_without_using_json_credentials(tmp_path, monkeypatch, missing):
    raw = json.loads((PROJECT_ROOT / "config.example.json").read_text())
    raw["llm"] = {"base_url": "https://old.example.test", "model": "old", "api_key": "old-key"}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    monkeypatch.delenv(missing)
    with pytest.raises(ValueError, match=missing):
        load_config(path)
