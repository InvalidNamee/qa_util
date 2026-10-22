import asyncio
import json
import re
import time
import traceback
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

from .models import Question, Snapshot, Stopped, normalize


def institution_login_url() -> str:
    service = "http://upcsso.zhihuishu.com/authorize?" + urlencode({
        "portal": "14", "response_type": "code", "scope": "read",
        "client_id": "zhihuishuupc", "redirect_uri": "http://oc.zhihuishu.com/oauth/callback/upc",
        "state": "09876999",
    })
    return "https://cas.upc.edu.cn/cas/login?" + urlencode({"service": service})


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    route, _, query = parts.fragment.partition("?")
    params = sorted((k, v) for k, v in parse_qsl(query) if k not in {"VNK", "aid"})
    return urlunsplit(parts._replace(fragment=route + ("?" + urlencode(params) if params else "")))


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

    def __init__(self, interval, control, log, username):
        self.interval, self.control, self.log, self.username = interval, control, log, username
        self.last_sent = None
        self.lock = asyncio.Lock()

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
                self.control.status(self.username, "")
                self.last_sent = time.monotonic()
                await route.continue_()
        except (Stopped, PlaywrightError):
            # 停止按钮会关闭浏览器，正在限速等待的路由随之释放。
            if not self.control.stop.is_set():
                raise


class BrowserPool:
    def __init__(self, config, control, log, *, headless=False, output_directory=None, debug=None):
        self.config, self.control, self.log = config, control, log
        self.headless = headless
        self.output_directory = output_directory
        self.debug = (lambda message: debug(config.redact(message))) if debug else None
        self.states = {}
        self.publishers = {}

    async def __aenter__(self):
        self.playwright = await async_playwright().start()
        try:
            self.browser = await self.playwright.chromium.launch(headless=self.headless)
        except Exception:
            await self.playwright.stop()
            raise
        self.stop_task = asyncio.create_task(self._watch_stop())
        self.closing = False
        return self

    async def _watch_stop(self):
        while not self.control.stop.is_set() and not self.closing:
            await asyncio.sleep(0.2)
        if self.control.stop.is_set():
            await self.browser.close()

    async def __aexit__(self, *_):
        self.closing = True
        await asyncio.gather(self.stop_task, return_exceptions=True)
        try:
            await self.browser.close()
        finally:
            await self.playwright.stop()

    @asynccontextmanager
    async def session(self, account):
        self.control.check()
        state = self.states.get(account.username)
        context = await self.browser.new_context(storage_state=state, viewport={"width": 1440, "height": 1000})
        guard = CaptchaGuard(context, self.control, self.log, account.username)
        page = forum = None
        try:
            pacer = self.publishers.setdefault(account.username, PublishPacer(
                self.config.publish_interval_seconds, self.control, self.log, account.username))
            await context.route(PUBLISH_URL, lambda route: pacer.send(route, guard))
            page = await context.new_page()
            page.set_default_timeout(1000)
            await guard.start()
            forum = Forum(page, account, self.config.forum_url, self.control, self.log, guard,
                          debug=self.debug, publish_interval=self.config.publish_interval_seconds)
            await forum.login(reuse=bool(state))
            yield forum
            if self.browser.is_connected():
                # storage_state会另开页面访问所有SSO来源，CAS跳转可能打断保存。
                # 登录凭据在Cookie中；仅保存当前论坛的localStorage，无需重访认证站点。
                origins = await page.evaluate("""() => ['http:', 'https:'].includes(location.protocol)
                  ? [{origin: location.origin, localStorage: Object.entries(localStorage)
                      .map(([name, value]) => ({name, value}))}]
                  : []""")
                self.states[account.username] = {"cookies": await context.cookies(), "origins": origins}
        except Exception as error:
            if (self.output_directory and page and not page.is_closed() and not self.control.stop.is_set()
                    and urlsplit(page.url).hostname == urlsplit(self.config.forum_url).hostname):
                name = f"failure-a{self.config.accounts.index(account) + 1}"
                details = {"url": canonical_url(page.url), "error": self.config.redact(str(error)),
                           "traceback": "".join(traceback.format_exception(error)),
                           "captcha": {"paused": guard.paused, "epoch": guard.epoch,
                                       "monitor_error": str(guard.failure) if guard.failure else None},
                           "publish": forum.last_publish if forum else None,
                           "publish_request": {
                               "endpoint": urlsplit(forum._publish_request.url).path if forum._publish_request else None,
                               "complete": forum._publish_complete, "failure": forum._publish_failure,
                           } if forum else None}
                (self.output_directory / f"{name}.json").write_text(
                    self.config.redact(json.dumps(details, ensure_ascii=False, indent=2)), encoding="utf-8")
                try:
                    await page.screenshot(path=str(self.output_directory / f"{name}.png"), timeout=5000)
                except PlaywrightError:
                    self.log(f"[{account.username}] 未能保存失败截图")
                self.log(f"[{account.username}] 已保存失败现场：{name}.json")
            raise
        finally:
            await guard.close()
            if forum:
                await forum.close_diagnostics()
            await context.close()


