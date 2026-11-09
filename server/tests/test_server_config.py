from qa_util_server.config import load_server_settings


def test_server_dotenv_and_reload(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    monkeypatch.setattr("qa_util_server.config.ENV_FILE", env)
    env.write_text("LLM_BASE_URL=https://llm.test/v1/\nLLM_MODEL=model1\nLLM_API_KEY=private-key\n"
                   "SERVER_ADMIN_PASSWORD=admin-pass\nSERVER_SECRET_KEY=cookie-secret\nPUBLISH_INTERVAL_SECONDS=15\n")
    # load_dotenv 写入环境；用 monkeypatch 登记以便测试结束恢复。
    for key in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "SERVER_ADMIN_PASSWORD", "SERVER_SECRET_KEY", "PUBLISH_INTERVAL_SECONDS"):
        monkeypatch.setenv(key, "old-value")
    settings = load_server_settings()
    assert settings.llm.model == "model1" and settings.llm.base_url == "https://llm.test/v1"
    assert settings.publish_interval_seconds == 15
    monkeypatch.delenv("SERVER_DATA_DIRECTORY", raising=False)
    assert load_server_settings().data_directory == tmp_path / "server-data"
    assert "private-key" not in repr(settings) and "admin-pass" not in repr(settings)
    env.write_text(env.read_text().replace("model1", "model2"))
    assert load_server_settings().llm.model == "model2"


def test_explicit_env_file_keeps_data_beside_config_after_changing_directory(tmp_path, monkeypatch):
    config_directory = tmp_path / "settings"
    config_directory.mkdir()
    env = config_directory / ".env"
    values = {"LLM_BASE_URL": "https://llm.test/v1", "LLM_MODEL": "model", "LLM_API_KEY": "key",
              "SERVER_ADMIN_PASSWORD": "admin", "SERVER_SECRET_KEY": "secret",
              "SERVER_DATA_DIRECTORY": "data", "PUBLISH_INTERVAL_SECONDS": "10"}
    for key in values:
        monkeypatch.setenv(key, "old-value")
    env.write_text("\n".join(f"{key}={value}" for key, value in values.items()))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    settings = load_server_settings(env)
    assert settings.llm.model == "model"
    assert settings.data_directory == config_directory / "data"


