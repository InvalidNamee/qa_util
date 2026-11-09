import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from qa_util_server.database import Database
from qa_util_server.service import ApiError, Service
from server_support import enroll, forum_url, seed


def generate_all(app):
    worker = app.extensions["qa_worker"]
    for _ in range(30):
        if not worker.step():
            return
    pytest.fail("生成任务没有收敛")


def test_existing_external_answers_only_fill_deficit_and_reports_are_idempotent(app, service):
    accounts = [enroll(service, name) for name in "abc"]
    task = service.create_task("任务", forum_url(), 1, 5, [a[0] for a in accounts])
    service.set_task_state(task, "running")
    for i, (aid, sid, _) in enumerate(accounts):
        seed(service, task, aid, sid, 1, answers=3 if i == 0 else 5)
    generate_all(app)
    assert app.extensions["qa_worker"].generator.calls == [("answers", 2)]
    b, c = accounts[1:]
    first = service.claim(b[0], b[1])["operation"]
    assert first["kind"] == "answer" and first["question"]["owner_id"] == accounts[0][0]
    assert service.claim(c[0], c[1])["operation"] is None  # 同一道题一次只发一条。
    service.start_operation(b[0], b[1], first["id"])
    result = {"status": "posted", "answer_count": 4}
    assert not service.finish(b[0], b[1], first["id"], result)["duplicate"]
    assert service.finish(b[0], b[1], first["id"], result)["duplicate"]
    assert service.claim(b[0], b[1])["operation"] is None
    second = service.claim(c[0], c[1])["operation"]
    assert second["body"] != first["body"]
    service.start_operation(c[0], c[1], second["id"])
    service.finish(c[0], c[1], second["id"], {"status": "posted", "answer_count": 5})
    assert service.database.rows("SELECT state FROM tasks WHERE id=?", (task,))[0]["state"] == "complete"
    assert service.database.rows("SELECT answer_count FROM questions WHERE id=?", (first["question_id"],))[0]["answer_count"] == 5


@pytest.mark.parametrize("existing", [0, 1, 2, 4])
def test_question_quota_reuses_latest_old_questions(app, service, existing):
    a, sid, _ = enroll(service, "a")
    task = service.create_task("任务", forum_url(), 2, 1, [a])
    service.set_task_state(task, "running")
    old = seed(service, task, a, sid, existing, target=2)
    generate_all(app)
    questions = service.database.rows("SELECT * FROM questions ORDER BY position")
    assert len(questions) == 2
    assert sum(q["url"] is not None for q in questions) == min(2, existing)
    assert sum(n for kind, n in app.extensions["qa_worker"].generator.calls if kind == "questions") == max(0, 2-existing)
    if old:
        # 同步时顺序改变，已经登记的槽位不替换。
        service.sync(a, sid, task, {"course_name": "测试课程", "question_count": existing, "questions": list(reversed(old))})
        assert service.database.rows("SELECT id,url FROM questions ORDER BY position") == [
            {"id": q["id"], "url": q["url"]} for q in questions]


def test_online_clients_work_without_waiting_for_offline_member(app, service):
    a, b, offline = [enroll(service, name) for name in "abc"]
    task = service.create_task("滚动任务", forum_url(), 1, 1, [a[0], b[0], offline[0]])
    service.set_task_state(task, "running")
    seed(service, task, a[0], a[1], 1)
    seed(service, task, b[0], b[1], 1, answers=1)
    generate_all(app)
    operation = service.claim(b[0], b[1])["operation"]
    assert operation["kind"] == "answer"
    assert service.database.rows("SELECT synced_at FROM members WHERE account_id=?", (offline[0],))[0]["synced_at"] is None


def test_concurrent_claims_only_reserve_question_once(app, service):
    accounts = [enroll(service, name) for name in "abcd"]
    task = service.create_task("并发", forum_url(), 1, 1, [a[0] for a in accounts])
    service.set_task_state(task, "running")
    for i, (aid, sid, _) in enumerate(accounts):
        seed(service, task, aid, sid, 1, answers=0 if i == 0 else 1)
    generate_all(app)
    barrier = threading.Barrier(3)

    def claim(account):
        barrier.wait()
        return service.claim(account[0], account[1]).get("operation")

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(claim, accounts[1:]))
    assert sum(result is not None for result in results) == 1


