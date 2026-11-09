import sqlite3

import pytest

from qa_util_server.database import Database, SCHEMA
from qa_util_server.service import ApiError
from server_support import enroll, forum_url, seed


def generate(app):
    for _ in range(30):
        if not app.extensions["qa_worker"].step():
            return
    pytest.fail("生成任务没有收敛")


@pytest.mark.parametrize("goal", [0, -1, 3, True])
def test_account_answer_goal_must_be_positive_and_not_above_question_goal(service, goal):
    aid, _, _ = enroll(service, "a")
    with pytest.raises(ValueError):
        service.create_task("目标校验", forum_url(), 2, 1, [aid], account_answer_target=goal)
    assert not service.database.rows("SELECT * FROM tasks")


def test_historical_personal_answers_count_and_missing_goal_can_exceed_question_threshold(app, service):
    a, b = [enroll(service, name) for name in "ab"]
    task = service.create_task("个人目标", forum_url(), 2, 1, [a[0], b[0]], account_answer_target=2)
    service.set_task_state(task, "running")
    seed(service, task, a[0], a[1], 2, target=2, answers=1, my_answers=2)
    seed(service, task, b[0], b[1], 2, target=2, answers=1, my_answers=1)
    assert service.database.rows("SELECT state FROM tasks")[0]["state"] == "running"
    generate(app)
    operation = service.claim(b[0], b[1])["operation"]
    assert operation["kind"] == "answer" and operation["question"]["owner_id"] == a[0]
    assert operation["question"]["answer_count"] == 1
    result = {"status": "posted", "answer_count": 2, "my_answer_count": 2}
    service.finish(b[0], b[1], operation["id"], result)
    service.finish(b[0], b[1], operation["id"], result)
    assert service.heartbeat(b[0], b[1])["tasks"][0]["my_answer_count"] == 2
    assert service.database.rows("SELECT state FROM tasks")[0]["state"] == "complete"


def test_personal_goal_does_not_replace_each_question_threshold(app, service):
    a, b = [enroll(service, name) for name in "ab"]
    task = service.create_task("同时达标", forum_url(), 1, 2, [a[0], b[0]])
    service.set_task_state(task, "running")
    for aid, sid, _ in (a, b):
        seed(service, task, aid, sid, 1, my_answers=1)
    generate(app)
    for account in (a, b):
        operation = service.claim(account[0], account[1])["operation"]
        assert operation["kind"] == "answer"
        service.finish(account[0], account[1], operation["id"], {
            "status": "posted", "answer_count": 1, "my_answer_count": 2})
    assert service.database.rows("SELECT state FROM tasks")[0]["state"] == "running"
    assert service.claim(a[0], a[1])["operation"] is None
    assert service.claim(b[0], b[1])["operation"] is None


def test_waiting_client_refreshes_personal_counts_and_credits_external_answers(service):
    now = [1000.0]
    service.clock = lambda: now[0]
    aid, sid, _ = enroll(service, "a")
    task = service.create_task("同步历史回答", forum_url(), 1, 1, [aid])
    service.set_task_state(task, "running")
    seed(service, task, aid, sid, 1, answers=1, my_answers=0)
    assert service.claim(aid, sid)["operation"] is None
    now[0] += 31
    assert service.claim(aid, sid)["sync_counts_task"]["id"] == task
    service.sync_counts(aid, sid, task, {"question_count": 1, "my_answer_count": 1})
    assert service.database.rows("SELECT state FROM tasks")[0]["state"] == "complete"


def test_alias_changes_only_management_display_and_is_escaped(app, service):
    aid, token = service.create_account("student-001", "小王")
    with app.test_client() as client:
        with client.session_transaction() as session:
            session.update(admin=True, csrf="test-csrf")
        assert "小王" in client.get("/").text
        assert client.post(f"/accounts/{aid}/alias", data={"alias": "别名"}).status_code == 400
        alias = '<script>alert("test")</script>'
        assert client.post(f"/accounts/{aid}/alias", data={"alias": alias, "csrf": "test-csrf"}).status_code == 302
        assert "&lt;script&gt;" in client.get("/").text
        assert "<script>alert" not in client.get("/").text
        assert client.post(f"/accounts/{aid}/alias", data={"alias": "x" * 101, "csrf": "test-csrf"}).status_code == 400
        assert client.post(f"/accounts/{aid}/alias", data={"alias": "", "csrf": "test-csrf"}).status_code == 302
        assert client.post("/accounts", data={"username": "student-002", "alias": "小李", "csrf": "test-csrf"}).status_code == 200
    assert service.authenticate(token) == aid
    service.open_session(aid, "device", "student-001")
    with pytest.raises(ApiError, match="不符"):
        service.open_session(aid, "device", "小王")


def test_admin_task_form_validates_personal_goal(app, service):
    aid, _, _ = enroll(service, "a")
    with app.test_client() as client:
        with client.session_transaction() as session:
            session.update(admin=True, csrf="test-csrf")
        data = {"name": "目标表单", "forum_url": forum_url(), "question_target": 2, "answer_target": 1,
                "account_answer_target": 3, "accounts": str(aid), "csrf": "test-csrf"}
        response = client.post("/tasks", data=data)
        assert response.status_code == 400 and "不能大于" in response.text
        data["account_answer_target"] = 2
        assert client.post("/tasks", data=data).status_code == 302
    assert service.database.rows("SELECT account_answer_target FROM tasks")[0]["account_answer_target"] == 2


def test_existing_database_migration_preserves_tasks_accounts_and_is_idempotent(tmp_path):
    path = tmp_path / "old.sqlite3"
    old_schema = "\n".join(line for line in SCHEMA.splitlines() if not any(
        field in line for field in (" alias TEXT", " account_answer_target INTEGER", " answer_count INTEGER, answer_synced_at")))
    with sqlite3.connect(path) as db:
        db.executescript(old_schema)
        db.execute("INSERT INTO accounts(id,username,token_hash) VALUES (1,'old-account','hashed-token')")
        db.execute("INSERT INTO tasks(id,name,forum_url,question_target,answer_target,state,created_at) VALUES (1,'旧任务',?,3,1,'paused',1)", (forum_url(),))
        db.execute("INSERT INTO members(task_id,account_id,question_count,synced_at) VALUES (1,1,3,1)")
    Database(path)
    reopened = Database(path)
    assert reopened.rows("SELECT username,alias,token_hash FROM accounts") == [
        {"username": "old-account", "alias": "", "token_hash": "hashed-token"}]
    assert reopened.rows("SELECT state,account_answer_target FROM tasks") == [{"state": "paused", "account_answer_target": 0}]
    assert reopened.rows("SELECT question_count,answer_count FROM members") == [{"question_count": 3, "answer_count": None}]
    with pytest.raises(sqlite3.IntegrityError):
        with reopened.transaction() as db:
            db.execute("UPDATE tasks SET account_answer_target=4 WHERE id=1")
