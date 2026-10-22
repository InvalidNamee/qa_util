import copy
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from qa_util.control import Control
from qa_util.models import Account, Config, LLMConfig, Question, Snapshot


@pytest.fixture(autouse=True)
def llm_environment(tmp_path, monkeypatch):
    monkeypatch.setattr("qa_util.config.ENV_FILE", tmp_path / ".env")
    for key, value in {"LLM_BASE_URL": "https://api.example.test/v1", "LLM_MODEL": "model",
                       "LLM_API_KEY": "secret-api-key"}.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def config():
    return Config(
        tuple(Account(name, "password-" + name) for name in "abcdef"),
        "https://qah5.zhihuishu.com/qa.html#/web/home/123?recruitId=456&role=2",
        2, 2, LLMConfig("https://api.example.test/v1", "model", "secret-api-key"),
    )


@pytest_asyncio.fixture
async def browser():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        yield browser
        await browser.close()


class FakePool:
    def __init__(self, config, control=None, log=None):
        self.config = config
        self.control = control or Control()
        self.active = 0
        self.maximum = 0
        self.released = 0
        self.rows = {a.username: [] for a in config.accounts}
        self.answers = []
        self.posts = []
        self.fail_preflight = set()
        self.fail_question = set()
        self.fail_answer = set()
        self.no_button = set()
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    @asynccontextmanager
    async def session(self, account):
        import asyncio
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        await asyncio.sleep(0.001)
        try:
            yield FakeForum(self, account.username)
        finally:
            self.active -= 1
            self.released += 1

    def seed(self, owner, count):
        self.rows[owner] = [Question(owner, f"{owner}的旧问题{i}？", f"https://fixture/{owner}/{i}")
                            for i in range(count)]


class FakeForum:
    def __init__(self, pool, username):
        self.pool, self.username = pool, username

    async def snapshot(self, limit):
        if self.username in self.pool.fail_preflight:
            raise RuntimeError("预检失败")
        rows = self.pool.rows[self.username]
        return Snapshot("测试课程", len(rows), copy.deepcopy(rows[:limit]))

    async def ensure_question(self, text, target):
        if self.username in self.pool.fail_question:
            raise RuntimeError("发帖失败")
        rows = self.pool.rows[self.username]
        if len(rows) >= target or any(q.text == text for q in rows):
            return False
        self.pool.posts.append((self.username, text))
        rows.insert(0, Question(self.username, text, f"https://fixture/{self.username}/new{len(rows)}"))
        return True

    async def answer(self, url, text):
        if self.username in self.pool.fail_answer:
            raise RuntimeError("回答失败")
        assert all(len(rows) >= self.pool.config.questions_per_account for rows in self.pool.rows.values())
        self.pool.answers.append((self.username, url, text))
        return (self.username, url) not in self.pool.no_button


class FakeGenerator:
    async def generate(self, course, questions):
        users = {q.owner for q in questions}
        for q in questions:
            q.text = q.text or f"{course}新问题{q.key}？"
            q.answers = {user: f"{user}回答{q.key}" for user in users - {q.owner}}
        return questions
