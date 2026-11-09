import hashlib
import json
import secrets
import time
import uuid

from .config import http_url
from .models import normalize
from .urls import question_url


class ApiError(Exception):
    def __init__(self, message, status=400, code="invalid"):
        super().__init__(message)
        self.status, self.code = status, code


def integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"数量必须是至少 {minimum} 的整数")
    return value


def text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise ValueError("正文不能为空或超过1000字")
    return normalize(value)


class Service:
    def __init__(self, database, *, interval=10, clock=time.time):
        self.database, self.interval, self.clock = database, interval, clock

    @staticmethod
    def _alias(value):
        if not isinstance(value, str) or len(value) > 100:
            raise ValueError("别名不能超过100字")
        return normalize(value)

    def create_account(self, username, alias=""):
        username = text(username)
        alias = self._alias(alias)
        token = secrets.token_urlsafe(32)
        with self.database.transaction() as db:
            account_id = db.execute("INSERT INTO accounts(username,token_hash,alias) VALUES (?,?,?)",
                                    (username, self.token_hash(token), alias)).lastrowid
        return account_id, token

    def set_alias(self, account_id, alias):
        alias = self._alias(alias)
        with self.database.transaction() as db:
            if not db.execute("UPDATE accounts SET alias=? WHERE id=?", (alias, account_id)).rowcount:
                raise ApiError("账号不存在", 404)

    def delete_account(self, account_id):
        with self.database.transaction() as db:
            if not db.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
                raise ApiError("账号不存在", 404)
            submitting = db.execute("""SELECT 1 FROM operations WHERE state='submitting' AND
                                       (account_id=? OR question_id IN (SELECT id FROM questions WHERE owner_id=?))""",
                                    (account_id, account_id)).fetchone()
            if submitting:
                raise ValueError("账号或其题目有正在提交、尚未核对的操作，请完成核对后再删除")
            db.execute("""UPDATE tasks SET state='paused' WHERE state='running' AND id IN
                          (SELECT task_id FROM members WHERE account_id=?)""", (account_id,))
            db.execute("""UPDATE answer_drafts SET state='ready' WHERE id IN
                          (SELECT draft_id FROM operations WHERE account_id=? AND state='reserved')""", (account_id,))
            # 保留取消的生成任务编号，防止正在等待 LLM 的线程写回已删除的题目。
            db.execute("""UPDATE generation_jobs SET state='cancelled',owner_id=NULL,question_id=NULL
                          WHERE owner_id=? OR question_id IN (SELECT id FROM questions WHERE owner_id=?)""",
                       (account_id, account_id))
            db.execute("""DELETE FROM operations WHERE account_id=? OR
                          question_id IN (SELECT id FROM questions WHERE owner_id=?)""", (account_id, account_id))
            db.execute("""DELETE FROM answer_attempts WHERE account_id=? OR
                          question_id IN (SELECT id FROM questions WHERE owner_id=?)""", (account_id, account_id))
            db.execute("DELETE FROM answer_drafts WHERE question_id IN (SELECT id FROM questions WHERE owner_id=?)", (account_id,))
            db.execute("DELETE FROM questions WHERE owner_id=?", (account_id,))
            db.execute("DELETE FROM members WHERE account_id=?", (account_id,))
            db.execute("DELETE FROM accounts WHERE id=?", (account_id,))

    @staticmethod
    def token_hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def authenticate(self, token):
        rows = self.database.rows("SELECT id FROM accounts WHERE token_hash=?", (self.token_hash(token),))
        if not rows:
            raise ApiError("账号接入码无效", 401, "unauthorized")
        return rows[0]["id"]

    def rotate_token(self, account_id):
        token = secrets.token_urlsafe(32)
        with self.database.transaction() as db:
            if not db.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
                raise ApiError("账号不存在", 404)
            db.execute("UPDATE accounts SET token_hash=?,client_id=NULL,session_id=NULL,session_until=0 WHERE id=?",
                       (self.token_hash(token), account_id))
        return token

    def create_task(self, name, forum_url, question_target, answer_target, accounts, *, account_answer_target=None):
        name, forum_url = text(name), http_url(forum_url)
        integer(question_target, 1)
        integer(answer_target, 1)
        if account_answer_target is None:
            account_answer_target = question_target
        integer(account_answer_target, 1)
        if account_answer_target > question_target:
            raise ValueError("每号回答目标不能大于每号提问目标")
        accounts = sorted(set(accounts))
        if not accounts:
            raise ValueError("请选择参与账号")
        with self.database.transaction() as db:
            if any(not db.execute("SELECT 1 FROM accounts WHERE id=?", (aid,)).fetchone() for aid in accounts):
                raise ValueError("参与账号不存在")
            task_id = db.execute("""INSERT INTO tasks(name,forum_url,question_target,answer_target,account_answer_target,created_at)
                                  VALUES (?,?,?,?,?,?)""",
                                 (name, forum_url, question_target, answer_target, account_answer_target, self.clock())).lastrowid
            db.executemany("INSERT INTO members(task_id,account_id) VALUES (?,?)", [(task_id, aid) for aid in accounts])
        return task_id

    def set_task_state(self, task_id, state):
        if state not in {"running", "paused"}:
            raise ValueError("任务状态无效")
        with self.database.transaction() as db:
            task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise ApiError("任务不存在", 404)
            if task["state"] == "complete":
                raise ValueError("任务已经完成；修改目标请新建任务")
            if state == "running" and not db.execute("SELECT 1 FROM members WHERE task_id=?", (task_id,)).fetchone():
                raise ValueError("任务已无参与账号，请新建任务")
            db.execute("UPDATE tasks SET state=? WHERE id=?", (state, task_id))
            if state == "running":
                self.schedule(db, task_id)

    def retry(self, task_id):
        with self.database.transaction() as db:
            db.execute("UPDATE generation_jobs SET state='pending',error='' WHERE task_id=? AND state='failed'", (task_id,))
            db.execute("UPDATE operations SET state='cancelled',error='' WHERE task_id=? AND state='failed'", (task_id,))
            db.execute("""DELETE FROM answer_attempts WHERE status='failed' AND question_id IN
                          (SELECT id FROM questions WHERE task_id=?)""", (task_id,))
            db.execute("UPDATE tasks SET error='' WHERE id=?", (task_id,))
            self.schedule(db, task_id)

    def _session(self, db, account_id, session_id):
        row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row or not session_id or row["session_id"] != session_id or row["session_until"] < self.clock():
            raise ApiError("客户端会话已过期", 409, "session_expired")
        return row

    def open_session(self, account_id, client_id, username):
        if not isinstance(client_id, str) or not 1 <= len(client_id) <= 100:
            raise ValueError("client_id 无效")
        with self.database.transaction() as db:
            account = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if username != account["username"]:
                raise ApiError("接入码与本地论坛账号不符", 403)
            uncertain = db.execute("SELECT 1 FROM operations WHERE account_id=? AND state='submitting'", (account_id,)).fetchone()
            if account["client_id"] and account["client_id"] != client_id:
                if account["session_until"] > self.clock() or uncertain:
                    raise ApiError("该账号已有客户端或尚未核对的提交，请在原客户端恢复", 409, "account_busy")
            session_id = account["session_id"] if account["client_id"] == client_id and account["session_id"] else str(uuid.uuid4())
            db.execute("""UPDATE accounts SET client_id=?,session_id=?,session_until=?,last_seen=?,client_status='在线'
                          WHERE id=?""", (client_id, session_id, self.clock() + 60, self.clock(), account_id))
            db.execute("UPDATE operations SET session_id=? WHERE account_id=? AND state IN ('reserved','submitting')",
                       (session_id, account_id))
            return {"session_id": session_id, "username": account["username"], "tasks": self._tasks(db, account_id),
                    "poll_seconds": 3, "heartbeat_seconds": 10}

    def heartbeat(self, account_id, session_id, status="在线"):
        with self.database.transaction() as db:
            self._session(db, account_id, session_id)
            db.execute("UPDATE accounts SET session_until=?,last_seen=?,client_status=? WHERE id=?",
                       (self.clock() + 60, self.clock(), str(status)[:100], account_id))
            return {"tasks": self._tasks(db, account_id)}

    def close_session(self, account_id, session_id):
        with self.database.transaction() as db:
            db.execute("UPDATE accounts SET session_until=0,client_status='已停止' WHERE id=? AND session_id=?",
                       (account_id, session_id))

    def _tasks(self, db, account_id):
        rows = db.execute("""SELECT t.*, m.question_count, m.synced_at, m.answer_count AS my_answer_count
                             FROM tasks t JOIN members m ON m.task_id=t.id
                             WHERE m.account_id=? ORDER BY t.id""", (account_id,)).fetchall()
        result = []
        for row in rows:
            task = dict(row)
            task["publish_interval_seconds"] = self.interval
            task["questions_registered"] = db.execute("SELECT count(*) FROM questions WHERE task_id=? AND url IS NOT NULL",
                                                       (task["id"],)).fetchone()[0]
            task["questions_satisfied"] = db.execute("SELECT count(*) FROM questions WHERE task_id=? AND url IS NOT NULL AND answer_count>=?",
                                                      (task["id"], task["answer_target"])).fetchone()[0]
            task["questions_expected"] = task["question_target"] * db.execute(
                "SELECT count(*) FROM members WHERE task_id=?", (task["id"],)).fetchone()[0]
            task["answers_done"] = db.execute("""SELECT count(*) FROM answer_attempts a JOIN questions q ON q.id=a.question_id
                                                WHERE q.task_id=? AND a.account_id=? AND a.status IN ('posted','already_answered')""",
                                               (task["id"], account_id)).fetchone()[0]
            result.append(task)
        return result

    def _member(self, db, task_id, account_id):
        task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task or not db.execute("SELECT 1 FROM members WHERE task_id=? AND account_id=?", (task_id, account_id)).fetchone():
            raise ApiError("账号未参与该任务", 403)
        return task

    def sync(self, account_id, session_id, task_id, payload):
        count = integer(payload.get("question_count"))
        course = text(payload.get("course_name"))
        questions = payload.get("questions")
        if not isinstance(questions, list):
            raise ValueError("缺少个人题目列表")
        with self.database.transaction() as db:
            self._session(db, account_id, session_id)
            task = self._member(db, task_id, account_id)
            if len(questions) != min(count, task["question_target"]):
                raise ValueError("个人题目数量与统计不符")
            if task["course_name"] and task["course_name"] != course:
                raise ValueError("客户端打开的课程与其他账号不符")
            urls = [question_url(q["url"], task["forum_url"]) for q in questions]
            if len(set(urls)) != len(urls):
                raise ValueError("个人题目链接重复")
            db.execute("UPDATE tasks SET course_name=? WHERE id=?", (course, task_id))
            db.execute("UPDATE members SET question_count=?,synced_at=? WHERE task_id=? AND account_id=?",
                       (count, self.clock(), task_id, account_id))
            self._answer_count(db, task, account_id, payload)
            for position in range(task["question_target"]):
                db.execute("INSERT OR IGNORE INTO questions(task_id,owner_id,position) VALUES (?,?,?)",
                           (task_id, account_id, position))
            for item, url in zip(questions, urls):
                content, answers = text(item["text"]), integer(item.get("answer_count", 0))
                row = db.execute("SELECT * FROM questions WHERE task_id=? AND url=?", (task_id, url)).fetchone()
                if row and row["owner_id"] != account_id:
                    raise ValueError("题目已属于另一个账号")
                if not row:
                    row = db.execute("""SELECT q.* FROM questions q WHERE task_id=? AND owner_id=? AND url IS NULL
                                        AND NOT EXISTS (SELECT 1 FROM operations o WHERE o.question_id=q.id
                                                        AND o.state IN ('reserved','submitting'))
                                        ORDER BY CASE WHEN text=? THEN 0 ELSE 1 END,position LIMIT 1""",
                                     (task_id, account_id, content)).fetchone()
                if row:
                    busy = db.execute("SELECT 1 FROM operations WHERE question_id=? AND state IN ('reserved','submitting')",
                                      (row["id"],)).fetchone()
                    if not busy:
                        db.execute("UPDATE questions SET text=?,url=?,answer_count=?,observed_at=? WHERE id=?",
                                   (content, url, answers, self.clock(), row["id"]))
            self.schedule(db, task_id)
            self._complete(db, task_id)
            return {"tasks": self._tasks(db, account_id)}

    def _answer_count(self, db, task, account_id, payload):
        if "my_answer_count" in payload:
            count = integer(payload["my_answer_count"])
            db.execute("UPDATE members SET answer_count=?,answer_synced_at=? WHERE task_id=? AND account_id=?",
                       (count, self.clock(), task["id"], account_id))

    def sync_counts(self, account_id, session_id, task_id, payload):
        count = integer(payload.get("question_count"))
        integer(payload.get("my_answer_count"))
        with self.database.transaction() as db:
            self._session(db, account_id, session_id)
            task = self._member(db, task_id, account_id)
            db.execute("UPDATE members SET question_count=? WHERE task_id=? AND account_id=?",
                       (count, task_id, account_id))
            self._answer_count(db, task, account_id, payload)
            self.schedule(db, task_id)
            self._complete(db, task_id)
            return {"tasks": self._tasks(db, account_id)}

    def schedule(self, db, task_id):
        task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task or task["state"] != "running" or not task["course_name"]:
            return
        owners = db.execute("""SELECT DISTINCT q.owner_id FROM questions q JOIN members m
                               ON m.task_id=q.task_id AND m.account_id=q.owner_id WHERE q.task_id=?
                               AND q.url IS NULL AND q.text='' AND m.question_count<?""",
                            (task_id, task["question_target"])).fetchall()
        for owner in owners:
            db.execute("""INSERT OR IGNORE INTO generation_jobs(task_id,kind,owner_id,created_at)
                          VALUES (?,'questions',?,?)""", (task_id, owner[0], self.clock()))
        for question in db.execute("SELECT * FROM questions WHERE task_id=? AND url IS NOT NULL", (task_id,)).fetchall():
            available = db.execute("SELECT count(*) FROM answer_drafts WHERE question_id=? AND state IN ('ready','reserved')",
                                   (question["id"],)).fetchone()[0]
            if available < self.answer_need(db, question):
                db.execute("""INSERT OR IGNORE INTO generation_jobs(task_id,kind,question_id,created_at)
                              VALUES (?,'answers',?,?)""", (task_id, question["id"], self.clock()))

    def attempted(self, db, question, account_id):
        return db.execute("""SELECT 1 FROM answer_attempts a JOIN questions q ON q.id=a.question_id
                             WHERE a.account_id=? AND (q.id=? OR
                               (q.url=? AND a.status IN ('posted','already_answered'))) LIMIT 1""",
                          (account_id, question["id"], question["url"])).fetchone() is not None

    def eligible_count(self, db, question):
        accounts = db.execute("SELECT account_id FROM members WHERE task_id=? AND account_id<>?",
                              (question["task_id"], question["owner_id"])).fetchall()
        return sum(not self.attempted(db, question, account[0]) for account in accounts)

    def answer_need(self, db, question):
        task = db.execute("SELECT * FROM tasks WHERE id=?", (question["task_id"],)).fetchone()
        eligible = [member for member in db.execute("SELECT * FROM members WHERE task_id=? AND account_id<>?",
                    (task["id"], question["owner_id"])) if not self.attempted(db, question, member["account_id"])]
        personal = sum(member["answer_count"] is not None and member["answer_count"] < task["account_answer_target"]
                       for member in eligible)
        return min(len(eligible), max(0, task["answer_target"] - question["answer_count"], personal))

    def _expire(self, db):
        expired = db.execute("SELECT * FROM operations WHERE state='reserved' AND reserved_until<?", (self.clock(),)).fetchall()
        for operation in expired:
            db.execute("UPDATE operations SET state='cancelled' WHERE id=?", (operation["id"],))
            if operation["draft_id"]:
                db.execute("UPDATE answer_drafts SET state='ready' WHERE id=?", (operation["draft_id"],))

    def _operation(self, db, operation, *, recover=False):
        operation = dict(operation)
        operation["question"] = dict(db.execute("SELECT * FROM questions WHERE id=?", (operation["question_id"],)).fetchone())
        operation["task"] = dict(db.execute("SELECT * FROM tasks WHERE id=?", (operation["task_id"],)).fetchone())
        operation["task"]["publish_interval_seconds"] = self.interval
        operation["recover"] = recover
        return operation

    def _reserve(self, db, account_id, session_id, task, question, kind, draft=None):
        operation_id = str(uuid.uuid4())
        body = draft["text"] if draft else question["text"] if kind == "question" else ""
        db.execute("""INSERT INTO operations(id,task_id,account_id,question_id,kind,session_id,reserved_until,body,draft_id,created_at)
                      VALUES (?,?,?,?,?,?,?,?,?,?)""", (operation_id, task["id"], account_id, question["id"], kind,
                                                         session_id, self.clock() + 60, body,
                                                         draft["id"] if draft else None, self.clock()))
        if draft:
            db.execute("UPDATE answer_drafts SET state='reserved' WHERE id=?", (draft["id"],))
        db.execute("UPDATE accounts SET task_cursor=? WHERE id=?", (task["id"], account_id))
        operation = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
        return {"operation": self._operation(db, operation)}

    def claim(self, account_id, session_id):
        with self.database.transaction() as db:
            account = self._session(db, account_id, session_id)
            self._expire(db)
            existing = db.execute("SELECT * FROM operations WHERE account_id=? AND state IN ('reserved','submitting')",
                                  (account_id,)).fetchone()
            if existing:
                return {"operation": self._operation(db, existing, recover=existing["state"] == "submitting")}
            tasks = db.execute("""SELECT t.*,m.synced_at,m.question_count,m.answer_count AS my_answer_count,m.answer_synced_at
                                  FROM tasks t JOIN members m ON m.task_id=t.id
                                  WHERE m.account_id=? AND t.state='running'
                                  ORDER BY CASE WHEN t.id>? THEN 0 ELSE 1 END,t.id""",
                               (account_id, account["task_cursor"])).fetchall()
            for task in tasks:
                registered = db.execute("SELECT count(*) FROM questions WHERE task_id=? AND owner_id=? AND url IS NOT NULL",
                                        (task["id"], account_id)).fetchone()[0]
                if task["synced_at"] is None or registered < min(task["question_count"], task["question_target"]):
                    return {"sync_task": dict(task)}
                if task["account_answer_target"] and (task["my_answer_count"] is None or
                        task["my_answer_count"] < task["account_answer_target"] and
                        (task["answer_synced_at"] is None or task["answer_synced_at"] < self.clock() - 30)):
                    return {"sync_counts_task": dict(task)}
                self._complete(db, task["id"])
                self.schedule(db, task["id"])
                own = db.execute("""SELECT q.* FROM questions q WHERE task_id=? AND owner_id=? AND url IS NULL AND text<>''
                                     AND NOT EXISTS (SELECT 1 FROM operations o WHERE o.question_id=q.id
                                       AND o.state IN ('reserved','submitting','failed')) ORDER BY position LIMIT 1""",
                                 (task["id"], account_id)).fetchone()
                if own:
                    return self._reserve(db, account_id, session_id, task, own, "question")
                personal = (task["my_answer_count"] or 0) < task["account_answer_target"]
                questions = db.execute("""SELECT q.* FROM questions q WHERE task_id=? AND url IS NOT NULL AND (answer_count<? OR ?)
                                          AND NOT EXISTS (SELECT 1 FROM operations o JOIN questions other ON other.id=o.question_id
                                                          WHERE o.state IN ('reserved','submitting')
                                                            AND (o.question_id=q.id OR other.url=q.url))
                                          ORDER BY CASE WHEN answer_count<? THEN 0 ELSE 1 END,answer_count,position,id""",
                                       (task["id"], task["answer_target"], personal, task["answer_target"])).fetchall()
                for question in questions:
                    if question["observed_at"] < self.clock() - 30:
                        return self._reserve(db, account_id, session_id, task, question, "inspect")
                    if question["owner_id"] == account_id or self.attempted(db, question, account_id):
                        continue
                    draft = db.execute("SELECT * FROM answer_drafts WHERE question_id=? AND state='ready' ORDER BY id LIMIT 1",
                                       (question["id"],)).fetchone()
                    if draft:
                        return self._reserve(db, account_id, session_id, task, question, "answer", draft)
            summary = self._tasks(db, account_id)
            reason = "等待生成内容或可回答账号"
            if not summary:
                reason = "等待管理员分配任务"
            elif all(task["state"] == "complete" for task in summary):
                reason = "已分配任务均已完成，等待新任务"
            elif not any(task["state"] == "running" for task in summary):
                reason = "任务尚未启动或已暂停，等待管理员恢复"
            elif any((task["my_answer_count"] or 0) < task["account_answer_target"] for task in summary if task["state"] == "running"):
                reason = "个人回答目标未达标，等待可回答题目或内容生成"
            return {"operation": None, "tasks": summary, "wait_reason": reason}

    def start_operation(self, account_id, session_id, operation_id):
        with self.database.transaction() as db:
            self._session(db, account_id, session_id)
            operation = db.execute("SELECT * FROM operations WHERE id=? AND account_id=?", (operation_id, account_id)).fetchone()
            if not operation:
                raise ApiError("操作已删除，请重新领取", 404, "operation_inactive")
            if operation["state"] == "submitting":
                return {"allowed": True}
            task = self._member(db, operation["task_id"], account_id)
            if operation["state"] != "reserved" or operation["reserved_until"] < self.clock() or task["state"] != "running":
                raise ApiError("操作已过期或任务已暂停，请重新领取", 409, "operation_inactive")
            db.execute("UPDATE operations SET state='submitting' WHERE id=?", (operation_id,))
            return {"allowed": True}

    def operation_status(self, account_id, session_id, operation_id):
        with self.database.transaction() as db:
            self._session(db, account_id, session_id)
            operation = db.execute("SELECT * FROM operations WHERE id=? AND account_id=?", (operation_id, account_id)).fetchone()
            if not operation:
                raise ApiError("操作已删除，请重新领取", 404, "operation_inactive")
            return self._operation(db, operation, recover=operation["state"] == "submitting")

    def finish(self, account_id, session_id, operation_id, result):
        status = result.get("status")
        if status not in {"posted", "already_answered", "threshold_met", "unavailable", "inspected", "failed", "uncertain", "not_needed", "cancelled"}:
            raise ValueError("操作结果无效")
        with self.database.transaction() as db:
            self._session(db, account_id, session_id)
            operation = db.execute("SELECT * FROM operations WHERE id=? AND account_id=?", (operation_id, account_id)).fetchone()
            if not operation:
                if status == "cancelled":
                    return {"accepted": True, "duplicate": True}
                raise ApiError("操作已删除，请重新领取", 404, "operation_inactive")
            if operation["result"]:
                return {"accepted": True, "duplicate": True}
            if status == "cancelled" and operation["state"] == "cancelled":
                return {"accepted": True, "duplicate": True}
            if operation["state"] not in {"reserved", "submitting"}:
                raise ApiError("操作已失效", 409, "operation_inactive")
            if status == "cancelled" and operation["state"] == "submitting":
                raise ApiError("提交中的操作需要核对，不能直接取消", 409, "needs_reconciliation")
            task = self._member(db, operation["task_id"], account_id)
            if status == "uncertain":
                db.execute("UPDATE operations SET state='submitting',error=? WHERE id=?",
                           (str(result.get("error", "等待原客户端核对"))[:1000], operation_id))
                return {"accepted": True, "reconcile": True}
            if operation["kind"] == "question" and status in {"posted", "not_needed"}:
                if status == "posted":
                    content = text(result.get("text"))
                    if content != normalize(operation["body"]):
                        raise ValueError("发布题目与分配正文不符")
                    url = question_url(result.get("url", ""), task["forum_url"])
                    db.execute("UPDATE questions SET text=?,url=?,observed_at=? WHERE id=?",
                               (content, url, self.clock(), operation["question_id"]))
                db.execute("UPDATE members SET question_count=?,synced_at=? WHERE task_id=? AND account_id=?",
                           (integer(result.get("question_count")), self.clock(), task["id"], account_id))
            related_tasks = {task["id"]}
            self._answer_count(db, task, account_id, result)
            if operation["kind"] == "answer" and "my_answer_count" not in result:
                db.execute("UPDATE members SET answer_synced_at=NULL WHERE task_id=? AND account_id=?", (task["id"], account_id))
            if "answer_count" in result:
                count = integer(result["answer_count"])
                url = db.execute("SELECT url FROM questions WHERE id=?", (operation["question_id"],)).fetchone()[0]
                db.execute("UPDATE questions SET answer_count=?,observed_at=? WHERE id=? OR (url=? AND url IS NOT NULL)",
                           (count, self.clock(), operation["question_id"], url))
                related_tasks.update(row[0] for row in db.execute("SELECT task_id FROM questions WHERE url=?", (url,)))
            if operation["kind"] == "answer" and status in {"posted", "already_answered", "unavailable", "failed"}:
                db.execute("INSERT OR REPLACE INTO answer_attempts(question_id,account_id,status) VALUES (?,?,?)",
                           (operation["question_id"], account_id, status))
            if operation["draft_id"]:
                db.execute("UPDATE answer_drafts SET state=? WHERE id=?",
                           ("used" if status == "posted" else "ready", operation["draft_id"]))
            error = str(result.get("error", ""))[:1000]
            db.execute("UPDATE operations SET state=?,result=?,error=? WHERE id=?",
                       ("failed" if status == "failed" else "cancelled" if status == "cancelled" else "done",
                        json.dumps(result, ensure_ascii=False), error, operation_id))
            if error:
                db.execute("UPDATE tasks SET error=? WHERE id=?", (error, task["id"]))
            for related in related_tasks:
                self.schedule(db, related)
                self._complete(db, related)
            return {"accepted": True, "duplicate": False}

    def _complete(self, db, task_id):
        task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task["state"] != "running":
            return
        members = db.execute("SELECT * FROM members WHERE task_id=?", (task_id,)).fetchall()
        registered, satisfied = db.execute("""SELECT count(*),coalesce(sum(answer_count>=?),0) FROM questions
                                             WHERE task_id=? AND url IS NOT NULL""", (task["answer_target"], task_id)).fetchone()
        busy = db.execute("SELECT 1 FROM operations WHERE task_id=? AND state IN ('reserved','submitting')", (task_id,)).fetchone()
        if members and all(m["synced_at"] is not None and m["question_count"] >= task["question_target"]
                           and (task["account_answer_target"] == 0 or
                                m["answer_count"] is not None and m["answer_count"] >= task["account_answer_target"])
                           for m in members):
            if registered == satisfied == len(members) * task["question_target"] and not busy:
                db.execute("UPDATE tasks SET state='complete',error='' WHERE id=?", (task_id,))

    def dashboard(self):
        accounts = self.database.rows("SELECT id,username,alias,last_seen,session_until,client_status FROM accounts ORDER BY id")
        tasks = self.database.rows("SELECT * FROM tasks ORDER BY id DESC")
        for task in tasks:
            task["members"] = self.database.rows("""SELECT a.username,a.alias,m.question_count,m.synced_at,m.answer_count FROM members m
                                                    JOIN accounts a ON a.id=m.account_id WHERE m.task_id=? ORDER BY a.id""", (task["id"],))
            task["questions"] = self.database.rows("""SELECT q.*,a.username,a.alias FROM questions q JOIN accounts a ON a.id=q.owner_id
                                                       WHERE q.task_id=? ORDER BY owner_id,position""", (task["id"],))
            task["pending_reconciliation"] = self.database.rows(
                "SELECT id,error FROM operations WHERE task_id=? AND state='submitting'", (task["id"],))
        return {"accounts": accounts, "tasks": tasks, "now": self.clock()}
