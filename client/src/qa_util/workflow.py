import asyncio
import traceback
from datetime import datetime

from .control import Control
from .models import Stopped
from .rpc import RemoteClient, RemoteError
from .storage import ClientLock, private_json, read_json


class ClientRunner:
    def __init__(self, config, *, control=None, progress=None, browser_factory=None, transport=None):
        self.config = config
        self.control = control or Control()
        self.emit = progress or (lambda _: None)
        self.browser_factory, self.transport = browser_factory, transport
        self.directory = config.data_directory / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self.journal_path = config.data_directory / "operation.json"
        self.operation = None
        self.failure = None
        self.closing = False
        self.view = {"phase": "等待连接", "detail": "", "task_name": "", "question_current": 0,
                     "question_total": 0, "pool_current": 0, "pool_total": 0, "answers_done": 0,
                     "answer_current": 0, "answer_total": 0}
        self.control.on_status = lambda username, detail: self.status(detail)

    def log(self, message):
        self.directory.mkdir(parents=True, exist_ok=True)
        line = datetime.now().isoformat(timespec="milliseconds") + " " + self.config.redact(str(message))
        with (self.directory / "debug.log").open("a", encoding="utf-8") as file:
            file.write(line + "\n")

    def update(self, **values):
        self.view.update(values)
        self.emit(dict(self.view))

    def status(self, detail):
        self.update(detail=detail)

    def task_progress(self, tasks):
        current = self.operation["task_id"] if self.operation else self.view.get("task_id")
        task = next((t for t in tasks if t["id"] == current), None)
        if task is None and tasks:
            task = next((t for t in tasks if t["state"] == "running"), tasks[-1])
        if task:
            self.update(task_id=task["id"], task_name=task["name"],
                        question_current=min(task["question_count"], task["question_target"]),
                        question_total=task["question_target"], pool_current=task["questions_satisfied"],
                        pool_total=task["questions_expected"], answers_done=task["answers_done"],
                        answer_current=task.get("my_answer_count") or 0, answer_total=task.get("account_answer_target", 0))

    async def heartbeat(self):
        try:
            while not self.closing:
                result = await self.remote.call("POST", "heartbeat", {"status": self.view["detail"] or self.view["phase"]})
                self.task_progress(result["tasks"])
                await self.control.sleep(10)
        except Stopped:
            return
        except Exception as error:
            self.failure = error
            self.control.stop.set()

    async def before_send(self):
        if not self.operation or self.operation["kind"] == "inspect":
            raise RuntimeError("没有可发布的服务端操作")
        journal = read_json(self.journal_path, {})
        journal["stage"] = "submitting"
        private_json(self.journal_path, journal)
        await self.remote.call("POST", f"work/{self.operation['id']}/start")
        self.log(f"服务端确认发送：{self.operation['id']}")

    async def sync(self, browser, task):
        self.update(phase="同步旧题", task_name=task["name"], task_id=task["id"], detail="")
        forum = await browser.open(task)
        snapshot = await forum.snapshot(task["question_target"])
        questions = []
        for question in snapshot.questions:
            state = await forum.inspect_question(question.url)
            questions.append({"text": state.text, "url": state.url, "answer_count": state.answer_count})
        result = await self.remote.call("POST", f"tasks/{task['id']}/sync", {
            "course_name": snapshot.course_name, "question_count": snapshot.count,
            "my_answer_count": snapshot.answer_count, "questions": questions})
        self.task_progress(result["tasks"])
        await browser.save_login()
        self.log(f"同步任务 {task['id']}：已有提问 {snapshot.count}、回答 {snapshot.answer_count}，登记 {len(questions)} 道")

    async def sync_counts(self, browser, task):
        self.update(phase="同步个人统计", task_name=task["name"], task_id=task["id"], detail="")
        forum = await browser.open(task)
        snapshot = await forum.snapshot(0)
        result = await self.remote.call("POST", f"tasks/{task['id']}/counts", {
            "question_count": snapshot.count, "my_answer_count": snapshot.answer_count})
        self.task_progress(result["tasks"])

    async def execute(self, browser, operation):
        task, question = operation["task"], operation["question"]
        self.update(phase={"question": "补充提问", "answer": "补答", "inspect": "核对回答数量"}[operation["kind"]],
                    task_name=task["name"], task_id=task["id"], detail="")
        forum = await browser.open(task)
        if operation["kind"] == "inspect":
            actual = await forum.inspect_question(question["url"])
            return {"status": "inspected", "answer_count": actual.answer_count}
        if operation["kind"] == "question":
            if operation.get("recover"):
                actual = await forum.find_question(operation["body"], task["question_target"])
            else:
                actual = None
            snapshot = await forum.snapshot(task["question_target"], urls=False)
            if actual is None and snapshot.count >= task["question_target"]:
                return {"status": "not_needed", "question_count": snapshot.count, "my_answer_count": snapshot.answer_count}
            if actual is None:
                result = await forum.post_question(operation["body"], lookback=task["question_target"])
                actual = result.question
                snapshot = await forum.snapshot(task["question_target"], urls=False)
            return {"status": "posted", "text": actual.text, "url": actual.url,
                    "answer_count": actual.answer_count, "question_count": snapshot.count, "my_answer_count": snapshot.answer_count}
        if operation.get("recover"):
            actual = await forum.inspect_question(question["url"])
            if actual.is_answered:
                snapshot = await forum.snapshot(0)
                return {"status": "already_answered", "answer_count": actual.answer_count, "my_answer_count": snapshot.answer_count}
        snapshot = await forum.snapshot(0)
        threshold = None if snapshot.answer_count < task.get("account_answer_target", 0) else task["answer_target"]
        result = await forum.answer(question["url"], operation["body"], answer_target=threshold)
        snapshot = await forum.snapshot(0)
        return {"status": result.status, "answer_count": result.question.answer_count, "my_answer_count": snapshot.answer_count}

    async def handle(self, browser, operation, journal=None):
        self.operation = operation
        journal = journal or {"operation": operation, "stage": "prepared"}
        private_json(self.journal_path, journal)
        try:
            result = journal.get("result")
            if not result or result["status"] == "uncertain":
                result = await self.execute(browser, operation)
        except Stopped:
            raise
        except RemoteError as error:
            if error.code != "operation_inactive":
                raise
            result = {"status": "cancelled"}
        except Exception as error:
            self.log(traceback.format_exc())
            await browser.failure_snapshot(error)
            stage = read_json(self.journal_path, {}).get("stage")
            result = {"status": "uncertain" if stage == "submitting" else "failed",
                      "error": self.config.redact(str(error))}
        journal = read_json(self.journal_path)
        journal["result"] = result
        private_json(self.journal_path, journal)
        try:
            reply = await self.remote.call("POST", f"work/{operation['id']}/result", result)
        except RemoteError as error:
            if error.code != "operation_inactive":
                raise
            reply = {}
            self.log("关联账号已删除或操作已失效，清理本地操作记录")
        await browser.save_login()
        self.log(f"操作 {operation['id']}：{result['status']}")
        if reply.get("reconcile"):
            raise RuntimeError("提交结果尚未确认；请在本客户端重新开始，先核对网页状态")
        self.journal_path.unlink(missing_ok=True)
        self.operation = None
        if result["status"] == "failed":
            raise RuntimeError(result["error"])
        if operation["kind"] == "question" and result["status"] in {"posted", "not_needed"}:
            await self.sync(browser, operation["task"])

    async def run(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.log("单账号客户端启动")
        try:
            with ClientLock(self.config.data_directory):
                self.remote = RemoteClient(self.config, self.control, self.log, self.status, transport=self.transport)
                try:
                    connected = await self.remote.connect()
                    self.log("服务端已连接，账号绑定已核对")
                    self.task_progress(connected["tasks"])
                    heartbeat = asyncio.create_task(self.heartbeat())
                    try:
                        if self.browser_factory is None:
                            from .browser import BrowserSession
                            self.browser_factory = BrowserSession
                        async with self.browser_factory(self.config, self.control, self.log, debug=self.log,
                                                        output_directory=self.directory, before_send=self.before_send) as browser:
                            journal = read_json(self.journal_path)
                            if journal:
                                try:
                                    operation = await self.remote.call("GET", f"work/{journal['operation']['id']}")
                                except RemoteError as error:
                                    if error.code != "operation_inactive":
                                        raise
                                    operation = None
                                if operation and operation["state"] in {"reserved", "submitting"}:
                                    await self.handle(browser, operation, journal)
                                else:
                                    self.journal_path.unlink(missing_ok=True)
                            while True:
                                self.control.check()
                                response = await self.remote.call("POST", "work/claim")
                                if response.get("sync_task"):
                                    await self.sync(browser, response["sync_task"])
                                elif response.get("sync_counts_task"):
                                    await self.sync_counts(browser, response["sync_counts_task"])
                                elif response.get("operation"):
                                    await self.handle(browser, response["operation"])
                                else:
                                    self.task_progress(response.get("tasks", []))
                                    self.update(phase="等待任务", detail=response.get("wait_reason", ""))
                                    await self.control.sleep(3)
                    finally:
                        self.closing = True
                        heartbeat.cancel()
                        await asyncio.gather(heartbeat, return_exceptions=True)
                finally:
                    await self.remote.close()
        except Stopped:
            if self.failure:
                self.log("".join(traceback.format_exception(self.failure)))
                self.update(phase="失败", detail=str(self.failure))
                return "failed"
            self.log("客户端已停止，未核对的操作留待下次恢复")
            self.update(phase="已停止", detail="")
            return "stopped"
        except Exception as error:
            self.log(traceback.format_exc())
            self.update(phase="失败", detail=self.config.redact(str(error)))
            return "failed"
