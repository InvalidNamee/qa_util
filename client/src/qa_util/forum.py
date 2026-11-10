import base64
import json
import re
import time
from http.cookiejar import Cookie
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.serialization import load_der_public_key
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

from .models import ForumResult, ManualVerification, Question, QuestionState, Snapshot, normalize
from .storage import private_json, read_json
from .urls import canonical_url

ORIGIN = "https://qah5.zhihuishu.com"
QA_API = "https://creditqa-api.zhihuishu.com/creditqa/gateway/t/v1/web/qa/"
KEY_API = "https://appcomm-user.zhihuishu.com/app-commserv-user/c/hasV2"
# Public protocol constants from the forum's 6.js, shared by all accounts.
PUBLIC_KEY = load_der_public_key(base64.b64decode(
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCgfZmpLpPEpEFRKBe+ZjWJUjPe+"
    "7qg7pGqcfN3j2egJ8H2mrKwaEqZEnPnpi2O3hN8HRyaFozDOp8gwZiYfiIZjWy0Jr/"
    "FNAiiKYh5bq0GsEn+ieMmRyJg/+i1rqizhvCXvFdrdGhFTw5EBwTpsGdwe1utdlrvIJUAFWj9Yh4qbQIDAQAB"
))
IV = bytes.fromhex("31673371716468346a7662736b623978")


def institution_login_url():
    service = "http://upcsso.zhihuishu.com/authorize?" + urlencode({
        "portal": "14", "response_type": "code", "scope": "read",
        "client_id": "zhihuishuupc", "redirect_uri": "http://oc.zhihuishu.com/oauth/callback/upc",
        "state": "09876999",
    })
    return "https://cas.upc.edu.cn/cas/login?" + urlencode({"service": service})


def decode_key(encoded):
    numbers = PUBLIC_KEY.public_numbers()
    size = (numbers.n.bit_length() + 7) // 8
    encrypted = base64.b64decode(encoded, validate=True)
    if len(encrypted) != size or int.from_bytes(encrypted, "big") >= numbers.n:
        raise ValueError("动态参数密文长度或范围无效")
    block = pow(int.from_bytes(encrypted, "big"), numbers.e, numbers.n).to_bytes(size, "big")
    if not re.match(rb"\x00\x01\xff{8,}\x00", block):
        raise ValueError("动态参数签名格式无效")
    payload = json.loads(block.split(b"\x00", 2)[2])
    key = payload["cKey"].encode()
    if payload["module"] != 10 or len(key) not in (16, 24, 32):
        raise ValueError("动态参数模块或密钥长度无效")
    return key


def encode_form(params, key):
    body = json.dumps(params, ensure_ascii=False, separators=(",", ":")).encode()
    padder = padding.PKCS7(128).padder()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(IV)).encryptor()
    encrypted = encryptor.update(padder.update(body) + padder.finalize()) + encryptor.finalize()
    return {"dateFormate": int(time.time()) * 1000, "secretStr": base64.b64encode(encrypted).decode()}


class LoginExpired(RuntimeError):
    pass


