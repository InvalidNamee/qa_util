from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Account:
    username: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    model: str
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class Config:
    accounts: tuple[Account, ...]
    forum_url: str
    max_concurrency: int
    questions_per_account: int
    llm: LLMConfig
    generation_prompt: str = ""
    publish_interval_seconds: float = 10.0

    def redact(self, text: str) -> str:
        secrets = [self.llm.api_key, *(a.password for a in self.accounts)]
        for secret in sorted(set(secrets), key=len, reverse=True):
            if secret:
                text = text.replace(secret, "***")
        return text

    def public_dict(self) -> dict:
        result = asdict(self)
        for account in result["accounts"]:
            account["password"] = "***"
        result.pop("llm")
        return result


@dataclass
class Question:
    owner: str
    text: str
    url: str = ""
    key: str = ""
    answers: dict[str, str] = field(default_factory=dict)


@dataclass
class Snapshot:
    course_name: str
    count: int
    questions: list[Question]


@dataclass
class AccountResult:
    username: str
    published: int = 0
    covered: int = 0
    skipped: int = 0
    error: str = ""


@dataclass(frozen=True)
class AccountProgress:
    username: str
    phase: str = "等待开始"
    current: int = 0
    total: int = 0
    state: str = "waiting"
    detail: str = ""


@dataclass
class RunResult:
    status: str
    accounts: list[AccountResult] = field(default_factory=list)


class Stopped(Exception):
    """用户主动停止，不是执行失败。"""


def normalize(text: str) -> str:
    return " ".join(text.split())
