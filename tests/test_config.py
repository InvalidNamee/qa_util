import json

import pytest

from comment_util.config import PROJECT_ROOT, load_config


def test_example_loads_and_redacts():
    config = load_config(PROJECT_ROOT / "config.example.json")
    public = config.public_dict()
    assert public["accounts"][0]["password"] == "***"
    assert public["llm"]["api_key"] == "***"
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
