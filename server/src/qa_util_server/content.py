import threading


class GenerationWorker:
    def __init__(self, service, generator, log):
        self.service, self.generator, self.log = service, generator, log
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        with self.service.database.transaction() as db:
            db.execute("UPDATE generation_jobs SET state='pending' WHERE state='running'")
        self.thread = threading.Thread(target=self.run, name="qa-content", daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)

    def run(self):
        while not self.stop_event.is_set():
            if not self.step():
                self.stop_event.wait(0.5)

    def step(self):
        with self.service.database.transaction() as db:
            job = db.execute("""SELECT j.*,t.course_name,t.answer_target FROM generation_jobs j
                                JOIN tasks t ON t.id=j.task_id WHERE j.state='pending' AND t.state='running'
                                ORDER BY j.id LIMIT 1""").fetchone()
            if not job:
                return False
            job = dict(job)
            db.execute("UPDATE generation_jobs SET state='running' WHERE id=?", (job["id"],))
            if job["kind"] == "questions":
                selected = db.execute("""SELECT id FROM questions WHERE task_id=? AND owner_id=? AND url IS NULL
                                         AND text='' ORDER BY position LIMIT 10""", (job["task_id"], job["owner_id"])).fetchall()
                count = len(selected)
                existing = [row[0] for row in db.execute("SELECT text FROM questions WHERE task_id=? AND text<>''", (job["task_id"],))]
            else:
                question = db.execute("SELECT * FROM questions WHERE id=?", (job["question_id"],)).fetchone()
                available = db.execute("SELECT count(*) FROM answer_drafts WHERE question_id=? AND state IN ('ready','reserved')",
                                       (question["id"],)).fetchone()[0]
                count = max(0, min(10, self.service.answer_need(db, question) - available))
        try:
            if count:
                if job["kind"] == "questions":
                    generated = self.generator.questions(job["course_name"], existing, count)
                else:
                    generated = self.generator.answers(job["course_name"], question["text"], count)
                if len(generated) != count:
                    raise ValueError("生成数量不符")
            if self.stop_event.is_set():
                return True  # 退出中的旧工作线程不覆盖下一次启动恢复的任务。
            with self.service.database.transaction() as db:
                if not db.execute("SELECT 1 FROM generation_jobs WHERE id=? AND state='running'", (job["id"],)).fetchone():
                    return True
                if count and job["kind"] == "questions":
                    for slot, value in zip(selected, generated):
                        db.execute("UPDATE questions SET text=? WHERE id=? AND text='' AND url IS NULL", (value, slot["id"]))
                elif count:
                    db.executemany("INSERT INTO answer_drafts(question_id,text) VALUES (?,?)",
                                   [(job["question_id"], value) for value in generated])
                db.execute("UPDATE generation_jobs SET state='done',error='' WHERE id=?", (job["id"],))
                self.service.schedule(db, job["task_id"])
            self.log.info("生成任务 %s 完成，类型=%s，数量=%s", job["id"], job["kind"], count)
        except Exception as error:
            message = str(error)
            llm = getattr(self.generator, "llm", None)
            if llm:
                message = message.replace(llm.api_key, "***")
            with self.service.database.transaction() as db:
                changed = db.execute("UPDATE generation_jobs SET state='failed',error=? WHERE id=? AND state='running'",
                                     (message, job["id"])).rowcount
                if not changed:
                    return True
                db.execute("UPDATE tasks SET error=? WHERE id=?", (message, job["task_id"]))
            self.log.error("生成任务 %s 失败：%s", job["id"], message)
        return True
