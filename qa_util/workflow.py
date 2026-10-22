import asyncio
import json
import traceback
from dataclasses import asdict, replace
from datetime import datetime
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import PROJECT_ROOT
from .control import Control
from .generator import Generator
from .models import AccountProgress, AccountResult, Config, Question, RunResult, Stopped, normalize


def preview_text(questions: list[Question]) -> str:
    blocks = []
    for q in questions:
        blocks.append(f"[{q.owner}] {'已有提问' if q.url else '待发布提问'}\n{q.text}")
        if q.url:
            blocks.append(q.url)
        blocks.extend(f"  [{user}] {answer}" for user, answer in q.answers.items())
        blocks.append("")
    return "\n".join(blocks)


class Workflow:
    def __init__(self, config: Config, confirm, log, *, control=None, output_root=None,
                 browser_factory=None, generator=None, progress=None):
        self.config, self.confirm, self.emit = config, confirm, log
        self.control = control or Control()
        stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S-%f")
        self.directory = Path(output_root or PROJECT_ROOT / "runs") / stamp
        self.browser_factory = browser_factory
        self.generator = generator or Generator(config, self.control, self.log)
        self.results = {a.username: AccountResult(a.username) for a in config.accounts}
        self.phase = "初始化"
        self.emit_progress = progress or (lambda _: None)
        self.progress = {a.username: AccountProgress(a.username) for a in config.accounts}
        self.question_counts = {}
        self.control.on_status = lambda username, detail: self.update_progress(username, detail=detail)

    def update_progress(self, username, **changes):
        changes = {k: self.config.redact(v) if isinstance(v, str) else v for k, v in changes.items()}
        updated = replace(self.progress[username], **changes)
        if updated != self.progress[username]:
            self.progress[username] = updated
            self.emit_progress(updated)

    def all_progress(self, **changes):
        for username in self.progress:
            self.update_progress(username, **changes)

    def log(self, message: str):
        message = self.config.redact(message)
        line = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%H:%M:%S ") + message
        self.emit(line)
        if self.directory.exists():
            with (self.directory / "run.log").open("a", encoding="utf-8") as file:
                file.write(line + "\n")
            self.debug(message)

    def save(self, filename: str, data):
        (self.directory / filename).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def debug(self, message: str):
        line = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="milliseconds")
        with (self.directory / "debug.log").open("a", encoding="utf-8") as file:
            file.write(line + " " + self.config.redact(message) + "\n")

    def record_error(self, scope: str, error: Exception):
        self.debug(f"{scope}，阶段={self.phase}\n" + "".join(traceback.format_exception(error)))

    async def run(self) -> RunResult:
        self.directory.mkdir(parents=True)
        self.debug(f"任务开始：账号数={len(self.config.accounts)}，并发={self.config.max_concurrency}，"
                   f"每号提问目标={self.config.questions_per_account}，"
                   f"发布间隔={self.config.publish_interval_seconds}秒")
        try:
            result = await self._run()
        except Stopped:
            self.log("任务已停止")
            result = RunResult("stopped", list(self.results.values()))
        except Exception as error:
            self.record_error("任务失败", error)
            self.log(f"任务失败：{error}")
            result = RunResult("failed", list(self.results.values()))
        self.save("result.json", asdict(result))
        for row in self.progress.values():
            if row.state in {"complete", "failed"}:
                continue
            state = "cancelled" if result.status == "cancelled" else "stopped" if result.status == "stopped" else "failed"
            self.update_progress(row.username, state=state, detail={
                "cancelled": "已放弃", "stopped": "已停止", "failed": "任务未完成，请查看日志",
            }[state])
        return result

    async def _run(self) -> RunResult:
        if self.browser_factory is None:
            from .browser import BrowserPool
            self.browser_factory = partial(BrowserPool, output_directory=self.directory, debug=self.debug)
        accounts = self.config.accounts
        target = self.config.questions_per_account
        semaphore = asyncio.Semaphore(min(self.config.max_concurrency, len(accounts)))
        async with self.browser_factory(self.config, self.control, self.log) as pool:
            async def execute(account, action):
                async with semaphore:
                    self.control.check()
                    self.update_progress(account.username, state="running", detail="登录中")
                    try:
                        async with pool.session(account) as forum:
                            self.update_progress(account.username, detail="")
                            return await action(forum, account)
                    except Stopped:
                        raise
                    except Exception as error:
                        self.record_error(f"[{account.username}] 账号失败", error)
                        message = self.config.redact(str(error))
                        self.results[account.username].error = message
                        self.log(f"[{account.username}] 失败：{message}")
                        self.update_progress(account.username, state="failed", detail="失败，请查看日志")
                        return None

            async def gather(action, phase):
                # 屏障放在协调器，所有账号都释放并发名额后才进入下一阶段。
                self.phase = phase
                self.debug(f"阶段开始：{phase}")
                for account in accounts:
                    is_question = phase == "补齐提问并冻结题池"
                    total = target if is_question else (len(accounts) - 1) * target if phase == "互答" else 0
                    current = min(target, self.question_counts.get(account.username, 0)) if is_question else 0
                    self.update_progress(account.username, phase=phase, current=current, total=total,
                                         state="waiting", detail="等待并发名额")
                rows = await asyncio.gather(*(execute(a, action) for a in accounts), return_exceptions=True)
                self.control.check()
                for row in rows:
                    if isinstance(row, BaseException):
                        raise row
                return rows

            async def inspect(forum, account):
                self.log(f"[{account.username}] 预检")
                snapshot = await forum.snapshot(target)
                self.question_counts[account.username] = snapshot.count
                self.update_progress(account.username, current=1, total=1, state="waiting", detail="预检完成")
                return snapshot

            snapshots = await gather(inspect, "预检")
            if any(s is None for s in snapshots):
                self.log("预检失败，停止生成和发布")
                return RunResult("failed", list(self.results.values()))
            course = snapshots[0].course_name
            if any(s.course_name != course for s in snapshots):
                raise ValueError("账号打开的课程不同，请检查 forum_url 和课程权限")
            questions = []
            for index, (account, snapshot) in enumerate(zip(accounts, snapshots)):
                self.log(f"[{account.username}] 已有 {snapshot.count} 道提问，补充 {max(0, target - snapshot.count)} 道")
                if len(snapshot.questions) != min(target, snapshot.count):
                    raise ValueError("个人题目列表与提问数量不符")
                selected = [replace(q, owner=account.username) for q in snapshot.questions]
                selected.extend(Question(account.username, "") for _ in range(target - len(selected)))
                for slot, question in enumerate(selected):
                    question.key = f"a{index}q{slot}"
                questions.extend(selected)

            self.phase = "生成内容"
            self.debug("阶段开始：生成内容")
            self.all_progress(phase="生成内容", current=0, total=0, state="running", detail="")
            questions = await self.generator.generate(course, questions)
            self._validate_generated(questions, accounts, target)
            self.save("generated.json", [asdict(q) for q in questions])
            preview = preview_text(questions)
            (self.directory / "preview.txt").write_text(preview, encoding="utf-8")
            self.log("内容已生成，等待预览确认")
            self.phase = "等待预览确认"
            self.all_progress(phase="等待预览确认", state="waiting")
            accepted = await self.control.interruptible(self.confirm(preview))
            if not accepted:
                self.log("已取消，未发布提问或回答")
                return RunResult("cancelled", list(self.results.values()))
            self.control.check()

            async def post_questions(forum, account):
                planned = [q for q in questions if q.owner == account.username]
                for question in planned:
                    if not question.url:
                        self.debug(f"[{account.username}] 补充提问：{question.key}")
                        if await forum.ensure_question(question.text, target):
                            self.results[account.username].published += 1
                            current = min(target, self.question_counts[account.username]
                                          + self.results[account.username].published)
                            self.update_progress(account.username, current=current)
                snapshot = await forum.snapshot(target)
                if snapshot.count < target or len(snapshot.questions) != target:
                    raise ValueError("提问尚未达到目标数量")
                by_url = {q.url: q for q in planned if q.url}
                by_text = {normalize(q.text): q for q in planned if not q.url}
                frozen = []
                for actual in snapshot.questions:
                    original = by_url.get(actual.url) or by_text.get(normalize(actual.text))
                    if original is None or normalize(original.text) != normalize(actual.text):
                        raise ValueError("当前最新题目与已确认内容不符，请重新运行预检")
                    frozen.append(replace(original, url=actual.url))
                if len({q.url for q in frozen}) != target:
                    raise ValueError("公共题池中出现重复的问题 URL")
                self.log(f"[{account.username}] 提问达标，贡献 {target} 道题")
                self.update_progress(account.username, current=target, state="waiting", detail="等待其他账号提问达标")
                return frozen

            groups = await gather(post_questions, "补齐提问并冻结题池")
            if any(group is None for group in groups):
                self.log("第一轮未全部成功，第二轮不启动")
                return RunResult("failed", list(self.results.values()))
            questions = [q for group in groups for q in group]
            self.save("questions.json", [asdict(q) for q in questions])
            self.log("公共题池已冻结，开始第二轮互答")

            async def answer_questions(forum, account):
                result = self.results[account.username]
                for question in questions:
                    if question.owner == account.username:
                        continue
                    self.control.check()
                    self.debug(f"[{account.username}] 回答目标：{question.key} {question.url}")
                    posted = await forum.answer(question.url, question.answers[account.username])
                    result.covered += 1
                    result.skipped += not posted
                    self.update_progress(account.username, current=result.covered)
                    self.log(f"[{account.username}] 已覆盖 {result.covered}/{(len(accounts) - 1) * target}"
                             + ("（目标题已加载，无回答入口，跳过）" if not posted else "（已确认回答成功）")
                             + f" {question.url}")
                self.update_progress(account.username, state="complete", detail="已完成")
                return result

            await gather(answer_questions, "互答")
        failed = any(r.error for r in self.results.values())
        self.log("任务结束，部分账号失败" if failed else "全部账号已完成")
        return RunResult("partial" if failed else "complete", list(self.results.values()))

    @staticmethod
    def _validate_generated(questions, accounts, target):
        if len(questions) != len(accounts) * target:
            raise ValueError("生成的公共题池数量不符")
        if len({q.key for q in questions}) != len(questions):
            raise ValueError("问题编号重复")
        users = {a.username for a in accounts}
        for account in accounts:
            if sum(q.owner == account.username for q in questions) != target:
                raise ValueError("账号的问题数量不符")
        for q in questions:
            if not q.text or len(q.text) > 1000 or set(q.answers) != users - {q.owner}:
                raise ValueError("生成内容或回答映射不完整")
            if any(not isinstance(a, str) or not a.strip() or len(a) > 1000 for a in q.answers.values()):
                raise ValueError("生成回答为空或超过1000字")
