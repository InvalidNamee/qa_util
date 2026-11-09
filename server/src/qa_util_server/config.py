import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

from .models import LLMConfig

ENV_FILE = Path(".env")


def http_url(value):
    if not isinstance(value, str) or urlsplit(value).scheme not in {"http", "https"} or not urlsplit(value).netloc:
        raise ValueError("需要完整的 HTTP(S) 链接")
    return value.rstrip("/")


def required(raw, key):
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} 不能为空")
    return value


@dataclass(frozen=True)
class ServerSettings:
    data_directory: Path
    admin_password: str = field(repr=False)
    secret_key: str = field(repr=False)
    llm: LLMConfig
    generation_prompt: str = ""
    publish_interval_seconds: float = 10


def load_server_settings(env_file=None):
    env_file = Path(env_file if env_file is not None else ENV_FILE).expanduser().resolve()
    load_dotenv(env_file, override=True)
    interval = float(os.environ.get("PUBLISH_INTERVAL_SECONDS", "10"))
    if not 0 < interval < float("inf"):
        raise ValueError("PUBLISH_INTERVAL_SECONDS 必须是正数")
    directory = Path(os.environ.get("SERVER_DATA_DIRECTORY", "server-data")).expanduser()
    if not directory.is_absolute():
        directory = env_file.parent / directory
    return ServerSettings(directory.resolve(), required(os.environ, "SERVER_ADMIN_PASSWORD"),
                          required(os.environ, "SERVER_SECRET_KEY"),
                          LLMConfig(http_url(required(os.environ, "LLM_BASE_URL")),
                                    required(os.environ, "LLM_MODEL"), required(os.environ, "LLM_API_KEY")),
                          os.environ.get("GENERATION_PROMPT", ""), interval)
