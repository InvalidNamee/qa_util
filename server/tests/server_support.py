import pytest

from qa_util_server.config import ServerSettings
from qa_util_server.models import LLMConfig


@pytest.fixture
def llm():
    return LLMConfig("https://api.example.test/v1", "model", "secret-api-key")


@pytest.fixture
def settings(tmp_path, llm):
    return ServerSettings(tmp_path / "server", "admin-password", "session-secret", llm)


class FakeGenerator:
    def __init__(self):
        self.calls = []
        self.number = 0
        self.fail = False

    def questions(self, course, existing, count):
        self.calls.append(("questions", count))
        if self.fail:
            raise RuntimeError("假 API 生成失败")
        result = []
        for _ in range(count):
            self.number += 1
            result.append(f"{course}的第{self.number}个概念究竟是什么？")
        return result

    def answers(self, course, question, count):
        self.calls.append(("answers", count))
        if self.fail:
            raise RuntimeError("假 API 生成失败")
        return [f"{question}的第{i+1}份回答包含可用等原样文字。" for i in range(count)]


@pytest.fixture
def app(settings):
    from qa_util_server.app import create_app
    instance = create_app(settings, generator=FakeGenerator(), start_worker=False)
    instance.config["TESTING"] = True
    yield instance
    instance.extensions["qa_worker"].close()
    for handler in instance.extensions["qa_logger"].handlers[:]:
        handler.close()
        instance.extensions["qa_logger"].removeHandler(handler)


@pytest.fixture
def service(app):
    return app.extensions["qa_service"]


def forum_url(course=123):
    return f"https://qah5.zhihuishu.com/qa.html#/web/home/{course}?role=2&recruitId=456"


def seed(service, task_id, aid, sid, count, *, target=1, answers=0, course=123, my_answers=None):
    questions = [{"text": f"账号{aid}的旧问题{i}是什么？",
                  "url": f"https://qah5.zhihuishu.com/qa.html#/web/questionDetail/{course}/{aid*100+i}?role=2&recruitId=456",
                  "answer_count": answers} for i in range(min(count, target))]
    if my_answers is None:
        my_answers = service.database.rows("SELECT account_answer_target FROM tasks WHERE id=?", (task_id,))[0]["account_answer_target"]
    service.sync(aid, sid, task_id, {"course_name": "测试课程", "question_count": count,
                                   "my_answer_count": my_answers, "questions": questions})
    return questions


def enroll(service, name):
    aid, token = service.create_account(name)
    sid = service.open_session(aid, "device-" + name, name)["session_id"]
    return aid, sid, token
