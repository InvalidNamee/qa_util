import json
import math
from pathlib import Path
from urllib.parse import urlsplit

from .models import Account, Config, LLMConfig

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config.json"


def load_config(path: str | Path) -> Config:
    raw = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("配置必须是 JSON 对象")

    def required(obj, key):
        value = obj.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} 不能为空")
        return value

    rows = raw.get("accounts")
    if not isinstance(rows, list) or not rows or not all(isinstance(a, dict) for a in rows):
        raise ValueError("accounts 必须是非空账号列表")
    accounts = tuple(Account(required(a, "username").strip(), required(a, "password")) for a in rows)
    if len({a.username for a in accounts}) != len(accounts):
        raise ValueError("accounts 中的 username 不能重复")
    for key in ("max_concurrency", "questions_per_account"):
        if type(raw.get(key)) is not int or raw[key] < 1:
            raise ValueError(f"{key} 必须是正整数")
    llm = raw.get("llm")
    if not isinstance(llm, dict):
        raise ValueError("缺少 llm 配置")
    forum_url = required(raw, "forum_url").strip()
    base_url = required(llm, "base_url").strip().rstrip("/")
    for url in (forum_url, base_url):
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("forum_url 和 base_url 必须是完整 HTTP(S) 链接")
    prompt = raw.get("generation_prompt", "")
    if not isinstance(prompt, str):
        raise ValueError("generation_prompt 必须是字符串")
    interval = raw.get("publish_interval_seconds", 10)
    if type(interval) not in (int, float) or not math.isfinite(interval) or interval <= 0:
        raise ValueError("publish_interval_seconds 必须是正数（秒）")
    return Config(
        accounts, forum_url, raw["max_concurrency"], raw["questions_per_account"],
        LLMConfig(base_url, required(llm, "model").strip(), required(llm, "api_key")), prompt, float(interval),
    )
