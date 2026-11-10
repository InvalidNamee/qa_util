import threading

import pytest

from qa_util_server.database import Database
from qa_util_server.service import ApiError, Service
from server_support import enroll, forum_url, seed


def generate(app):
    for _ in range(30):
        if not app.extensions["qa_worker"].step():
            return
    pytest.fail("生成任务没有收敛")


def test_running_and_unreconciled_tasks_cannot_be_edited(app, service):
    aid, sid, _ = enroll(service, "a")
    task = service.create_task("原任务", forum_url(), 2, 1, [aid])
    service.set_task_state(task, "running")
    seed(service, task, aid, sid, 0, target=2)
    generate(app)
    original = service.database.rows("SELECT * FROM tasks")
    with pytest.raises(ValueError, match="先暂停"):
        service.update_task(task, "修改", forum_url(), 3, 1, [aid])
    assert service.database.rows("SELECT * FROM tasks") == original
    operation = service.claim(aid, sid)["operation"]
    service.start_operation(aid, sid, operation["id"])
    service.set_task_state(task, "paused")
    with pytest.raises(ValueError, match="核对"):
        service.update_task(task, "修改", forum_url(), 3, 1, [aid])
    service.finish(aid, sid, operation["id"], {"status": "posted", "text": operation["body"],
        "url": forum_url().replace("/home/123", "/questionDetail/123/999"), "question_count": 1})
    service.update_task(task, "修改", forum_url(), 3, 1, [aid])
    assert service.database.rows("SELECT state FROM tasks") == [{"state": "paused"}]
    assert service.database.rows("SELECT count(*) AS n FROM questions WHERE url IS NOT NULL")[0]["n"] == 1


def test_completed_task_can_increase_target_and_only_generate_missing_questions(app, service):
    aid, sid, _ = enroll(service, "a")
    task = service.create_task("完成后追加", forum_url(), 1, 1, [aid])
    service.set_task_state(task, "running")
    seed(service, task, aid, sid, 1, answers=1)
    assert service.database.rows("SELECT state FROM tasks")[0]["state"] == "complete"
    old = service.database.rows("SELECT id,url FROM questions")
    service.update_task(task, "追加目标", forum_url() + "&VNK=new", 2, 1, [aid], account_answer_target=1)
    assert service.database.rows("SELECT course_name,state FROM tasks") == [{"course_name": "测试课程", "state": "paused"}]
    service.set_task_state(task, "running")
    assert service.claim(aid, sid)["sync_task"]["question_target"] == 2
    seed(service, task, aid, sid, 1, target=2, answers=1, my_answers=1)
    generate(app)
    assert service.database.rows("SELECT id,url FROM questions WHERE url IS NOT NULL") == old
    assert app.extensions["qa_worker"].generator.calls == [("questions", 1)]
    assert service.claim(aid, sid)["operation"]["kind"] == "question"


@pytest.mark.parametrize("existing", [0, 1, 2, 5])
def test_increased_target_credits_existing_questions_before_generation(app, service, existing):
    aid, sid, _ = enroll(service, "a")
    task = service.create_task("提高配额", forum_url(), 1, 1, [aid])
    service.set_task_state(task, "running")
    seed(service, task, aid, sid, existing)
    old = service.database.rows("SELECT id,url FROM questions WHERE url IS NOT NULL")
    service.set_task_state(task, "paused")
    service.update_task(task, "提高配额", forum_url(), 3, 1, [aid])
    service.set_task_state(task, "running")
    assert service.claim(aid, sid)["sync_task"]["question_target"] == 3
    seed(service, task, aid, sid, existing, target=3)
    generate(app)
    questions = service.database.rows("SELECT * FROM active_questions ORDER BY position")
    assert len(questions) == 3
    assert [{"id": q["id"], "url": q["url"]} for q in questions[:len(old)]] == old
    calls = app.extensions["qa_worker"].generator.calls
    assert sum(count for kind, count in calls if kind == "questions") == max(0, 3 - existing)


def test_editing_legacy_task_preserves_zero_answer_goal_until_enabled(service):
    aid, _, _ = enroll(service, "a")
    task = service.create_task("旧规则", forum_url(), 2, 1, [aid])
    with service.database.transaction() as db:
        db.execute("UPDATE tasks SET account_answer_target=0 WHERE id=?", (task,))
    service.update_task(task, "旧规则改名", forum_url(), 2, 1, [aid], account_answer_target=0)
    assert service.database.rows("SELECT account_answer_target FROM tasks") == [{"account_answer_target": 0}]
    service.update_task(task, "启用个人目标", forum_url(), 2, 1, [aid], account_answer_target=1)
    with pytest.raises(ValueError):
        service.update_task(task, "关闭个人目标", forum_url(), 2, 1, [aid], account_answer_target=0)


