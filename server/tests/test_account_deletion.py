import threading

import pytest

from qa_util_server.service import ApiError
from server_support import enroll, forum_url, seed


def prepare(app, service):
    a, b = [enroll(service, name) for name in "ab"]
    task = service.create_task("删除测试", forum_url(), 1, 1, [a[0], b[0]])
    service.set_task_state(task, "running")
    for aid, sid, _ in (a, b):
        seed(service, task, aid, sid, 1)
    for _ in range(10):
        if not app.extensions["qa_worker"].step():
            break
    return a, b, task


def test_delete_account_revokes_token_and_allows_registering_same_username(service):
    aid, token = service.create_account("same-account", "旧别名")
    service.delete_account(aid)
    with pytest.raises(ApiError):
        service.authenticate(token)
    new_id, new_token = service.create_account("same-account", "新别名")
    assert service.authenticate(new_token) == new_id
    assert service.dashboard()["accounts"][0]["alias"] == "新别名"


def test_delete_cleans_related_records_releases_drafts_and_keeps_other_accounts(app, service):
    a, b, task = prepare(app, service)
    other = service.create_task("无关任务", forum_url(456), 1, 1, [b[0]])
    service.set_task_state(other, "running")
    own = service.claim(a[0], a[1])["operation"]
    peer = service.claim(b[0], b[1])["operation"]
    assert own["question"]["owner_id"] == b[0] and peer["question"]["owner_id"] == a[0]
    service.delete_account(a[0])
    assert service.authenticate(b[2]) == b[0]
    assert service.database.rows("SELECT state FROM tasks WHERE id=?", (task,)) == [{"state": "paused"}]
    assert service.database.rows("SELECT state FROM tasks WHERE id=?", (other,)) == [{"state": "running"}]
    assert service.database.rows("SELECT owner_id FROM questions") == [{"owner_id": b[0]}]
    assert service.database.rows("SELECT account_id FROM members WHERE task_id=?", (task,)) == [{"account_id": b[0]}]
    assert not service.database.rows("SELECT * FROM operations WHERE task_id=?", (task,))
    assert service.database.rows("SELECT state FROM answer_drafts WHERE id=?", (own["draft_id"],)) == [{"state": "ready"}]
    with pytest.raises(ApiError) as error:
        service.start_operation(b[0], b[1], peer["id"])
    assert error.value.code == "operation_inactive"
    assert service.finish(b[0], b[1], peer["id"], {"status": "cancelled"})["accepted"]
    assert not service.database.rows("PRAGMA foreign_key_check")


@pytest.mark.parametrize("actor", [0, 1])
def test_delete_waits_for_own_and_other_accounts_submissions_to_reconcile(app, service, actor):
    a, b, task = prepare(app, service)
    account = (a, b)[actor]
    operation = service.claim(account[0], account[1])["operation"]
    service.start_operation(account[0], account[1], operation["id"])
    with pytest.raises(ValueError, match="核对"):
        service.delete_account(a[0])
    assert service.authenticate(a[2]) == a[0]
    assert service.database.rows("SELECT state FROM tasks WHERE id=?", (task,)) == [{"state": "running"}]
    service.finish(account[0], account[1], operation["id"], {"status": "posted", "answer_count": 1, "my_answer_count": 2})
    service.delete_account(a[0])
    assert not service.database.rows("SELECT * FROM accounts WHERE id=?", (a[0],))


@pytest.mark.parametrize("fail", [False, True])
def test_delete_during_generation_discards_late_content_and_error(app, service, fail):
    a, b = [enroll(service, name) for name in "ab"]
    task = service.create_task("取消生成", forum_url(), 1, 1, [a[0], b[0]])
    service.set_task_state(task, "running")
    seed(service, task, a[0], a[1], 1)
    seed(service, task, b[0], b[1], 1, answers=1)
    entered, release = threading.Event(), threading.Event()

    def delayed(*_args):
        entered.set()
        assert release.wait(timeout=5)
        if fail:
            raise RuntimeError("被删除任务的迟到异常")
        return ["被删除任务的迟到回答"]

    worker = app.extensions["qa_worker"]
    worker.generator.answers = delayed
    thread = threading.Thread(target=worker.step)
    thread.start()
    try:
        assert entered.wait(timeout=3)
        service.delete_account(a[0])
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert service.database.rows("SELECT state,error FROM tasks WHERE id=?", (task,)) == [{"state": "paused", "error": ""}]
    assert not service.database.rows("SELECT * FROM answer_drafts")
    assert service.database.rows("SELECT state FROM generation_jobs") == [{"state": "cancelled"}]


def test_delete_last_participant_keeps_task_paused_and_blocks_restart(service):
    aid, _, _ = enroll(service, "a")
    task = service.create_task("空任务", forum_url(), 1, 1, [aid])
    service.set_task_state(task, "running")
    service.delete_account(aid)
    with pytest.raises(ValueError, match="已无参与账号"):
        service.set_task_state(task, "running")


def test_delete_route_requires_admin_and_csrf(app, service):
    aid, _, token = enroll(service, "a")
    with app.test_client() as client:
        assert client.get(f"/accounts/{aid}/delete").status_code != 200
        with client.session_transaction() as session:
            session.update(csrf="test-csrf")
        assert client.post(f"/accounts/{aid}/delete", data={"csrf": "test-csrf"}).status_code == 302
        assert service.authenticate(token) == aid
        with client.session_transaction() as session:
            session["admin"] = True
        assert client.post(f"/accounts/{aid}/delete").status_code == 400
        assert service.authenticate(token) == aid
        assert client.post(f"/accounts/{aid}/delete", data={"csrf": "test-csrf"}).status_code == 302
        assert client.post(f"/accounts/{aid}/delete", data={"csrf": "test-csrf"}).status_code == 404
