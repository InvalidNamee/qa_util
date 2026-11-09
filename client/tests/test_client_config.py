import json

import pytest

from qa_util.config import default_config, load_config
from qa_util.storage import ClientLock, private_json, read_json


def test_single_account_config_is_local_and_needs_no_llm(tmp_path, monkeypatch):
    for key in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / "client.json"
    path.write_text(json.dumps({"server_url": "http://127.0.0.1:8000/", "access_token": "private-token",
                                "account": {"username": "a", "password": "private-password"}}))
    config = load_config(path)
    assert config.server_url == "http://127.0.0.1:8000"
    assert config.data_directory == tmp_path / "client-data/a"
    assert config.account.username == "a"
    assert "private-token" not in repr(config) and "private-password" not in repr(config)
    assert "private-token" not in json.dumps(config.public_dict())
    assert config.redact("private-token private-password") == "*** ***"


def test_default_config_is_loaded_from_launch_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "client.json").write_text(json.dumps({
        "server_url": "http://server.test", "access_token": "token",
        "account": {"username": "a", "password": "pass"}}))
    assert load_config().data_directory == tmp_path / "client-data/a"


@pytest.mark.parametrize("platform,executable,expected", [
    ("darwin", "release/qa_util.app/Contents/MacOS/qa_util", "release/client.json"),
    ("win32", "release/qa_util/qa_util.exe", "release/qa_util/client.json"),
    ("linux", "release/qa_util/qa_util", "release/qa_util/client.json"),
])
def test_frozen_app_finds_config_outside_bundle(tmp_path, monkeypatch, platform, executable, expected):
    monkeypatch.setattr("qa_util.config.sys.frozen", True, raising=False)
    monkeypatch.setattr("qa_util.config.sys.platform", platform)
    monkeypatch.setattr("qa_util.config.sys.executable", str(tmp_path / executable))
    monkeypatch.chdir(tmp_path)
    assert default_config() == tmp_path / expected


@pytest.mark.parametrize("field,value", [("server_url", "file:///tmp/x"), ("access_token", ""),
                                          ("account", {}), ("accounts", [])])
def test_invalid_or_legacy_client_config_is_rejected(tmp_path, field, value):
    raw = {"server_url": "https://server.test", "access_token": "token", "account": {"username": "a", "password": "pass"}}
    raw[field] = value
    path = tmp_path / "client.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        load_config(path)


def test_private_state_atomic_write_and_single_instance(tmp_path):
    path = tmp_path / "state/login.json"
    private_json(path, {"cookie": "secret"})
    private_json(path, {"cookie": "new"})
    assert read_json(path) == {"cookie": "new"}
    assert path.stat().st_mode & 0o777 == 0o600
    with ClientLock(tmp_path):
        with pytest.raises(RuntimeError, match="已有客户端"):
            with ClientLock(tmp_path):
                pytest.fail("不能同时占用同一状态目录")
    with ClientLock(tmp_path):
        pass