def test_reduced_pool_can_complete_without_counting_excess_or_removed_accounts(service):
    accounts = [enroll(service, name) for name in "abc"]
    task = service.create_task("缩小目标", forum_url(), 3, 2, [a[0] for a in accounts])
    service.set_task_state(task, "running")
    for aid, sid, _ in accounts:
        seed(service, task, aid, sid, 3, target=3, answers=1)
    old = service.database.rows("SELECT id,url FROM questions")
    service.set_task_state(task, "paused")
    service.update_task(task, "缩小目标", forum_url(), 1, 1, [a[0] for a in accounts[:2]])
    assert len(service.dashboard()["tasks"][0]["questions"]) == 2
    assert service.database.rows("SELECT id,url FROM questions") == old
    service.set_task_state(task, "running")
    for aid, sid, _ in accounts[:2]:
        seed(service, task, aid, sid, 3, target=1, answers=1)
    assert service.database.rows("SELECT state FROM tasks")[0]["state"] == "complete"
    assert service.heartbeat(accounts[2][0], accounts[2][1])["tasks"] == []


def test_removing_and_restoring_members_and_targets_keeps_answers_and_releases_reservations(app, service):
    a, b, c = [enroll(service, name) for name in "abc"]
    task = service.create_task("保留历史", forum_url(), 2, 2, [a[0], b[0], c[0]])
    service.set_task_state(task, "running")
    for aid, sid, _ in (a, b, c):
        seed(service, task, aid, sid, 2, target=2)
    generate(app)
    posted = service.claim(b[0], b[1])["operation"]
    service.finish(b[0], b[1], posted["id"], {"status": "posted", "answer_count": 1})
    reserved = service.claim(c[0], c[1])["operation"]
    old_pool = service.database.rows("SELECT id,url FROM questions ORDER BY id")
    service.set_task_state(task, "paused")
    service.update_task(task, "暂时减少", forum_url(), 1, 1, [a[0], b[0]])
    with pytest.raises(ApiError) as error:
        service.start_operation(c[0], c[1], reserved["id"])
    assert error.value.code == "operation_inactive"
    assert service.finish(c[0], c[1], reserved["id"], {"status": "cancelled"})["accepted"]
    assert service.database.rows("SELECT state FROM answer_drafts WHERE id=?", (reserved["draft_id"],)) == [{"state": "ready"}]
    service.update_task(task, "恢复原范围", forum_url(), 2, 2, [a[0], b[0], c[0]])
    service.set_task_state(task, "running")
    for aid, sid, _ in (a, b, c):
        seed(service, task, aid, sid, 2, target=2)
    assert service.database.rows("SELECT id,url FROM questions ORDER BY id") == old_pool
    with service.database.transaction() as db:
        question = db.execute("SELECT * FROM questions WHERE id=?", (posted["question_id"],)).fetchone()
        assert service.attempted(db, question, b[0])
    restored = Service(Database(service.database.path))
    assert len(restored.dashboard()["tasks"][0]["questions"]) == 6
    assert not restored.database.rows("PRAGMA foreign_key_check")


def test_editing_failed_task_allows_retry_without_hidden_failed_operation(app, service):
    aid, sid, _ = enroll(service, "a")
    task = service.create_task("失败恢复", forum_url(), 1, 1, [aid])
    service.set_task_state(task, "running")
    seed(service, task, aid, sid, 0)
    generate(app)
    failed = service.claim(aid, sid)["operation"]
    service.finish(aid, sid, failed["id"], {"status": "failed", "error": "发布失败"})
    service.set_task_state(task, "paused")
    service.update_task(task, "失败恢复", forum_url(), 1, 1, [aid])
    service.set_task_state(task, "running")
    retry = service.claim(aid, sid)["operation"]
    assert retry["id"] != failed["id"] and retry["body"] == failed["body"]


