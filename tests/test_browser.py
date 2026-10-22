import asyncio
import json
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from comment_util.browser import (PUBLISH_URL, BrowserPool, CaptchaGuard, Forum, PublishPacer,
                                  canonical_url, institution_login_url)
from comment_util.control import Control
from comment_util.models import Account, Stopped


@pytest.fixture
def site():
    state = {"questions": [], "answers": [], "posts": [], "question_mode": "", "answer_mode": ""}
    html = (Path(__file__).parent / "fixtures" / "forum.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, body, kind="application/json"):
            self.send_response(200)
            self.send_header("Content-Type", kind + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if urlsplit(self.path).path == "/creditqa/gateway/t/v1/web/qa/getQuestionInfo":
                question_id = parse_qs(urlsplit(self.path).query)["id"][0]
                q = next(q for q in state["questions"] if str(q["id"]) == question_id)
                user = self.headers.get("Cookie", "user=a").split("user=")[-1].split(";")[0]
                self.reply(json.dumps({"status": "200", "rt": {"questionInfo": {
                    "questionId": q["id"], "courseId": 123, "content": q["text"],
                    "isAnswer": any(a["id"] == question_id and a["user"] == user for a in state["answers"]),
                }}}).encode())
            elif self.path == "/creditqa/gateway/t/v1/web/qa/myParticipateQaNum":
                user = self.headers.get("Cookie", "user=a").split("user=")[-1].split(";")[0]
                body = {**state, "user": user, "answered": [a["id"] for a in state["answers"] if a["user"] == user]}
                self.reply(json.dumps(body).encode())
            else:
                self.reply(html if self.path.startswith("/qa.html") else b"<html></html>", "text/html")

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            user = self.headers.get("Cookie", "user=a").split("user=")[-1].split(";")[0]
            state.setdefault("send_times", []).append({"time": time.monotonic(), "path": self.path, "user": user})
            if state.get("require_publish_captcha") and not state.get("captcha_challenged"):
                state["captcha_challenged"] = True
                self.reply(b'{"status":"400","code":1010,"msg":"captcha required"}')
                return
            if self.path.endswith("/qa/saveQuestion"):
                state["posts"].append(body["text"])
                state["questions"].insert(0, {"id": len(state["questions"]) + 1, "owner": user, "text": body["text"]})
            else:
                state["answers"].append({**body, "user": user})
            self.reply(b'{"status":"200","msg":"request accepted","rt":{"id":"saved"}}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    yield origin, state
    server.shutdown()
    server.server_close()
    thread.join()


async def forum_for(browser, origin, *, user="a", control=None, logs=None, debug=None):
    context = await browser.new_context(viewport={"width": 1440, "height": 1000})
    await context.add_cookies([{"name": "user", "value": user, "url": origin}])
    page = await context.new_page()
    page.set_default_timeout(1000)
    control = control or Control()
    logs = logs if logs is not None else []
    guard = CaptchaGuard(context, control, logs.append, user)
    await guard.start()
    forum = Forum(page, Account(user, "test-password"), origin + "/qa.html#/web/home/123?role=2&recruitId=456",
                  control, logs.append, guard, debug=debug)
    return forum, guard, context


async def cleanup(guard, context):
    await guard.close()
    await context.close()


async def test_snapshot_scrolls_and_closes_popup_tabs(browser, site):
    origin, state = site
    state["questions"] = [dict(id=i, owner="a", text=f"问题{i}？") for i in range(5, 0, -1)]
    forum, guard, context = await forum_for(browser, origin)
    try:
        snapshot = await forum.snapshot(4)
        assert snapshot.count == 5 and snapshot.course_name == "测试课程"
        assert [q.text for q in snapshot.questions] == [f"问题{i}？" for i in (5, 4, 3, 2)]
        assert all("questionDetail/123/" in q.url for q in snapshot.questions)
        assert len(context.pages) == 1
        assert not guard.epoch
    finally:
        await cleanup(guard, context)


@pytest.mark.parametrize("count", [0, 1])
async def test_home_loaded_statistics_do_not_wait_for_unrelated_requests(browser, site, monkeypatch, count):
    origin, state = site
    state["background_request"] = True
    state["questions"] = [dict(id=i, owner="a", text=f"已有课程问题{i}？") for i in range(count)]
    forum, guard, context = await forum_for(browser, origin)
    release = asyncio.Event()
    started = asyncio.Event()

    async def background(route):
        started.set()
        await release.wait()
        await route.fulfill(body="{}")

    await context.route("**/api/background", background)
    original_wait = forum.wait

    async def quick_wait(predicate, description, *, timeout=30):
        return await original_wait(predicate, description, timeout=min(timeout, 2))

    monkeypatch.setattr(forum, "wait", quick_wait)
    try:
        course, actual = await forum._home()
        assert course == "测试课程" and actual == count
        assert started.is_set() and not release.is_set()
        assert not state["posts"] and not state["answers"]
    finally:
        release.set()
        await cleanup(guard, context)


async def test_home_waits_for_current_counts_instead_of_initial_zero(browser, site):
    origin, state = site
    state["questions"] = [dict(id=1, owner="a", text="已有课程问题？")]
    forum, guard, context = await forum_for(browser, origin)
    release = asyncio.Event()
    started = asyncio.Event()

    async def counts(route):
        started.set()
        await release.wait()
        await route.continue_()

    await context.route("**/qa/myParticipateQaNum", counts)
    task = asyncio.create_task(forum._home())
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        await asyncio.sleep(0.6)
        assert await forum.page.locator(".qa_jion_num").inner_text() == "0"
        assert not task.done()
        release.set()
        assert await asyncio.wait_for(task, timeout=4) == ("测试课程", 1)
        assert not state["posts"] and not state["answers"]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await cleanup(guard, context)


async def test_vnk_hash_change_reuses_loaded_counts_without_extra_refresh(browser, site):
    origin, state = site
    state["reuse_home_counts"] = True
    state["questions"] = [dict(id=1, owner="a", text="已有课程问题？")]
    forum, guard, context = await forum_for(browser, origin)
    requests = []
    try:
        assert await forum._home() == ("测试课程", 1)
        await forum.page.evaluate("history.replaceState(null,'',location.href+'&VNK=initial')")
        forum.page.on("request", lambda request: requests.append(request))
        assert await asyncio.wait_for(forum._home(), timeout=4) == ("测试课程", 1)
        assert not requests
        assert await forum.page.evaluate("performance.getEntriesByType('navigation')[0].type") == "navigate"
    finally:
        await cleanup(guard, context)


@pytest.mark.parametrize("mode", ["", "before", "after"])
async def test_question_captcha_reconciles_without_duplicate_post(browser, site, mode):
    origin, state = site
    state["question_mode"] = mode
    logs = []
    forum, guard, context = await forum_for(browser, origin, logs=logs)
    task = asyncio.create_task(forum.ensure_question("新的课程问题？", 1))
    try:
        if mode:
            popup = forum.page.locator(".yidun_popup")
            await popup.wait_for(state="visible", timeout=15_000)
            await asyncio.sleep(0.5)
            assert not task.done()
            assert len(state["posts"]) == (1 if mode == "after" else 0)
            await forum.page.locator("#manual-solve").click()
        assert await asyncio.wait_for(task, timeout=20)
        assert state["posts"] == ["新的课程问题？"]
        assert not await forum.ensure_question("新的课程问题？", 1)
        assert state["posts"] == ["新的课程问题？"]
        if mode:
            assert sum("等待人工" in message for message in logs) == 1
    finally:
        await cleanup(guard, context)
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("mode", ["", "before", "after"])
async def test_answer_captcha_and_already_answered_skip(browser, site, mode):
    origin, state = site
    state["questions"] = [dict(id=1, owner="b", text="其他同学的问题？")]
    state["answer_mode"] = mode
    forum, guard, context = await forum_for(browser, origin)
    url = origin + "/qa.html#/web/questionDetail/123/1?role=2&recruitId=456"
    task = asyncio.create_task(forum.answer(url, "这是对应题目的回答。"))
    try:
        if mode:
            await forum.page.locator(".yidun_popup").wait_for(state="visible", timeout=15_000)
            await asyncio.sleep(0.5)
            assert not task.done()
            await forum.page.locator("#manual-solve").click()
        assert await asyncio.wait_for(task, timeout=20)
        assert len(state["answers"]) == 1
        assert not await forum.answer(url, "不应重复发布")
        assert len(state["answers"]) == 1
    finally:
        await cleanup(guard, context)
        await asyncio.gather(task, return_exceptions=True)


async def test_three_answers_on_router_that_reuses_previous_detail(browser, site):
    origin, state = site
    state["reuse_detail"] = True
    state["questions"] = [dict(id=i, owner="b", text=f"问题{i}？") for i in range(1, 4)]
    forum, guard, context = await forum_for(browser, origin)
    try:
        for i in range(1, 4):
            url = origin + f"/qa.html#/web/questionDetail/123/{i}?role=2&recruitId=456"
            assert await forum.answer(url, f"问题{i}的回答"), f"第{i}题被上一题的状态误跳过"
        assert [(a["id"], a["text"]) for a in state["answers"]] == [
            (str(i), f"问题{i}的回答") for i in range(1, 4)
        ]
    finally:
        await cleanup(guard, context)


async def test_publish_interval_is_shared_across_question_answer_and_new_context(config, site, monkeypatch):
    origin, state = site
    state["questions"] = [dict(id=1, owner="b", text="其他同学的问题？")]
    config = replace(config, accounts=config.accounts[:1], forum_url=origin + "/qa.html#/web/home/123",
                     publish_interval_seconds=1.5)
    logs = []

    async def no_auth(self, *, reuse=False):
        await self.goto(self.home_url)

    monkeypatch.setattr(Forum, "login", no_auth)
    async with BrowserPool(config, Control(), logs.append, headless=True) as pool:
        async with pool.session(config.accounts[0]) as forum:
            await forum.page.context.add_cookies([{"name": "user", "value": "a", "url": origin}])
            assert await forum.ensure_question("准备提出的新问题？", 1)
        assert not pool.browser.contexts
        async with pool.session(config.accounts[0]) as forum:
            assert await forum.answer(origin + "/qa.html#/web/questionDetail/123/1", "这是别人的题目的回答")
            assert not await forum.answer(origin + "/qa.html#/web/questionDetail/123/1", "不应重复")
    sends = state["send_times"]
    assert len(sends) == 2
    assert sends[0]["path"].endswith("saveQuestion") and sends[1]["path"].endswith("saveAnswer")
    assert sends[1]["time"] - sends[0]["time"] >= 1.45
    assert any("发布间隔" in line for line in logs)


async def test_captcha_callback_obeys_publish_interval(browser, site):
    origin, state = site
    state["questions"] = [dict(id=1, owner="b", text="需要人工验证的问题？")]
    state["require_publish_captcha"] = True
    logs = []
    forum, guard, context = await forum_for(browser, origin, logs=logs)
    pacer = PublishPacer(1.5, forum.control, logs.append, "a")
    forum.publish_interval = pacer.interval
    await context.route(PUBLISH_URL, lambda route: pacer.send(route, guard))
    task = asyncio.create_task(forum.answer(origin + "/qa.html#/web/questionDetail/123/1", "人工验证后重新提交的回答"))
    try:
        await forum.page.locator(".yidun_popup").wait_for(state="visible", timeout=5000)
        await forum.page.locator("#manual-solve").click()
        assert await asyncio.wait_for(task, timeout=6)
        assert len(state["answers"]) == 1 and len(state["send_times"]) == 2
        assert state["send_times"][1]["time"] - state["send_times"][0]["time"] >= 1.45
    finally:
        await cleanup(guard, context)
        await asyncio.gather(task, return_exceptions=True)


async def test_publish_cooldown_only_blocks_own_account_and_stop_releases_it(browser, site):
    origin, state = site
    state["questions"] = [dict(id=1, owner="c", text="两个账号都能回答的问题？")]
    control = Control()
    waiting = asyncio.Event()
    forum, guard, context = await forum_for(browser, origin, control=control)
    other, other_guard, other_context = await forum_for(browser, origin, user="b")
    pacer = PublishPacer(60, control, lambda _: waiting.set(), "a")
    pacer.last_sent = time.monotonic()
    await context.route(PUBLISH_URL, lambda route: pacer.send(route, guard))
    task = asyncio.create_task(forum.answer(origin + "/qa.html#/web/questionDetail/123/1", "等待间隔的回答"))
    try:
        await asyncio.wait_for(waiting.wait(), timeout=4)
        assert await asyncio.wait_for(
            other.answer(origin + "/qa.html#/web/questionDetail/123/1", "另外账号可以立即回答"), timeout=4)
        assert not task.done()
        control.stop.set()
        with pytest.raises(Stopped):
            await asyncio.wait_for(task, timeout=2)
        assert [answer["user"] for answer in state["answers"]] == ["b"]
    finally:
        control.stop.set()
        await cleanup(guard, context)
        await cleanup(other_guard, other_context)
        await asyncio.gather(task, return_exceptions=True)


async def test_mismatched_question_data_is_not_skipped_or_answered(browser, site):
    origin, state = site
    state["questions"] = [dict(id=1, owner="b", text="目标题目？")]
    forum, guard, context = await forum_for(browser, origin)
    await context.route("**/qa/getQuestionInfo?**", lambda route: route.fulfill(
        json={"status": "200", "rt": {"questionInfo": {
            "questionId": 2, "courseId": 123, "content": "上一题？", "isAnswer": True,
        }}},
    ))
    try:
        with pytest.raises(RuntimeError, match="详情数据与目标题目不符"):
            await forum.answer(origin + "/qa.html#/web/questionDetail/123/1", "不能发到错误的题目")
        assert not state["answers"]
    finally:
        await cleanup(guard, context)


async def test_disappearing_button_alone_does_not_confirm_publication(browser, site):
    origin, state = site
    state["questions"] = [dict(id=1, owner="b", text="目标题目？")]
    forum, guard, context = await forum_for(browser, origin)

    async def rejected_answer(route):
        state["hide_answer_button"] = True
        await route.fulfill(json={"status": "200", "rt": {"id": "not-actually-saved"}})

    await context.route("**/qa/saveAnswer", rejected_answer)
    try:
        with pytest.raises(RuntimeError, match="提交后未确认本人已回答"):
            await forum.answer(origin + "/qa.html#/web/questionDetail/123/1", "未实际保存的回答")
        assert not state["answers"]
    finally:
        await cleanup(guard, context)


@pytest.mark.parametrize("http_status,payload", [
    (200, {"status": "400", "code": 1003, "msg": "操作频繁，请稍后再试"}),
    (503, {"msg": "服务暂不可用"}),
])
async def test_rejected_publication_reports_server_reason_without_waiting_for_dialog(
        browser, site, monkeypatch, http_status, payload):
    origin, state = site
    state["questions"] = [dict(id=1, owner="b", text="目标题目？")]
    forum, guard, context = await forum_for(browser, origin)
    await context.route("**/qa/saveAnswer", lambda route: route.fulfill(status=http_status, json=payload))
    original_wait = forum.wait

    async def bounded_wait(predicate, description, *, timeout=30):
        return await original_wait(predicate, description, timeout=min(timeout, 2))

    monkeypatch.setattr(forum, "wait", bounded_wait)
    try:
        with pytest.raises(RuntimeError, match=payload["msg"]):
            await forum.answer(origin + "/qa.html#/web/questionDetail/123/1", "用户已确认的回答")
        assert await forum.page.get_by_role("dialog", name="我来回答").is_visible()
        assert not state["answers"]
        assert forum.last_publish["http_status"] == http_status
    finally:
        await cleanup(guard, context)


async def test_accepted_answer_is_reconciled_even_when_editor_stays_open(browser, site):
    origin, state = site
    state["questions"] = [dict(id=1, owner="b", text="目标题目？")]
    state["keep_publish_dialog"] = True
    forum, guard, context = await forum_for(browser, origin)
    try:
        assert await asyncio.wait_for(
            forum.answer(origin + "/qa.html#/web/questionDetail/123/1", "实际保存的回答"), timeout=6)
        assert len(state["answers"]) == 1
        assert not await forum.page.get_by_role("dialog").count()
    finally:
        await cleanup(guard, context)


async def test_captcha_callback_submission_finishes_before_reconciliation(browser, site):
    origin, state = site
    state["questions"] = [dict(id=1, owner="b", text="目标题目？")]
    forum, guard, context = await forum_for(browser, origin)
    resumed = asyncio.Event()
    release = asyncio.Event()
    requests = 0

    async def publish(route):
        nonlocal requests
        requests += 1
        if requests == 1:
            await route.fulfill(json={"status": "400", "code": 1010, "msg": "需要验证码"})
        else:
            resumed.set()
            await release.wait()
            await route.continue_()

    await context.route("**/qa/saveAnswer", publish)
    task = asyncio.create_task(forum.answer(origin + "/qa.html#/web/questionDetail/123/1", "通过验证后的回答"))
    try:
        await forum.page.locator(".yidun_popup").wait_for(state="visible", timeout=5000)
        await forum.page.locator("#manual-solve").click()
        await asyncio.wait_for(resumed.wait(), timeout=3)
        await asyncio.sleep(0.8)
        assert not task.done() and not state["answers"]
        assert await forum.page.get_by_role("dialog", name="我来回答").is_visible()
        release.set()
        assert await asyncio.wait_for(task, timeout=6)
        assert len(state["answers"]) == 1 and requests == 2
    finally:
        release.set()
        await cleanup(guard, context)
        await asyncio.gather(task, return_exceptions=True)


async def test_browser_validation_error_is_reported_without_a_publish_request(browser, site):
    origin, state = site
    state["questions"] = [dict(id=1, owner="b", text="目标题目？")]
    forum, guard, context = await forum_for(browser, origin)
    try:
        await forum.goto(origin + "/qa.html#/web/questionDetail/123/1")
        await forum._detail_ready()
        await forum.click(forum.page.locator("div.my-answer-btn"))
        dialog = forum.page.get_by_role("dialog", name="我来回答")
        await dialog.locator(".up-btn").evaluate("""e => e.onclick = () => {
          const notice = document.createElement('div'); notice.className = 'el-message--error';
          notice.textContent = '字数不足'; document.body.appendChild(notice);
        }""")
        with pytest.raises(RuntimeError, match="页面未提交发布：字数不足"):
            await forum.submit(dialog)
        assert not state["answers"]
    finally:
        await cleanup(guard, context)


async def test_failed_publication_saves_page_and_sanitized_response(config, site, tmp_path, monkeypatch):
    origin, state = site
    state["questions"] = [dict(id=1, owner="b", text="目标题目？")]
    config = replace(config, forum_url=origin + "/qa.html#/web/home/123")

    async def no_auth(self, *, reuse=False):
        await self.goto(self.home_url)

    monkeypatch.setattr(Forum, "login", no_auth)
    async with BrowserPool(config, Control(), lambda _: None, headless=True, output_directory=tmp_path) as pool:
        with pytest.raises(RuntimeError, match="发布失败"):
            async with pool.session(config.accounts[0]) as forum:
                await forum.page.context.route("**/qa/saveAnswer", lambda route: route.fulfill(
                    json={"status": "400", "msg": "服务端拒绝 " + config.accounts[0].password}))
                await forum.answer(origin + "/qa.html#/web/questionDetail/123/1", "已确认内容")
        assert not pool.browser.contexts
    diagnostic = (tmp_path / "failure-a1.json").read_text()
    assert config.accounts[0].password not in diagnostic
    assert json.loads(diagnostic)["publish"]["status"] == "400"
    assert "Traceback" in json.loads(diagnostic)["traceback"]
    assert json.loads(diagnostic)["captcha"] == {"paused": False, "epoch": 0, "monitor_error": None}
    assert (tmp_path / "failure-a1.png").read_bytes().startswith(b"\x89PNG")
    assert not state["answers"]


async def test_debug_records_api_and_script_errors_without_payload_or_tokens(browser, site):
    origin, state = site
    state["questions"] = [dict(id=1, owner="b", text="目标题目？")]
    debug = []
    forum, guard, context = await forum_for(browser, origin, debug=debug.append)
    await context.route("**/qa/saveAnswer", lambda route: route.fulfill(json={
        "status": "400", "code": 1003, "msg": "发布频率受限",
        "token": "private-auth-token", "rt": {"cookie": "private-cookie"},
    }))
    try:
        with pytest.raises(RuntimeError, match="发布频率受限"):
            await forum.answer(origin + "/qa.html#/web/questionDetail/123/1", "不应出现在诊断日志中的回答正文")
        error_seen = asyncio.Event()
        forum.page.once("pageerror", lambda _: error_seen.set())
        await forum.page.evaluate("setTimeout(() => { throw new Error('页面初始化异常'); }, 0)")
        await asyncio.wait_for(error_seen.wait(), timeout=2)
        await forum.close_diagnostics()
        text = "\n".join(debug)
        assert "API 请求 POST /creditqa/gateway/t/v1/web/qa/saveAnswer" in text
        assert '"http_status": 200' in text and '"code": 1003' in text and "发布频率受限" in text
        assert '"questionId": 1' in text and '"isAnswer": false' in text
        assert "页面脚本异常：" in text and "页面初始化异常" in text
        assert "不应出现在诊断日志中的回答正文" not in text
        assert "private-auth-token" not in text and "private-cookie" not in text
        assert all(line.startswith("[a]") for line in debug)
    finally:
        await forum.close_diagnostics()
        await cleanup(guard, context)


async def test_hidden_captcha_in_ancestor_and_iframe_is_not_a_challenge(browser):
    context = await browser.new_context()
    await context.route("**/captcha-idle", lambda route: route.fulfill(body="<div></div>"))
    page = await context.new_page()
    guard = CaptchaGuard(context, Control(), lambda _: None, "a")
    try:
        for style in ("display:none", "opacity:0", "position:absolute;top:-1000000px"):
            await page.set_content(f'<div style="{style}"><div class="yidun_popup" style="width:300px;height:200px">验证</div></div>')
            assert await guard.detect() is None
        await page.set_content('<iframe style="opacity:0" srcdoc="<div class=yidun_popup>验证</div>"></iframe>')
        await page.wait_for_timeout(200)
        assert await guard.detect() is None
        await page.set_content('<div class="yidun_popup" style="width:300px;height:200px">验证</div>')
        assert await guard.detect() is page
    finally:
        await cleanup(guard, context)


async def test_one_account_wait_does_not_block_other_and_can_be_stopped(browser, site):
    origin, state = site
    state["question_mode"] = "before"
    control = Control()
    forum, guard, context = await forum_for(browser, origin, control=control)
    other, other_guard, other_context = await forum_for(browser, origin, user="b")
    task = asyncio.create_task(forum.ensure_question("等待验证的问题？", 1))
    try:
        await forum.page.locator(".yidun_popup").wait_for(state="visible", timeout=15_000)
        assert (await asyncio.wait_for(other.snapshot(1), timeout=5)).count == 0
        assert not task.done()
        control.stop.set()
        with pytest.raises(Stopped):
            await asyncio.wait_for(task, timeout=3)
        assert not state["posts"]
    finally:
        await cleanup(guard, context)
        await cleanup(other_guard, other_context)
        await asyncio.gather(task, return_exceptions=True)


async def test_captcha_wait_does_not_consume_regular_wait_timeout(browser):
    context = await browser.new_context()
    page = await context.new_page()
    await page.set_content('<div class="yidun_popup" style="height:100px">人工验证</div>')
    control = Control()
    guard = CaptchaGuard(context, control, lambda _: None, "a")
    forum = Forum(page, Account("a", "test"), "http://fixture", control, lambda _: None, guard)

    async def complete_later():
        await asyncio.sleep(0.6)
        await page.locator(".yidun_popup").evaluate("e=>e.remove()")

    solve = asyncio.create_task(complete_later())
    try:
        async def ready():
            return True
        assert await forum.wait(ready, "测试", timeout=0.1)
    finally:
        await solve
        await cleanup(guard, context)


async def test_institution_login_submits_hidden_standard_form(browser, site):
    origin, _ = site
    forum, guard, context = await forum_for(browser, origin)
    credentials = []

    async def cas(route):
        if route.request.method == "POST":
            credentials.append(route.request.post_data)
            await route.fulfill(status=302, headers={"Location": "https://hub.polymas.com/stu-hike/ctp/student-workspace"})
        else:
            await route.fulfill(content_type="text/html", body='''<iframe srcdoc="可见登录界面"></iframe>
              <form id="loginForm" method="POST" style="display:none"><input name="username">
              <input name="password" type="password"><input type="submit"></form>''')

    await context.route("https://cas.upc.edu.cn/**", cas)
    await context.route("https://hub.polymas.com/**", lambda route: route.fulfill(body="已授权"))
    try:
        await forum.login()
        assert len(credentials) == 1
        assert "username=a" in credentials[0] and "password=test-password" in credentials[0]
        assert forum.page.url.startswith(origin)
    finally:
        await cleanup(guard, context)


def test_urls_keep_course_context_and_strip_transient_parameters():
    url = "https://qah5.zhihuishu.com/qa.html#/web/questionDetail/123/9?VNK=abc&role=2&aid=7&recruitId=456"
    assert canonical_url(url) == "https://qah5.zhihuishu.com/qa.html#/web/questionDetail/123/9?recruitId=456&role=2"
    assert urlsplit(institution_login_url()).hostname == "cas.upc.edu.cn"


async def test_real_pool_reuses_only_its_own_account_storage(config, site, monkeypatch):
    origin, _ = site

    async def no_auth(self, *, reuse=False):
        pass

    monkeypatch.setattr(Forum, "login", no_auth)
    async with BrowserPool(config, Control(), lambda _: None, headless=True) as pool:
        async with pool.session(config.accounts[0]) as forum:
            await forum.page.context.add_cookies([{"name": "identity", "value": "a", "url": origin}])
            await forum.page.goto(origin + "/qa.html#/web/home/123")
            await forum.page.evaluate("localStorage.setItem('account-marker', 'a')")
        assert not pool.browser.contexts
        async with pool.session(config.accounts[1]) as forum:
            assert not await forum.page.context.cookies(origin)
            await forum.page.goto(origin + "/qa.html#/web/home/123")
            assert await forum.page.evaluate("localStorage.getItem('account-marker')") is None
        async with pool.session(config.accounts[0]) as forum:
            cookies = await forum.page.context.cookies(origin)
            assert cookies[0]["value"] == "a"
            await forum.page.goto(origin + "/qa.html#/web/home/123")
            assert await forum.page.evaluate("localStorage.getItem('account-marker')") == "a"
        assert not pool.browser.contexts
    assert not pool.browser.is_connected()


async def test_real_pool_stop_closes_browser_without_waiting_for_captcha(config, monkeypatch):
    control = Control()

    async def no_auth(self, *, reuse=False):
        pass

    monkeypatch.setattr(Forum, "login", no_auth)
    async with BrowserPool(config, control, lambda _: None, headless=True) as pool:
        async with pool.session(config.accounts[0]) as forum:
            await forum.page.set_content('<div class="yidun_popup" style="height:100px">验证</div>')
            control.stop.set()
            await asyncio.wait_for(pool.stop_task, timeout=3)
            assert not pool.browser.is_connected()
    assert not pool.browser.contexts