def test_reservations_expire_but_submissions_survive_disconnect_and_server_restart(app, service):
    now = [1000.0]
    service.clock = lambda: now[0]
    a, b, c = [enroll(service, name) for name in "abc"]
    task = service.create_task("恢复", forum_url(), 1, 2, [a[0], b[0], c[0]])
    service.set_task_state(task, "running")
    for i, account in enumerate((a, b, c)):
        seed(service, task, account[0], account[1], 1, answers=0 if i == 0 else 2)
    generate_all(app)
    reserved = service.claim(b[0], b[1])["operation"]
    now[0] += 61
    sid_c = service.open_session(c[0], "device-c", "c")["session_id"]
    next_operation = service.claim(c[0], sid_c)["operation"]
    assert next_operation["question_id"] == reserved["question_id"]
    service.start_operation(c[0], sid_c, next_operation["id"])
    now[0] += 120
    restarted = Service(Database(service.database.path), clock=lambda: now[0])
    sid_b = restarted.open_session(b[0], "device-b", "b")["session_id"]
    assert restarted.claim(b[0], sid_b)["operation"] is None
    with pytest.raises(ApiError, match="原客户端"):
        restarted.open_session(c[0], "different-device", "c")
    sid_c = restarted.open_session(c[0], "device-c", "c")["session_id"]
    recovered = restarted.claim(c[0], sid_c)["operation"]
    assert recovered["recover"] and recovered["id"] == next_operation["id"]
    restarted.finish(c[0], sid_c, recovered["id"], {"status": "already_answered", "answer_count": 1})


def test_captcha_heartbeat_retains_account_session_and_submission(app, service):
    now = [1000.0]
    service.clock = lambda: now[0]
    a, sid, _ = enroll(service, "a")
    task = service.create_task("任务", forum_url(), 1, 1, [a])
    service.set_task_state(task, "running")
    seed(service, task, a, sid, 0)
    generate_all(app)
    operation = service.claim(a, sid)["operation"]
    service.start_operation(a, sid, operation["id"])
    for _ in range(20):
        now[0] += 30
        service.heartbeat(a, sid, "等待人工验证")
    assert service.claim(a, sid)["operation"]["id"] == operation["id"]
    with pytest.raises(ApiError, match="原客户端"):
        service.open_session(a, "second-computer", "a")


def test_pause_blocks_new_submission_but_allows_started_result(app, service):
    a, sid, _ = enroll(service, "a")
    task = service.create_task("暂停", forum_url(), 2, 1, [a])
    service.set_task_state(task, "running")
    seed(service, task, a, sid, 0, target=2)
    generate_all(app)
    operation = service.claim(a, sid)["operation"]
    service.set_task_state(task, "paused")
    with pytest.raises(ApiError, match="暂停"):
        service.start_operation(a, sid, operation["id"])
    service.finish(a, sid, operation["id"], {"status": "cancelled"})
    assert service.claim(a, sid)["operation"] is None
    service.set_task_state(task, "running")
    operation = service.claim(a, sid)["operation"]
    service.start_operation(a, sid, operation["id"])
    service.set_task_state(task, "paused")
    service.finish(a, sid, operation["id"], {
        "status": "posted", "text": operation["body"], "url": forum_url().replace("/home/123", "/questionDetail/123/999"),
        "answer_count": 0, "question_count": 1})
    assert service.database.rows("SELECT state FROM tasks WHERE id=?", (task,))[0]["state"] == "paused"


def test_unavailable_entry_never_counts_as_an_answer(app, service):
    a, b = [enroll(service, name) for name in "ab"]
    task = service.create_task("任务", forum_url(), 1, 1, [a[0], b[0]])
    service.set_task_state(task, "running")
    seed(service, task, a[0], a[1], 1)
    seed(service, task, b[0], b[1], 1, answers=1)
    generate_all(app)
    operation = service.claim(b[0], b[1])["operation"]
    service.finish(b[0], b[1], operation["id"], {"status": "unavailable", "answer_count": 0})
    assert service.claim(b[0], b[1])["operation"] is None
    assert service.database.rows("SELECT state FROM tasks WHERE id=?", (task,))[0]["state"] == "running"


def test_failed_generation_waits_for_admin_retry(app, service):
    a, sid, _ = enroll(service, "a")
    task = service.create_task("生成失败", forum_url(), 12, 1, [a])
    service.set_task_state(task, "running")
    seed(service, task, a, sid, 0, target=12)
    worker = app.extensions["qa_worker"]
    worker.generator.fail = True
    assert worker.step()
    assert not worker.step()
    assert "生成失败" in service.database.rows("SELECT error FROM tasks")[0]["error"]
    worker.generator.fail = False
    service.retry(task)
    generate_all(app)
    assert worker.generator.calls[-2:] == [("questions", 10), ("questions", 2)]


