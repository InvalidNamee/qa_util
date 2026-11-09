import asyncio
import json

import httpx

from qa_util.control import Control
from qa_util.models import BrowserResult, Question, QuestionState, Snapshot


class FastControl(Control):
    async def sleep(self, seconds=0.2):
        self.check()
        await asyncio.sleep(min(seconds, 0.01))
        self.check()


def flask_transport(app, intercept=None):
    def handler(request):
        if intercept:
            early = intercept(request)
            if early is not None:
                return early
        with app.test_client() as client:
            response = client.open(request.url.path, method=request.method, data=request.content,
                                   headers={**dict(request.headers), "Content-Type": "application/json"})
            return httpx.Response(response.status_code, content=response.data,
                                  headers={"Content-Type": "application/json"})
    return httpx.MockTransport(handler)


class Website:
    def __init__(self):
        self.questions = []
        self.published = []
        self.fail_after_answer = False
        self.captcha_user = None
        self.captcha_release = asyncio.Event()
        self.historical_answers = {}

    def add(self, forum_url, owner, content, answers=0):
        url = forum_url.replace("/home/", "/questionDetail/")
        route, separator, query = url.partition("?")
        url = route + "/" + str(len(self.questions) + 1) + (separator + query if separator else "")
        row = {"forum": forum_url, "owner": owner, "text": content, "url": url, "count": answers, "authors": set()}
        self.questions.insert(0, row)
        return row

    def factory(self, *args, **kwargs):
        return FakeBrowser(*args, **kwargs, site=self)


class FakeBrowser:
    def __init__(self, config, control, log, *, debug=None, output_directory=None, before_send=None, site):
        self.config, self.control, self.before_send, self.site = config, control, before_send, site

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def open(self, task):
        return FakeForum(self, task)

    async def save_login(self):
        pass

    async def failure_snapshot(self, error):
        pass


class FakeForum:
    def __init__(self, browser, task):
        self.browser, self.task = browser, task
        self.site = browser.site
        self.user = browser.config.account.username

    async def snapshot(self, limit, *, urls=True):
        own = [q for q in self.site.questions if q["forum"] == self.task["forum_url"] and q["owner"] == self.user]
        forum = self.task["forum_url"]
        answered = self.site.historical_answers.get((forum, self.user), 0) + sum(
            self.user in q["authors"] for q in self.site.questions if q["forum"] == forum)
        return Snapshot("测试课程", len(own), [Question(self.user, q["text"], q["url"]) for q in own[:limit]], answered)

    async def inspect_question(self, url):
        row = next(q for q in self.site.questions if q["url"].split("?")[0] == url.split("?")[0])
        return QuestionState(row["text"], row["url"], row["count"], self.user in row["authors"],
                             row["owner"] != self.user and self.user not in row["authors"])

    async def find_question(self, content, limit):
        row = next((q for q in self.site.questions if q["owner"] == self.user and q["text"] == content
                    and q["forum"] == self.task["forum_url"]), None)
        return await self.inspect_question(row["url"]) if row else None

    async def post_question(self, content, *, lookback=50):
        await self.browser.before_send()
        if self.site.captcha_user == self.user:
            self.browser.control.status(self.user, "等待人工验证")
            while not self.site.captcha_release.is_set():
                await self.browser.control.sleep()
            self.browser.control.status(self.user, "")
        row = self.site.add(self.task["forum_url"], self.user, content)
        self.site.published.append(("question", self.user, row["url"]))
        return BrowserResult("posted", await self.inspect_question(row["url"]))

    async def answer(self, url, content, *, answer_target=None):
        actual = await self.inspect_question(url)
        if answer_target is not None and actual.answer_count >= answer_target:
            return BrowserResult("threshold_met", actual)
        if actual.is_answered:
            return BrowserResult("already_answered", actual)
        await self.browser.before_send()
        row = next(q for q in self.site.questions if q["url"].split("?")[0] == url.split("?")[0])
        row["authors"].add(self.user)
        row["count"] += 1
        self.site.published.append(("answer", self.user, url))
        if self.site.fail_after_answer:
            self.site.fail_after_answer = False
            raise RuntimeError("模拟已发布但未取得返回")
        return BrowserResult("posted", await self.inspect_question(url))
