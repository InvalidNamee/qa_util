import asyncio
import json
import re
import time
from urllib.parse import urlencode, urlsplit, urlunsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

from .models import BrowserResult, Question, QuestionState, Snapshot, normalize
from .storage import private_json, read_json
from .urls import canonical_url


def institution_login_url() -> str:
    service = "http://upcsso.zhihuishu.com/authorize?" + urlencode({
        "portal": "14", "response_type": "code", "scope": "read",
        "client_id": "zhihuishuupc", "redirect_uri": "http://oc.zhihuishu.com/oauth/callback/upc",
        "state": "09876999",
    })
    return "https://cas.upc.edu.cn/cas/login?" + urlencode({"service": service})
DISPLAYED_JS = """(element) => {
  for (let node = element; node; node = node.parentElement) {
    const style = getComputedStyle(node);
    if (style.display === 'none' || ['hidden','collapse'].includes(style.visibility)
        || Number(style.opacity) === 0) return false;
  }
  const rect = element.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0 || rect.right <= 0 || rect.bottom <= 0
      || rect.left >= innerWidth || rect.top >= innerHeight) return false;
  for (let node = element.parentElement; node; node = node.parentElement) {
    const style = getComputedStyle(node);
    if ([style.overflow, style.overflowX, style.overflowY].some(v => ['hidden','clip'].includes(v))) {
      const outer = node.getBoundingClientRect();
      if (rect.right <= outer.left || rect.left >= outer.right
          || rect.bottom <= outer.top || rect.top >= outer.bottom) return false;
    }
  }
  return true;
}"""
CAPTCHA_JS = """() => {
  const visible = DISPLAYED;
  const candidates = [...document.querySelectorAll(
    '.yidun_popup,.yidun_modal,.yidun_popup__content,#tcaptcha_transform_dy,#j-captcha-mobile')];
  candidates.push(...[...document.querySelectorAll('iframe')].filter(el =>
    /captcha|yidun|turing|cstaticdun|necaptcha/i.test(el.getAttribute('src') || '')));
  return candidates.some(visible);
}""".replace("DISPLAYED", DISPLAYED_JS)


class CaptchaGuard:
    """监视与人工等待只作用于当前账号；epoch 用于识别需要重新对账的动作。"""

    def __init__(self, context, control, log, username):
        self.context, self.control, self.log, self.username = context, control, log, username
        self.epoch = 0
        self.paused = False
        self.failure = None
        self.task = None
        self.closing = False

    async def start(self):
        self.task = asyncio.create_task(self._watch())

    async def close(self):
        self.closing = True
        if self.task:
            await asyncio.gather(self.task, return_exceptions=True)

    async def detect(self):
        self.control.check()
        for page in self.context.pages:
            if page.is_closed():
                continue
            for frame in page.frames:
                try:
                    parent = frame
                    visible = True
                    while parent.parent_frame:
                        element = await parent.frame_element()
                        visible = await element.evaluate(DISPLAYED_JS)
                        await element.dispose()
                        if not visible:
                            break
                        parent = parent.parent_frame
                    if visible and await frame.evaluate(CAPTCHA_JS):
                        return page
                except PlaywrightError as error:
                    # 导航/弹窗销毁期间的观察可以在下一次200ms轮询重做。
                    if not any(word in str(error) for word in (
                        "Execution context was destroyed", "Cannot find context", "detached",
                        "has been closed", "most likely because of a navigation",
                    )):
                        raise
        return None

    async def _mark(self, page):
        if not self.paused:
            self.paused = True
            self.epoch += 1
            self.log(f"[{self.username}] 等待人工完成验证码，仅暂停此账号")
            self.control.status(self.username, "等待人工验证")
            await page.bring_to_front()

    async def _watch(self):
        try:
            while not self.closing:
                page = await self.detect()
                if page:
                    await self._mark(page)
                await self.control.sleep()
        except Exception as error:
            self.failure = error

    async def checkpoint(self) -> bool:
        self.control.check()
        if self.failure:
            raise self.failure
        page = await self.detect()
        if page:
            await self._mark(page)
        if not self.paused:
            return False
        clear_since = None
        while True:
            self.control.check()
            if self.failure:
                raise self.failure
            if await self.detect():
                clear_since = None
            elif clear_since is None:
                clear_since = time.monotonic()
            elif time.monotonic() - clear_since >= 0.4:
                break
            await self.control.sleep()
        self.paused = False
        self.control.status(self.username, "")
        self.log(f"[{self.username}] 验证窗口已关闭，重新核对当前状态")
        return True