class ForumSession:
    def __init__(self, config, control, log, *, before_send=None):
        self.config, self.control, self.log = config, control, log
        self.before_send = before_send
        self.login_path = config.data_directory / "login.json"
        self.cooldown_path = config.data_directory / "cooldown.json"
        previous = read_json(self.cooldown_path, {}).get("last_sent")
        self.last_sent = time.monotonic() - max(0, time.time() - previous) if previous else None
        self.key = None
        self.course_names = {}

    async def __aenter__(self):
        saved = read_json(self.login_path, {})
        if saved and saved.get("username") != self.config.account.username:
            raise ValueError("本地登录状态不属于当前账号，请使用独立数据目录")
        self.http = AsyncSession(impersonate="chrome", max_clients=1, timeout=45, retry=0)
        try:
            # Reuse cookies from the previous client's state.cookies format.
            for row in saved.get("state", {}).get("cookies", []):
                domain = row["domain"]
                if not (domain.lstrip(".") == "zhihuishu.com" or domain.endswith(".zhihuishu.com")):
                    continue
                expires = row.get("expires")
                if expires is not None and expires >= 0 and expires <= time.time():
                    continue
                self.http.cookies.jar.set_cookie(Cookie(
                    0, row["name"], row["value"], None, False, domain, domain.startswith("."),
                    domain.startswith("."), row["path"], True, row.get("secure", False),
                    int(expires) if expires is not None and expires >= 0 else None,
                    expires is None or expires < 0, None, None, {},
                ))
        except BaseException:
            await self.http.close()
            raise
        return self

    async def __aexit__(self, *_):
        try:
            if self.cookie("jt-cas") and self.cookie("CASLOGC"):
                self.save_login()
        finally:
            await self.http.close()

    def cookie(self, name):
        return unquote(next((cookie.value for cookie in self.http.cookies.jar
                            if cookie.name == name and cookie.domain.lstrip(".") == "zhihuishu.com"
                            and not cookie.is_expired()), ""))

    async def request(self, method, url, label, **kwargs):
        try:
            response = await self.control.interruptible(self.http.request(method, url, **kwargs))
        except RequestException as error:
            raise RuntimeError(f"{label}网络请求失败（{type(error).__name__}）") from None
        self.log(f"{label} HTTP {response.status_code}")
        return response

    @staticmethod
    def payload(response, label, *, write=False):
        if response.status_code in (422, 429):
            raise ManualVerification(f"{label}遇到平台风控（HTTP {response.status_code}）", rejected=write)
        if response.status_code in (401, 403) or 300 <= response.status_code < 400:
            raise LoginExpired("论坛会话已失效")
        if response.status_code != 200:
            raise RuntimeError(f"{label}请求失败（HTTP {response.status_code}）")
        try:
            data = response.json()
        except ValueError:
            raise RuntimeError(f"{label}未返回有效 JSON") from None
        if not isinstance(data, dict):
            raise RuntimeError(f"{label}返回的数据格式无效")
        code, status = str(data.get("code")), str(data.get("status"))
        if code == "1010":
            raise ManualVerification(f"{label}需要验证码（1010）", rejected=write)
        if code in {"401", "403"} or status in {"401", "403"}:
            raise LoginExpired("论坛会话已失效")
        return data

    def save_login(self):
        cookies = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
                    "secure": c.secure, "expires": c.expires}
                   for c in self.http.cookies.jar
                   if c.domain.lstrip(".") == "zhihuishu.com" or c.domain.endswith(".zhihuishu.com")]
        private_json(self.login_path, {"username": self.config.account.username, "state": {"cookies": cookies}})

    async def login(self):
        self.http.cookies.clear()
        self.key = None
        self.control.status(self.config.account.username, "登录智慧树")
        try:
            response = await self.request("GET", institution_login_url(), "获取学校登录表单")
            form = BeautifulSoup(response.text, "html.parser").select_one("form#loginForm")
            if not form:
                raise RuntimeError("未找到学校登录表单")
            if form.select_one("input[name*=captcha i]:not([type=hidden]), input[name=verifyCode i]:not([type=hidden])"):
                raise ManualVerification("学校登录需要人工验证")
            action = urljoin(response.url, form.get("action") or response.url)
            destination = urlsplit(action)
            if destination.scheme != "https" or destination.netloc != "cas.upc.edu.cn":
                raise RuntimeError("学校登录表单地址无效")
            fields = {field["name"]: field.get("value", "") for field in form.select("input[name]:not([disabled])")
                      if field.get("type", "").lower() not in ("checkbox", "radio", "button")
                      or field.has_attr("checked")}
            fields.update(username=self.config.account.username, password=self.config.account.password)
            response = await self.request("POST", action, "学校登录", data=fields)
            target = urlsplit(response.url)
            if target.hostname == "login.zhihuishu.com" and target.path == "/wxTransit/3":
                await self.exchange(parse_qs(target.query))
            if not self.cookie("jt-cas") or not self.cookie("CASLOGC"):
                raise RuntimeError("智慧树登录未完成，请核对账号状态")
            self.save_login()
            self.log("智慧树已登录")
        finally:
            self.control.status(self.config.account.username, "")

    async def exchange(self, query):
        cache = query.get("cacheId", [""])[0]
        if query.get("isLock") == ["1"]:
            raise ManualVerification("智慧树账号需要人工处理")
        if not cache:
            raise RuntimeError("智慧树登录缺少中转票据")
        headers = {"Origin": "https://login.zhihuishu.com", "Referer": "https://login.zhihuishu.com/",
                   "Content-Type": "application/json;charset=UTF-8"}
        user_api = "https://user.zhihuishu.com/zhsuser/"
        cache = quote(cache, safe="")
        response = await self.request("GET", user_api + "user/cache/" + cache, "核对登录中转", headers=headers)
        if str(self.payload(response, "核对登录中转").get("status")) != "200":
            raise RuntimeError("无法读取智慧树登录中转状态")
        response = await self.request("POST", user_api + "login/code/skipSet/" + cache,
                                      "交换一次性登录口令", headers=headers, data="null")
        ticket = self.payload(response, "交换一次性登录口令")
        if str(ticket.get("status")) != "1" or not (ticket.get("rt") or {}).get("pwd"):
            raise RuntimeError("未取得智慧树一次性登录口令")
        target = query.get("service", ["https://www.zhihuishu.com"])[0]
        if "oauth/login/ssoLogin" not in target and not target.startswith("https://oauth.zhihuishu.com/authorize"):
            target = "https://cloudapi.polymas.com/oauth/login/ssoLogin?redirect=" + quote(target, safe="")
        await self.request("GET", "https://passport.zhihuishu.com/login", "完成智慧树授权",
                           params={"pwd": ticket["rt"]["pwd"], "service": target})

    async def dynamic_key(self):
        uid = base64.b64encode(PUBLIC_KEY.encrypt(b'{"module":10}', PKCS1v15())).decode()
        response = await self.request("GET", KEY_API, "获取论坛动态参数", params={"uid": uid})
        data = self.payload(response, "获取论坛动态参数")
        try:
            self.key = decode_key(data["rt"]["sl"])
        except (KeyError, TypeError, ValueError):
            raise RuntimeError("智慧树动态参数格式已变更") from None

    async def call(self, endpoint, params, *, interval=None):
        write = interval is not None
        for attempt in range(2):
            if not self.cookie("jt-cas") or not self.cookie("CASLOGC"):
                await self.login()
            if self.key is None:
                await self.dynamic_key()
            if write:
                remaining = 0 if self.last_sent is None else interval - (time.monotonic() - self.last_sent)
                if remaining > 0:
                    self.control.status(self.config.account.username, "等待发送间隔")
                    await self.control.sleep(remaining)
                await self.control.wait_online()
                self.control.status(self.config.account.username, "")
                if self.before_send:
                    await self.before_send()
                self.last_sent = time.monotonic()
                private_json(self.cooldown_path, {"last_sent": time.time()})
            headers = {"Origin": ORIGIN, "Referer": ORIGIN + "/", "Authorization": self.cookie("jt-cas")}
            response = await self.request("POST", QA_API + endpoint, endpoint,
                                          headers=headers, data=encode_form(params, self.key),
                                          allow_redirects=False)
            try:
                data = self.payload(response, endpoint, write=write)
            except LoginExpired:
                if write or attempt:
                    raise
                await self.login()
                continue
            if str(data.get("status")) != "200" or data.get("rt") is None:
                raise RuntimeError(f"{endpoint}未返回成功结果")
            return data["rt"]

    async def open(self, task):
        return Forum(self, task["forum_url"], task.get("publish_interval_seconds", 10))