def test_changing_forum_resets_only_edited_task_and_ignores_old_paused_sync(app, service):
    aid, sid, _ = enroll(service, "a")
    tasks = [service.create_task(f"任务{i}", forum_url(), 1, 1, [aid]) for i in range(2)]
    for task in tasks:
        service.set_task_state(task, "running")
        seed(service, task, aid, sid, 1)
    generate(app)
    old_other_pool = service.database.rows("SELECT * FROM questions WHERE task_id=?", (tasks[1],))
    task = tasks[0]
    service.set_task_state(task, "paused")
    service.update_task(task, "新论坛", forum_url(789), 2, 1, [aid])
    seed(service, task, aid, sid, 1)  # 暂停之前开始的只读同步迟到，不得写回。
    assert not service.database.rows("SELECT * FROM questions WHERE task_id=?", (task,))
    assert service.database.rows("SELECT course_name FROM tasks WHERE id=?", (task,)) == [{"course_name": ""}]
    assert service.database.rows("SELECT question_count,answer_count FROM members WHERE task_id=?", (task,)) == [
        {"question_count": 0, "answer_count": None}]
    assert service.database.rows("SELECT * FROM questions WHERE task_id=?", (tasks[1],)) == old_other_pool
    service.set_task_state(task, "running")
    assert service.claim(aid, sid)["sync_task"]["forum_url"] == forum_url(789)
    seed(service, task, aid, sid, 0, target=2, course=789)
    generate(app)
    assert len(service.database.rows("SELECT * FROM active_questions WHERE task_id=?", (task,))) == 2
    assert not service.database.rows("PRAGMA foreign_key_check")


@pytest.mark.parametrize("fail", [False, True])
def test_edit_during_generation_discards_late_content_and_error(app, service, fail):
    aid, sid, _ = enroll(service, "a")
    task = service.create_task("修改生成配额", forum_url(), 3, 1, [aid])
    service.set_task_state(task, "running")
    seed(service, task, aid, sid, 0, target=3)
    worker = app.extensions["qa_worker"]
    original = worker.generator.questions
    entered, release = threading.Event(), threading.Event()

    def delayed(*_args):
        entered.set()
        assert release.wait(timeout=5)
        if fail:
            raise RuntimeError("旧生成任务的迟到错误")
        return ["旧生成问题？"] * 3

    worker.generator.questions = delayed
    thread = threading.Thread(target=worker.step)
    thread.start()
    try:
        assert entered.wait(timeout=3)
        service.set_task_state(task, "paused")
        service.update_task(task, "修改生成配额", forum_url(), 1, 1, [aid])
        service.set_task_state(task, "running")
        seed(service, task, aid, sid, 0)
    finally:
        release.set()
        thread.join(timeout=5)
        worker.generator.questions = original
    assert not thread.is_alive()
    assert all(not q["text"] for q in service.database.rows("SELECT text FROM questions"))
    assert service.database.rows("SELECT error FROM tasks") == [{"error": ""}]
    generate(app)
    assert worker.generator.calls == [("questions", 1)]
    assert sum(bool(q["text"]) for q in service.database.rows("SELECT text FROM questions")) == 1


@pytest.mark.parametrize("changes", [
    {"accounts": []}, {"accounts": [999]}, {"question_target": 0},
    {"account_answer_target": 3}, {"answer_target": 0}, {"forum_url": "bad-url"},
])
def test_invalid_edits_do_not_change_task_or_existing_progress(service, changes):
    aid, _, _ = enroll(service, "a")
    values = dict(name="原任务", forum_url=forum_url(), question_target=2, answer_target=1,
                  accounts=[aid], account_answer_target=2)
    task = service.create_task(**values)
    original = service.database.rows("SELECT * FROM tasks")
    with pytest.raises(ValueError):
        service.update_task(task, **(values | changes))
    assert service.database.rows("SELECT * FROM tasks") == original
    assert service.database.rows("SELECT account_id FROM members") == [{"account_id": aid}]


def test_admin_edit_requires_csrf_and_only_shows_when_stopped(app, service):
    aid, _, _ = enroll(service, "a")
    service.set_alias(aid, "别名甲")
    task = service.create_task('任务"<&', forum_url(), 2, 1, [aid])
    path = f"/tasks/{task}/edit"
    data = dict(name="改后任务", forum_url=forum_url(), question_target=3, account_answer_target=2,
                answer_target=2, accounts=str(aid), csrf="test-csrf")
    with app.test_client() as client:
        with client.session_transaction() as session:
            session["csrf"] = "test-csrf"
        assert client.post(path, data=data).status_code == 302  # 尚未登录管理端。
        with client.session_transaction() as session:
            session["admin"] = True
        html = client.get("/").text
        assert f'id="task-edit-{task}"' in html
        assert '任务&#34;&lt;&amp;' in html and "别名甲" in html
        assert client.post(path, data=data | {"csrf": "bad"}).status_code == 400
        service.set_task_state(task, "running")
        assert f'id="task-edit-{task}"' not in client.get("/").text
        assert client.post(path, data=data).status_code == 400
        service.set_task_state(task, "paused")
        assert client.post(path, data=data).status_code == 302
        assert client.post("/tasks/999/edit", data=data).status_code == 404
    assert service.database.rows("SELECT name,question_target,account_answer_target,answer_target,state FROM tasks") == [
        dict(name="改后任务", question_target=3, account_answer_target=2, answer_target=2, state="paused")]