class Forum:
    def __init__(self, page, account, home_url, control, log, guard, *, debug=None, publish_interval=0):
        self.page, self.account, self.home_url = page, account, home_url
        self.control, self.log, self.guard = control, log, guard
        self.publish_interval = publish_interval
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
            paused = await self.guard.checkpoint()
            if paused:
                deadline += time.monotonic() - start
            self.control.check()
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
              const name = [...document.querySelectorAll('.qa_jion_name')]
                .find(el => el.textContent.trim() === '我的提问');
              const count = name?.parentElement.querySelector('.qa_jion_num')?.textContent.trim();
              return {course: heading?.textContent.trim(), count};
            }""")
            # 消息/风控/题目列表等请求与提问计数无关，不能等待全站网络空闲。
            if value["course"] and value["count"] and self._counts_ready:
                return value
            if self.is_login(self.page.url):
                raise RuntimeError("登录会话已失效")
            return False

        # 页面统计由异步请求填充，等待两次连续读取一致，避免把初始0当成实际值。
        previous = await self.wait(stats, "提问统计")
        while True:
            await self.control.sleep(0.4)
            current = await self.wait(stats, "提问统计")
            if current == previous:
                self.debug(f"提问统计：course={current['course']} count={current['count']}")
                return current["course"].replace("共享课", "").strip(), int(current["count"].replace(",", ""))
            previous = current

    async def snapshot(self, limit, *, urls=True):
        course, count = await self._home()
        needed = min(count, limit)
        if not needed:
            return Snapshot(course, count, [])
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
        return Snapshot(course, count, questions)

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

    async def ensure_question(self, text, target):
        while True:
            snapshot = await self.snapshot(target, urls=False)
            if any(normalize(q.text) == normalize(text) for q in snapshot.questions):
                return False
            if snapshot.count >= target:
                return False
            await self._home()
            await self.click(self.page.locator(".ask-btn"))
            dialog = self.page.get_by_role("dialog", name="提问", exact=True)
            await self.wait(dialog.is_visible, "提问编辑框")
            await self.fill(dialog.get_by_placeholder("写下你的问题，以问号结尾"), text)
            interrupted = await self.submit(dialog)
            actual = await self.snapshot(target, urls=False)
            if any(normalize(q.text) == normalize(text) for q in actual.questions):
                self.log(f"[{self.account.username}] 已发布提问：{text}")
                return True
            if actual.count >= target:
                return False
            if not interrupted:
                raise RuntimeError("发布后未找到该提问，停止账号以避免重复发布")
            # 人工验证后确认没有提交，才允许重新打开编辑器提交同一题。

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

    async def answer(self, url, text):
        attempted = False
        while True:
            await self.goto(url)
            question = await self._detail_ready()
            button = self.page.locator("div.my-answer-btn")
            if not await button.is_visible():
                self.debug(f"无回答入口：isAnswer={question.get('isAnswer')}，按规则跳过")
                return attempted
            await self.click(button)
            dialog = self.page.get_by_role("dialog", name="我来回答", exact=True)
            await self.wait(dialog.is_visible, "回答编辑框")
            await self.fill(dialog.get_by_placeholder("请输入您的回答"), text)
            interrupted = await self.submit(dialog)
            attempted = True
            # 用新的详情请求核对，不因弹窗关闭就认定发布成功。
            await self.goto(url)
            question = await self._detail_ready()
            if not await self.page.locator("div.my-answer-btn").is_visible():
                if question.get("isAnswer") is not True:
                    raise RuntimeError("提交后未确认本人已回答，停止账号以免误报成功")
                return True
            if not interrupted:
                raise RuntimeError("发布后仍有回答入口，停止账号以避免重复回答")
            attempted = False