PUBLISH_URL = re.compile(r"/qa/save(?:Question|Answer)(?:\?|$)")


class PublishPacer:
    """每个账号共用发布时钟，包含网页在人工验证后自行提交的请求。"""

    def __init__(self, interval, control, log, username, *, state_path=None, before_send=None):
        self.interval, self.control, self.log, self.username = interval, control, log, username
        self.last_sent = None
        self.lock = asyncio.Lock()
        self.state_path, self.before_send, self.failure = state_path, before_send, None
        if state_path:
            previous = read_json(state_path, {}).get("last_sent")
            if previous:
                self.last_sent = time.monotonic() - max(0, time.time() - previous)

    async def send(self, route, guard):
        try:
            if route.request.method != "POST":
                await route.continue_()
                return
            async with self.lock:
                remaining = 0 if self.last_sent is None else self.last_sent + self.interval - time.monotonic()
                if remaining > 0:
                    self.log(f"[{self.username}] 发布间隔，等待 {remaining:.1f} 秒")
                    self.control.status(self.username, "等待发送间隔")
                    await self.control.sleep(remaining)
                # 正常等待同样可停止；验证码出现时不释放待发送的请求。
                await guard.checkpoint()
                self.control.check()
                if self.before_send:
                    await self.before_send()
                self.control.status(self.username, "")
                self.last_sent = time.monotonic()
                if self.state_path:
                    private_json(self.state_path, {"last_sent": time.time()})
                await route.continue_()
        except Exception as error:
            self.failure = error
            try:
                await route.abort()
            except PlaywrightError:
                pass  # 停止时浏览器可能已经关闭；失败由等待发布结果的调用方接收。


