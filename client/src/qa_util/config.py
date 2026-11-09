import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .models import Account

def default_config():
    if not getattr(sys, "frozen", False):
        return Path("client.json")
    executable = Path(sys.executable)
    directory = executable.parent
    if sys.platform == "darwin" and directory.name == "MacOS" and directory.parent.name == "Contents":
        directory = directory.parent.parent.parent
    return directory / "client.json"


DEFAULT_CONFIG = default_config()


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
class ClientConfig:
    server_url: str
    access_token: str = field(repr=False)
    account: Account
    data_directory: Path

    def redact(self, text):
        for secret in sorted({self.access_token, self.account.password}, key=len, reverse=True):
            if secret:
                text = text.replace(secret, "***")
        return text

    def public_dict(self):
        return {"server_url": self.server_url, "access_token": "***",
                "account": {"username": self.account.username, "password": "***"},
                "data_directory": str(self.data_directory)}


def load_config(path=DEFAULT_CONFIG):
    path = Path(path).expanduser().resolve()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "accounts" in raw:
        raise ValueError("请使用单账号 client.json；旧 config.json 的迁移方式见 README")
    account = raw.get("account")
    if not isinstance(account, dict):
        raise ValueError("缺少 account")
    account = Account(required(account, "username").strip(), required(account, "password"))
    directory = Path(raw.get("data_directory", f"client-data/{account.username}")).expanduser()
    if not directory.is_absolute():
        directory = path.parent / directory
    return ClientConfig(http_url(required(raw, "server_url")), required(raw, "access_token"), account,
                        directory.resolve())