def test_multi_task_round_robin_and_cross_account_auth(app, service):
    a, sid, token = enroll(service, "a")
    b, sid_b, _ = enroll(service, "b")
    tasks = [service.create_task(f"任务{i}", forum_url(i), 1, 1, [a]) for i in (123, 456)]
    for tid, course in zip(tasks, (123, 456)):
        service.set_task_state(tid, "running")
        seed(service, tid, a, sid, 0, course=course)
    generate_all(app)
    operation = service.claim(a, sid)["operation"]
    service.finish(a, sid, operation["id"], {"status": "failed", "error": "测试失败"})
    second = service.claim(a, sid)["operation"]
    assert operation["task_id"] != second["task_id"]
    with pytest.raises(ApiError):
        service.operation_status(b, sid_b, second["id"])
    with pytest.raises(ApiError):
        service.sync(b, sid_b, tasks[0], {"course_name": "测试", "question_count": 0, "questions": []})
    data = service.database.path.read_bytes()
    assert token.encode() not in data
    new_token = service.rotate_token(a)
    with pytest.raises(ApiError):
        service.authenticate(token)
    assert service.authenticate(new_token) == a


def test_admin_auth_csrf_and_client_api_never_store_forum_credentials(app, service):
    client = app.test_client()
    assert client.get("/").status_code == 302
    login = client.get("/login")
    csrf = re.search(rb'name="csrf" value="([^"]+)"', login.data)[1].decode()
    assert client.post("/login", data={"password": "admin-password"}).status_code == 400
    assert client.post("/login", data={"password": "admin-password", "csrf": csrf}).status_code == 302
    assert client.get("/").status_code == 200
    aid, _, token = enroll(service, "client-account")
    headers = {"Authorization": "Bearer " + token}
    assert client.post("/api/v1/session", json={"username": "client-account", "client_id": "device-client-account",
                                                 "password": "DO-NOT-STORE"}, headers=headers).status_code == 400
    assert client.post("/api/v1/session", json={"username": "wrong", "client_id": "another"}, headers=headers).status_code == 403
    assert client.post("/api/v1/work/claim").status_code == 401
    assert b"DO-NOT-STORE" not in service.database.path.read_bytes()
    columns = service.database.rows("PRAGMA table_info(accounts)")
    assert not {"password", "cookies", "storage_state"} & {column["name"] for column in columns}


def test_overlapping_forum_tasks_do_not_publish_to_same_url_concurrently(app, service):
    accounts = [enroll(service, name) for name in "abc"]
    tasks = [service.create_task(f"同论坛任务{i}", forum_url(), 1, 1, [a[0] for a in accounts]) for i in range(2)]
    for task in tasks:
        service.set_task_state(task, "running")
        for index, (aid, sid, _) in enumerate(accounts):
            seed(service, task, aid, sid, 1, answers=0 if index == 0 else 1)
    generate_all(app)
    b, c = accounts[1:]
    operation = service.claim(b[0], b[1])["operation"]
    assert service.claim(c[0], c[1])["operation"] is None
    service.start_operation(b[0], b[1], operation["id"])
    service.finish(b[0], b[1], operation["id"], {"status": "posted", "answer_count": 1})
    assert all(row["state"] == "complete" for row in service.database.rows("SELECT state FROM tasks"))


def test_reported_quota_without_registered_urls_requests_sync_after_restart(app, service):
    a, sid, _ = enroll(service, "a")
    task = service.create_task("补录题池", forum_url(), 1, 1, [a])
    service.set_task_state(task, "running")
    seed(service, task, a, sid, 0)
    generate_all(app)
    operation = service.claim(a, sid)["operation"]
    service.finish(a, sid, operation["id"], {"status": "not_needed", "question_count": 1})
    # 模拟客户端在回报“无需补题”之后、上传实际题池之前退出。
    restarted = Service(Database(service.database.path))
    assert restarted.claim(a, sid)["sync_task"]["id"] == task
    seed(restarted, task, a, sid, 1, answers=1)
    assert restarted.database.rows("SELECT state FROM tasks")[0]["state"] == "complete"