class BrowserSession:
    """单账号、单浏览器上下文。持久登录状态只写客户端目录。"""

    def __init__(self, config, control, log, *, debug=None, output_directory=None, headless=False, before_send=None):
        self.config, self.control, self.log = config, control, log
        self.debug = lambda message: debug(config.redact(str(message))) if debug else None
        self.output_directory, self.headless = output_directory, headless
        self.before_send = before_send
        self.forum = None
        self.closing = False

    async def __aenter__(self):
        self.playwright = await async_playwright().start()
        try:
            self.browser = await self.playwright.chromium.launch(headless=self.headless)
            saved = read_json(self.config.data_directory / "login.json", {})
            if saved and saved.get("username") != self.config.account.username:
                raise ValueError("本地登录状态不属于当前账号，请使用独立数据目录")
            self.context = await self.browser.new_context(storage_state=saved.get("state"),
                                                         viewport={"width": 1440, "height": 1000})
            self.page = await self.context.new_page()
            self.page.set_default_timeout(1000)
            self.guard = CaptchaGuard(self.context, self.control, self.log, self.config.account.username)
            await self.guard.start()
            self.pacer = PublishPacer(10, self.control, self.log, self.config.account.username,
                                      state_path=self.config.data_directory / "cooldown.json", before_send=self.before_send)
            await self.context.route(PUBLISH_URL, lambda route: self.pacer.send(route, self.guard))
        except Exception:
            if hasattr(self, "browser"):
                await self.browser.close()
            await self.playwright.stop()
            raise
        self.stop_task = asyncio.create_task(self._watch_stop())
        return self

    async def _watch_stop(self):
        while not self.closing and not self.control.stop.is_set():
            await asyncio.sleep(0.2)
        if self.control.stop.is_set():
            await self.save_login()
            await self.browser.close()

    async def save_login(self):
        if self.browser.is_connected() and not self.page.is_closed():
            try:
                origins = await self.page.evaluate("""() => ['http:', 'https:'].includes(location.protocol)
                  ? [{origin: location.origin, localStorage: Object.entries(localStorage)
                       .map(([name,value]) => ({name,value}))}] : []""")
                private_json(self.config.data_directory / "login.json", {
                    "username": self.config.account.username,
                    "state": {"cookies": await self.context.cookies(), "origins": origins}})
            except PlaywrightError:
                self.debug("浏览器已关闭，保留上一次保存的登录状态")

    async def open(self, task):
        self.pacer.failure = None
        self.pacer.interval = task.get("publish_interval_seconds", 10)
        if self.forum is None:
            self.forum = Forum(self.page, self.config.account, task["forum_url"], self.control, self.log,
                               self.guard, debug=self.debug, publish_interval=self.pacer.interval,
                               send_failure=lambda: self.pacer.failure)
        self.forum.home_url = task["forum_url"]
        self.forum.publish_interval = self.pacer.interval
        await self.forum.login(reuse=bool(read_json(self.config.data_directory / "login.json")))
        await self.save_login()
        return self.forum

    async def failure_snapshot(self, error):
        if not self.output_directory or self.page.is_closed() or self.control.stop.is_set():
            return
        if self.forum and urlsplit(self.page.url).hostname == urlsplit(self.forum.home_url).hostname:
            details = {
                "url": canonical_url(self.page.url), "error": self.config.redact(str(error)),
                "publish": self.forum.last_publish, "captcha": {"paused": self.guard.paused, "epoch": self.guard.epoch}}
            private_json(self.output_directory / "failure.json",
                         json.loads(self.config.redact(json.dumps(details, ensure_ascii=False))))
            try:
                await self.page.screenshot(path=str(self.output_directory / "failure.png"), timeout=5000)
            except PlaywrightError:
                self.debug("未能保存失败截图")

    async def __aexit__(self, *_):
        self.closing = True
        await self.save_login()
        await asyncio.gather(self.stop_task, return_exceptions=True)
        await self.guard.close()
        if self.forum:
            await self.forum.close_diagnostics()
        await self.browser.close()
        await self.playwright.stop()