class Forum:
    def __init__(self, session, url, interval):
        self.session, self.interval = session, interval
        self.parts = urlsplit(canonical_url(url))
        route, _, query = self.parts.fragment.partition("?")
        match = re.fullmatch(r"/web/(?:home|myJoin)/([0-9]+)", route)
        params = parse_qs(query)
        recruit = params.get("recruitId", [""])[0]
        if self.parts.hostname != "qah5.zhihuishu.com" or not match or not recruit.isdecimal():
            raise ValueError("请使用智慧树课程问答主页链接（含 recruitId）")
        self.base = {"courseId": match[1], "recruitId": recruit}
        self.role = params.get("role", ["2"])[0]
        self.course_key = match[1], recruit

    def question_url(self, question_id):
        question_id = str(question_id)
        if not question_id.isdecimal():
            raise ValueError("论坛返回的题目 ID 无效")
        query = urlencode({"recruitId": self.base["recruitId"], "role": self.role})
        route = f"/web/questionDetail/{self.base['courseId']}/{question_id}?{query}"
        return urlunsplit(self.parts._replace(fragment=route))

    def question_id(self, url):
        detail = urlsplit(url)
        route, _, query = detail.fragment.partition("?")
        match = re.fullmatch(r"/web/questionDetail/([0-9]+)/([0-9]+)", route)
        if ((detail.scheme, detail.netloc, detail.path) != (self.parts.scheme, self.parts.netloc, self.parts.path)
                or not match or match[1] != self.base["courseId"]
                or parse_qs(query).get("recruitId") != [self.base["recruitId"]]):
            raise ValueError("题目链接与当前论坛不符")
        return match[2]

    async def own_questions(self, limit=None):
        page, seen = 0, set()
        while limit is None or len(seen) < limit:
            data = await self.session.call("myQuestionList", {**self.base, "pageIndex": page, "pageSize": 50})
            for item in data["questionInfoList"] or []:
                url = self.question_url(item["questionId"])
                if url not in seen:
                    seen.add(url)
                    answer_count = item.get("answerNum")
                    yield Question(self.session.config.account.username, normalize(item["content"]), url,
                                   int(answer_count) if answer_count is not None else None)
                if limit is not None and len(seen) >= limit:
                    return
            next_page = int(data["pageIndex"])
            if next_page < 0:
                return
            if next_page <= page:
                raise RuntimeError("个人题目列表分页未推进")
            page = next_page

    async def snapshot(self, limit):
        counts = await self.session.call("myParticipateQaNum", self.base)
        if self.course_key not in self.session.course_names:
            user = json.loads(self.session.cookie("CASLOGC"))
            info = await self.session.call("qaAnswerIndexPage", {
                **self.base, "sourceType": self.role, "uuid": user["uuid"],
            })
            course = info.get("myCourseDto") or info.get("qaAnswerIndexPage") or {}
            self.session.course_names[self.course_key] = normalize(course["courseName"]).removesuffix("共享课").strip()
        count = int(counts["questionNum"])
        needed = min(count, limit)
        questions = [q async for q in self.own_questions(needed)]
        if len(questions) != needed:
            raise RuntimeError("个人题目列表不足统计数量")
        return Snapshot(self.session.course_names[self.course_key], count, questions, int(counts["answerNum"]))

    async def inspect_question(self, url):
        question_id = self.question_id(url)
        data = await self.session.call("getQuestionInfo", {"questionId": question_id, "sourceType": self.role})
        info = data["questionInfo"]
        if str(info["questionId"]) != question_id or str(info["courseId"]) != self.base["courseId"]:
            raise RuntimeError("详情数据与目标题目不符")
        if type(info["isAnswer"]) is not bool or type(data["isMyQuestion"]) is not bool:
            raise RuntimeError("无法核对本人回答和作者状态")
        answered = info["isAnswer"]
        can_answer = not (answered or data["isMyQuestion"]) and str(info.get("contentShowStatus", 1)) == "1"
        return QuestionState(normalize(info["content"]), self.question_url(question_id),
                             int(info["answerNum"]), answered, can_answer)

    async def find_question(self, content):
        async for question in self.own_questions():
            if question.text == normalize(content):
                return await self.inspect_question(question.url)
        return None

    async def post_question(self, content):
        existing = await self.find_question(content)
        if existing:
            return ForumResult("posted", existing)
        result = await self.session.call("saveQuestion", {
            **self.base, "annexs": "[]", "content": content, "chapterId": "",
            "sourceType": self.role, "saveSource": 1,
        }, interval=self.interval)
        actual = await self.inspect_question(self.question_url(result["questionId"]))
        if actual.text != normalize(content):
            raise RuntimeError("发布后未核对到提问正文")
        return ForumResult("posted", actual)

    async def answer(self, url, content, *, answer_target=None):
        actual = await self.inspect_question(url)
        if actual.is_answered:
            return ForumResult("already_answered", actual)
        if answer_target is not None and actual.answer_count >= answer_target:
            return ForumResult("threshold_met", actual)
        if not actual.can_answer:
            return ForumResult("unavailable", actual)
        question_id = self.question_id(url)
        await self.session.call("saveAnswer", {
            **self.base, "annexs": "[]", "qid": question_id, "source": self.role,
            "aContent": content, "saveSource": 1,
        }, interval=self.interval)
        actual = await self.inspect_question(url)
        if not actual.is_answered:
            raise RuntimeError("发布后未确认本人已回答")
        return ForumResult("posted", actual)
