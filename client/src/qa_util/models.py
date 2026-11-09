from dataclasses import dataclass, field


@dataclass(frozen=True)
class Account:
    username: str
    password: str = field(repr=False)


@dataclass
class Question:
    owner: str
    text: str
    url: str = ""


@dataclass
class Snapshot:
    course_name: str
    count: int
    questions: list[Question]
    answer_count: int = 0


@dataclass(frozen=True)
class QuestionState:
    text: str
    url: str
    answer_count: int
    is_answered: bool
    can_answer: bool


@dataclass(frozen=True)
class BrowserResult:
    status: str
    question: QuestionState | None = None


class Stopped(Exception):
    """用户主动停止，不是执行失败。"""


def normalize(text: str) -> str:
    return " ".join(text.split())