class Forum:
    def __init__(self, page, account, home_url, control, log, guard, *, debug=None, publish_interval=0, send_failure=None):
        self.page, self.account, self.home_url = page, account, home_url
        self.control, self.log, self.guard = control, log, guard
        self.publish_interval = publish_interval
        self.send_failure = send_failure
        self._counts_request = None
        self._counts_response_ok = False
        self._counts_ready = False
        self._detail_request = None
        self._detail_response = None
        self._detail_complete = False
        self._publish_request = None
        self._publish_response = None
        self._publish_complete = False
        self._publish_failure = None
        self.last_publish = None
        self.debug = lambda message: debug(f"[{account.username}] {message}") if debug else None
        self._api_started = {}
        self._diagnostic_tasks = set()

        def requested(request):
            endpoint = urlsplit(request.url).path
            if debug and endpoint.endswith(("/qa/myParticipateQaNum", "/qa/getQuestionInfo",
                                            "/qa/saveQuestion", "/qa/saveAnswer")):
                self._api_started[request] = time.monotonic()
                self.debug(f"API 请求 {request.method} {endpoint}")
            if urlsplit(request.url).path.endswith("/qa/myParticipateQaNum"):
                self._counts_request = request
                self._counts_response_ok = False
                self._counts_ready = False
            elif urlsplit(request.url).path.endswith("/qa/getQuestionInfo"):
                self._detail_request = request
                self._detail_response = None
                self._detail_complete = False
            elif urlsplit(request.url).path.endswith(("/qa/saveQuestion", "/qa/saveAnswer")):
                self._publish_request = request
                self._publish_response = None
                self._publish_complete = False
                self._publish_failure = None

        def responded(response):
            if response.request is self._counts_request:
                self._counts_response_ok = response.ok
            elif response.request is self._detail_request:
                self._detail_response = response
            elif response.request is self._publish_request:
                self._publish_response = response

        def finished(request):
            if request is self._counts_request:
                self._counts_ready = self._counts_response_ok
            elif request is self._detail_request:
                self._detail_complete = self._detail_response is not None
            elif request is self._publish_request:
                self._publish_complete = True
            start = self._api_started.pop(request, None)
            if start is not None:
                task = asyncio.create_task(self._record_response(request, start))
                self._diagnostic_tasks.add(task)
                task.add_done_callback(self._diagnostic_tasks.discard)

        def failed(request):
            if self._api_started.pop(request, None) is not None:
                self.debug(f"API 网络失败 {urlsplit(request.url).path} {request.failure}")
            if request is self._publish_request:
                self._publish_failure = request.failure

        page.on("request", requested)
        page.on("response", responded)
        page.on("requestfinished", finished)
        page.on("requestfailed", failed)
        page.on("pageerror", lambda error: self.debug(f"页面脚本异常：{error.stack or error}"))

    async def _record_response(self, request, start):
        info = {"endpoint": urlsplit(request.url).path,
                "elapsed_ms": round((time.monotonic() - start) * 1000)}
        try:
            response = await request.response()
            info["http_status"] = response.status
            if "json" in response.headers.get("content-type", ""):
                payload = await response.json()
                # 只保留诊断字段，不记录请求正文、Cookie、登录票据或其他学生资料。
                if isinstance(payload, dict):
                    info.update({k: payload[k] for k in ("status", "code", "msg") if k in payload})
                    result = payload.get("rt")
                    if isinstance(result, dict):
                        info.update({k: result[k] for k in ("answerId", "questionId", "questionNum", "answerNum")
                                     if k in result})
                        question = result.get("questionInfo")
                        if isinstance(question, dict):
                            info["question"] = {k: question[k] for k in ("courseId", "questionId", "isAnswer", "answerNum")
                                                if k in question}
        except (PlaywrightError, ValueError) as error:
            info["read_error"] = str(error)
        self.debug("API 完成 " + json.dumps(info, ensure_ascii=False))

    async def close_diagnostics(self):
        if self._diagnostic_tasks:
            await asyncio.gather(*self._diagnostic_tasks)

    async def wait(self, predicate, description, *, timeout=30):
        deadline = time.monotonic() + timeout
        while True:
            start = time.monotonic()
            offline = await self.control.wait_online()
            paused = await self.guard.checkpoint()
            if paused or offline:
                deadline += time.monotonic() - start
            self.control.check()
            if self.send_failure and self.send_failure():
                raise self.send_failure()
            value = await predicate()
            if value:
                # 条件检查期间监视器也可能发现验证码，完成等待后重新读取条件。
                if self.guard.paused:
                    continue
                return value
            if time.monotonic() > deadline:
                raise TimeoutError(f"等待{description}超时")
            await self.control.sleep()

    async def goto(self, url):
        await self.guard.checkpoint()
        parts = urlsplit(url)
        destination = canonical_url(url) if parts.hostname == urlsplit(self.home_url).hostname else urlunsplit(
            parts._replace(query="", fragment=""))
        self.debug(f"导航 {destination}")
        # hash变化仍是同一文档，Vue可能直接复用已有计数而不重新请求。
        # 只有真正换文档或刷新时才清除；新计数请求开始时也会自动清除。
        same_document = urlsplit(self.page.url)._replace(fragment="") == urlsplit(url)._replace(fragment="")
        # 实站复用详情组件时不会因questionId变化重取数据，需完整重载详情。
        reload_detail = same_document and all(
            "/web/questionDetail/" in urlsplit(value).fragment for value in (self.page.url, url)
        )
        if self.page.url == url or not same_document or reload_detail:
            self._counts_request = None
            self._counts_response_ok = False
            self._counts_ready = False
            self._detail_request = None
            self._detail_response = None
            self._detail_complete = False
        if self.page.url == url:
            # 相同的带hash地址可能只是SPA的同文档导航，必须刷新才能重新对账。
            await self.page.reload(wait_until="domcontentloaded", timeout=30_000)
        else:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            if reload_detail:
                await self.guard.checkpoint()
                await self.page.reload(wait_until="domcontentloaded", timeout=30_000)
        await self.guard.checkpoint()

    async def click(self, locator):
        # 这里只重做导航/打开编辑框等可逆操作，发布操作由 submit 单独处理。
        while True:
            await self.guard.checkpoint()
            try:
                await locator.click(timeout=1000)
                return
            except PlaywrightTimeout:
                if not await self.guard.checkpoint():
                    raise

    async def fill(self, locator, text):
        while True:
            await self.guard.checkpoint()
            try:
                await locator.fill(text, timeout=1000)
                return
            except PlaywrightTimeout:
                if not await self.guard.checkpoint():
                    raise

    @staticmethod
    def is_login(url):
        parsed = urlsplit(url)
        return parsed.hostname in {"cas.upc.edu.cn", "login.zhihuishu.com", "passport.zhihuishu.com"}

    async def login(self, *, reuse=False):
        if reuse:
            await self.goto(self.home_url)
            if await self._ready_or_login():
                return
        await self.goto(institution_login_url())
        submitted = False

        async def authenticated():
            nonlocal submitted
            parsed = urlsplit(self.page.url)
            if parsed.hostname in {"hub.polymas.com", "onlineweb.zhihuishu.com", "onlineh5.zhihuishu.com",
                                   "studyvideoh5.zhihuishu.com"} and "/login" not in parsed.path:
                return True
            if parsed.hostname == "cas.upc.edu.cn" and parsed.path == "/cas/login":
                form = self.page.locator("form#loginForm")
                if not submitted and await form.count():
                    await self.guard.checkpoint()
                    await form.evaluate("""(form, credentials) => {
                      const user = form.elements.namedItem('username');
                      const pass = form.elements.namedItem('password');
                      if (!user || !pass) throw new Error('CAS标准表单字段缺失');
                      user.value = credentials[0]; pass.value = credentials[1];
                      form.requestSubmit(form.querySelector('input[type="submit"]') || undefined);
                    }""", [self.account.username, self.account.password])
                    submitted = True
            return False

        await self.wait(authenticated, "机构SSO授权", timeout=45)
        await self.goto(self.home_url)
        if not await self._ready_or_login():
            raise RuntimeError("机构授权后仍被跳转到登录页")
        self.log(f"[{self.account.username}] 已登录")

    async def _ready_or_login(self):
        async def ready():
            if self.is_login(self.page.url):
                return "login"
            if await self.page.locator(".qa_jion_name").count():
                return "forum"
            return False
        return await self.wait(ready, "论坛登录状态") == "forum"

    async def _home(self):
        await self.goto(self.home_url)
        if not await self._ready_or_login():
            await self.login()

        async def stats():
            value = await self.page.evaluate("""() => {
              const heading = document.querySelector('h1');
              const counts = label => [...document.querySelectorAll('.qa_jion_name')]
                .find(el => el.textContent.trim() === label)?.parentElement
                .querySelector('.qa_jion_num')?.textContent.trim();
              return {course: heading?.textContent.trim(), count: counts('我的提问'), answers: counts('我的回答')};
            }""")
            # 消息/风控/题目列表等请求与提问计数无关，不能等待全站网络空闲。
            if value["course"] and value["count"] and value["answers"] and self._counts_ready:
                return value
            if self.is_login(self.page.url):
                raise RuntimeError("登录会话已失效")
            return False

        # 页面统计由异步请求填充，等待两次连续读取一致，避免把初始0当成实际值。
        previous = await self.wait(stats, "提问及回答统计")
        while True:
            await self.control.sleep(0.4)
            current = await self.wait(stats, "提问及回答统计")
            if current == previous:
                self.debug(f"个人统计：course={current['course']} questions={current['count']} answers={current['answers']}")
                return (current["course"].replace("共享课", "").strip(), int(current["count"].replace(",", "")),
                        int(current["answers"].replace(",", "")))
            previous = current

    async def snapshot(self, limit, *, urls=True):
        course, count, answers = await self._home()
        needed = min(count, limit)
        if not needed:
            return Snapshot(course, count, [], answers)
        await self.click(self.page.locator("li").filter(
            has=self.page.locator(".qa_jion_name").filter(has_text=re.compile("^我的提问$"))))

        async def list_ready():
            return "/web/myJoin/" in self.page.url and await self.page.locator("li.question-item").count()
        await self.wait(list_ready, "个人提问列表")
        items = self.page.locator("li.question-item")
        while await items.count() < needed:
            before = await items.count()
            await self.guard.checkpoint()
            await items.last.scroll_into_view_if_needed(timeout=1000)
            await self.page.mouse.wheel(0, 900)

            async def grew():
                if await items.count() > before:
                    return True
                if await self.page.get_by_text("没有更多了", exact=True).is_visible():
                    raise ValueError("个人提问列表不足统计数量")
                return False
            await self.wait(grew, "更多个人提问")
        questions = []
        for index in range(needed):
            await self.guard.checkpoint()
            title = items.nth(index).locator(".question-content")
            text = await title.get_attribute("title") or await title.inner_text()
            if not urls:
                questions.append(Question(self.account.username, normalize(text)))
                continue
            before = set(self.page.context.pages)
            await self.click(title)

            async def opened():
                return next((p for p in self.page.context.pages if p not in before), None)
            detail = await self.wait(opened, "问题详情标签页", timeout=10)
            try:
                async def detail_ready():
                    return "/web/questionDetail/" in detail.url
                await self.wait(detail_ready, "问题详情链接")
                questions.append(Question(self.account.username, normalize(text), canonical_url(detail.url)))
            finally:
                await detail.close()
        return Snapshot(course, count, questions, answers)

    async def submit(self, dialog):
        """发布动作只尝试一次。验证码或不确定结果交给调用者从网页重新对账。"""
        epoch = self.guard.epoch
        if await self.guard.checkpoint():
            return True
        self._publish_request = self._publish_response = None
        self._publish_complete = False
        self._publish_failure = None
        self.last_publish = None
        self.debug("点击立即发布")
        try:
            await dialog.locator(".up-btn").click(timeout=1000)
        except PlaywrightTimeout:
            if self.guard.epoch == epoch and not await self.guard.checkpoint():
                raise

        async def settled():
            if self._publish_failure:
                raise RuntimeError(f"发布请求失败：{self._publish_failure}")
            if self._publish_request:
                # 验证码回调可能自动再次提交；请求仍在途中时不能刷新页面取消它。
                if not self._publish_complete:
                    return False
                response = self._publish_response
                payload = (await response.json()
                           if response.ok or "json" in response.headers.get("content-type", "") else {})
                self.last_publish = {
                    "endpoint": urlsplit(response.url).path, "http_status": response.status,
                    "status": payload.get("status"), "code": payload.get("code"),
                    "message": payload.get("msg", ""),
                }
                verification = str(payload.get("code")) == "1010" or response.status == 422
                if not verification:
                    if response.ok and str(payload.get("status")) == "200" and payload.get("rt"):
                        return True  # 调用方仍须重新读取网页，核对是否实际保存。
                    reason = payload.get("msg") or "服务端未接受发布"
                    raise RuntimeError(f"发布失败：{reason}（HTTP {response.status}，"
                                       f"status={payload.get('status')}，code={payload.get('code')}）")
                return self.guard.epoch != epoch
            errors = dialog.page.locator(".el-message--error")
            for index in range(await errors.count()):
                notice = errors.nth(index)
                if await notice.is_visible():
                    raise RuntimeError(f"页面未提交发布：{await notice.inner_text()}")
            return self.guard.epoch != epoch or not await dialog.is_visible()

        try:
            await self.wait(settled, "发布结果", timeout=30 + self.publish_interval)
        except TimeoutError as error:
            if self.last_publish:
                reason = (f"接口返回 status={self.last_publish['status']}，code={self.last_publish['code']}，"
                          f"{self.last_publish['message'] or '尚未完成页面验证'}")
            else:
                reason = "发布请求尚未返回" if self._publish_request else "页面未发出发布请求"
            raise TimeoutError(f"{error}（{reason}）") from error
        await self.guard.checkpoint()
        return self.guard.epoch != epoch

    async def find_question(self, content, limit):
        snapshot = await self.snapshot(limit, urls=False)
        index = next((i for i, q in enumerate(snapshot.questions) if normalize(q.text) == normalize(content)), None)
        if index is None:
            return None
        before = set(self.page.context.pages)
        await self.click(self.page.locator("li.question-item").nth(index).locator(".question-content"))

        async def opened():
            return next((page for page in self.page.context.pages if page not in before), None)

        detail = await self.wait(opened, "问题详情标签页", timeout=10)
        try:
            async def ready():
                return "/web/questionDetail/" in detail.url
            await self.wait(ready, "问题详情链接")
            url = canonical_url(detail.url)
        finally:
            await detail.close()
        return await self.inspect_question(url)

    async def post_question(self, content, *, lookback=50):
        while True:
            existing = await self.find_question(content, lookback)
            if existing:
                return BrowserResult("posted", existing)
            await self._home()
            await self.click(self.page.locator(".ask-btn"))
            dialog = self.page.get_by_role("dialog", name="提问", exact=True)
            await self.wait(dialog.is_visible, "提问编辑框")
            await self.fill(dialog.get_by_placeholder("写下你的问题，以问号结尾"), content)
            interrupted = await self.submit(dialog)
            actual = await self.find_question(content, lookback)
            if actual:
                return BrowserResult("posted", actual)
            if not interrupted:
                raise RuntimeError("发布后未找到该提问，停止操作以避免重复发布")

    async def _detail_ready(self):
        route = urlsplit(self.page.url).fragment.split("?")[0].split("/")
        course_id, question_id = route[-2:]

        async def response_ready():
            if self.is_login(self.page.url):
                raise RuntimeError("登录会话已失效")
            return self._detail_complete

        await self.wait(response_ready, "目标题目详情数据")
        response = self._detail_response
        if not response.ok:
            raise RuntimeError(f"读取题目详情失败：HTTP {response.status}")
        payload = await response.json()
        if str(payload.get("status")) != "200":
            raise RuntimeError(f"读取题目详情失败：{payload.get('msg', '接口未成功')}")
        question = payload.get("rt", {}).get("questionInfo", {})
        if str(question.get("questionId")) != question_id or str(question.get("courseId")) != course_id:
            raise RuntimeError("详情数据与目标题目不符，停止账号以免漏答或错答")

        async def ready():
            if self.is_login(self.page.url):
                raise RuntimeError("登录会话已失效")
            content = self.page.locator("div.question-content")
            expected = normalize(question.get("content", ""))
            if expected and await content.count() and normalize(await content.first.inner_text()).startswith(expected):
                loading = self.page.get_by_text("加载中...", exact=True)
                return not await loading.count() or not await loading.first.is_visible()
            return False
        await self.wait(ready, "问题及回答状态")
        await self.guard.checkpoint()
        return question

    async def inspect_question(self, url):
        await self.goto(url)
        if self.is_login(self.page.url):
            await self.login()
            await self.goto(url)
        question = await self._detail_ready()
        return QuestionState(normalize(question["content"]), canonical_url(self.page.url),
                             int(question.get("answerNum", 0)), question.get("isAnswer") is True,
                             await self.page.locator("div.my-answer-btn").is_visible())

    async def answer(self, url, content, *, answer_target=None):
        while True:
            question = await self.inspect_question(url)
            if answer_target is not None and question.answer_count >= answer_target:
                return BrowserResult("threshold_met", question)
            if question.is_answered:
                return BrowserResult("already_answered", question)
            if not question.can_answer:
                return BrowserResult("unavailable", question)
            await self.click(self.page.locator("div.my-answer-btn"))
            dialog = self.page.get_by_role("dialog", name="我来回答", exact=True)
            await self.wait(dialog.is_visible, "回答编辑框")
            await self.fill(dialog.get_by_placeholder("请输入您的回答"), content)
            interrupted = await self.submit(dialog)
            actual = await self.inspect_question(url)
            if actual.is_answered:
                return BrowserResult("posted", actual)
            if not interrupted:
                raise RuntimeError("发布后未确认本人已回答，停止操作以避免重复发布")
