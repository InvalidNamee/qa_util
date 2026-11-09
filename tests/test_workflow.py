import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from qa_util.models import Account
from qa_util.storage import private_json, read_json
from qa_util.workflow import ClientRunner
from client_fakes import FastControl, Website, flask_transport
from server_support import enroll, forum_url, seed


async def until(predicate, running=(), timeout=8):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if any(task.done() for task in running):
            pytest.fail("客户端提前退出：" + str([task.result() for task in running if task.done()]))
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("任务未在时限内完成")
        await asyncio.sleep(0.02)


def client_config(config, username, token):
    return replace(config, access_token=token, account=Account(username, "password-" + username),
                   data_directory=config.data_directory.parent / ("client-" + username))


async def test_two_clients_roll_forward_without_preview_and_without_credentials_upload(app, service, config):
    accounts = [service.create_account(name) for name in "ab"]
    task_id = service.create_task("完整任务", forum_url(), 2, 1, [aid for aid, _ in accounts])
    service.set_task_state(task_id, "running")
    site = Website()
    site.add(forum_url(), "a", "旧题究竟是什么？")
    captured = []

    def inspect(request):
        captured.append(request.content.decode())
        return None

    runners = [ClientRunner(client_config(config, name, token), control=FastControl(),
                            browser_factory=site.factory, transport=flask_transport(app, inspect))
               for name, (aid, token) in zip("ab", accounts)]
    app.extensions["qa_worker"].start()
    running = [asyncio.create_task(r.run()) for r in runners]
    try:
        await until(lambda: service.database.rows("SELECT state FROM tasks")[0]["state"] == "complete", running)
        assert len([p for p in site.published if p[0] == "question"]) == 3
        assert len([p for p in site.published if p[0] == "answer"]) == 4
        assert all(row["count"] >= 1 for row in site.questions)
        assert all(not (row["owner"] in row["authors"]) for row in site.questions)
        assert all("password-a" not in body and "password-b" not in body for body in captured)
        assert all("password-a" not in (r.directory / "debug.log").read_text() for r in runners)
    finally:
        for r in runners:r.control.stop.set()
        await asyncio.gather(*running)


async def test_personal_goals_complete_even_when_questions_already_meet_threshold(app, service, config):
    accounts = [service.create_account(name) for name in "abc"]
    task_id = service.create_task("已有外部回答", forum_url(), 1, 1, [aid for aid, _ in accounts])
    service.set_task_state(task_id, "running")
    site = Website()
    for name in "abc":
        site.add(forum_url(), name, f"{name}的问题是什么？", answers=1)
    runners = [ClientRunner(client_config(config, name, token), control=FastControl(),
                            browser_factory=site.factory, transport=flask_transport(app))
               for name, (_, token) in zip("abc", accounts)]
    app.extensions["qa_worker"].start()
    running = [asyncio.create_task(runner.run()) for runner in runners]
    try:
        await until(lambda: service.database.rows("SELECT state FROM tasks")[0]["state"] == "complete", running)
        assert all(row["answer_count"] >= 1 for row in service.database.rows("SELECT answer_count FROM members"))
        assert len(site.published) == 3 and all(item[0] == "answer" for item in site.published)
        assert all(runner.view["answer_total"] == 1 for runner in runners)
    finally:
        for runner in runners:
            runner.control.stop.set()
        await asyncio.gather(*running)


async def test_restart_discards_operation_removed_with_another_account(app, service, config):
    a, b = [enroll(service, name) for name in "ab"]
    task_id = service.create_task("删除后的恢复", forum_url(), 1, 1, [a[0], b[0]])
    service.set_task_state(task_id, "running")
    for aid, sid, _ in (a, b):
        seed(service, task_id, aid, sid, 1)
    while app.extensions["qa_worker"].step():
        pass
    operation = service.claim(b[0], b[1])["operation"]
    cfg = client_config(config, "b", b[2])
    private_json(cfg.data_directory / "identity.json", {"client_id": "device-b"})
    private_json(cfg.data_directory / "operation.json", {"operation": operation, "stage": "prepared"})
    service.delete_account(a[0])
    site = Website()
    runner = ClientRunner(cfg, control=FastControl(), browser_factory=site.factory, transport=flask_transport(app))
    running = asyncio.create_task(runner.run())
    try:
        await until(lambda: not runner.journal_path.exists() and runner.view["phase"] == "等待任务", [running])
        assert not site.published
    finally:
        runner.control.stop.set()
        assert await running == "stopped"


async def test_account_deleted_after_lost_result_ack_does_not_block_other_client(app, service, config):
    a, b = [enroll(service, name) for name in "ab"]
    task_id = service.create_task("删除与回报并发", forum_url(), 1, 1, [a[0], b[0]])
    service.set_task_state(task_id, "running")
    qa = seed(service, task_id, a[0], a[1], 1)[0]
    qb = seed(service, task_id, b[0], b[1], 1, answers=1, my_answers=0)[0]
    while app.extensions["qa_worker"].step():
        pass
    site = Website()
    site.add(forum_url(), "a", qa["text"])["url"] = qa["url"]
    site.add(forum_url(), "b", qb["text"], answers=1)["url"] = qb["url"]
    cfg = client_config(config, "b", b[2])
    private_json(cfg.data_directory / "identity.json", {"client_id": "device-b"})
    original, lost = flask_transport(app), [False]

    def handler(request):
        response = original.handle_request(request)
        if request.url.path.endswith("/result") and not lost[0]:
            service.delete_account(a[0])
            lost[0] = True
            raise httpx.ConnectError("回报收妥后，关联账号被删除且响应丢失")
        return response

    runner = ClientRunner(cfg, control=FastControl(), browser_factory=site.factory, transport=httpx.MockTransport(handler))
    running = asyncio.create_task(runner.run())
    try:
        await until(lambda: lost[0] and not runner.journal_path.exists() and runner.view["phase"] == "等待任务", [running])
        assert len(site.published) == 1
    finally:
        runner.control.stop.set()
        assert await running == "stopped"


async def test_publish_success_then_client_restart_reconciles_without_duplicate(app, service, config):
    a, token_a = service.create_account("a")
    b, token_b = service.create_account("b")
    task_id = service.create_task("恢复测试", forum_url(), 1, 1, [a, b])
    service.set_task_state(task_id, "running")
    site = Website()
    site.add(forum_url(), "b", "乙的问题究竟是什么？", answers=1)
    site.add(forum_url(), "a", "甲的问题究竟是什么？")
    site.historical_answers[(forum_url(), "a")] = 1
    app.extensions["qa_worker"].start()
    cfg_a = client_config(config, "a", token_a)
    # a只负责同步题池；关闭后 b 仍能补答其问题。
    runner_a = ClientRunner(cfg_a, control=FastControl(), browser_factory=site.factory, transport=flask_transport(app))
    running_a = asyncio.create_task(runner_a.run())
    await until(lambda: bool(service.database.rows("SELECT synced_at FROM members WHERE account_id=?", (a,))[0]["synced_at"]), [running_a])
    runner_a.control.stop.set()
    await running_a
    site.fail_after_answer = True
    cfg_b = client_config(config, "b", token_b)
    first = ClientRunner(cfg_b, control=FastControl(), browser_factory=site.factory, transport=flask_transport(app))
    assert await first.run() == "failed"
    assert len([p for p in site.published if p[0] == "answer"]) == 1
    assert read_json(first.journal_path)["result"]["status"] == "uncertain"
    second = ClientRunner(cfg_b, control=FastControl(), browser_factory=site.factory, transport=flask_transport(app))
    resumed = asyncio.create_task(second.run())
    try:
        await until(lambda: service.database.rows("SELECT state FROM tasks")[0]["state"] == "complete", [resumed])
        assert len([p for p in site.published if p[0] == "answer"]) == 1
        assert not second.journal_path.exists()
        assert service.database.rows("SELECT status FROM answer_attempts")[0]["status"] == "already_answered"
    finally:
        second.control.stop.set()
        await resumed


async def test_lost_result_ack_repeats_report_not_browser_submission(app, service, config):
    aid, token = service.create_account("a")
    task_id = service.create_task("回报丢失", forum_url(), 1, 1, [aid])
    service.set_task_state(task_id, "running")
    site = Website()
    app.extensions["qa_worker"].start()
    lost = [False]
    original = flask_transport(app)

    def handler(request):
        response = original.handle_request(request)
        if request.url.path.endswith("/result") and not lost[0]:
            lost[0] = True
            raise httpx.ConnectError("模拟服务端已收妥而响应丢失")
        return response

    runner = ClientRunner(client_config(config, "a", token), control=FastControl(), browser_factory=site.factory,
                          transport=httpx.MockTransport(handler))
    task = asyncio.create_task(runner.run())
    try:
        await until(lambda: len(service.database.rows("SELECT * FROM operations WHERE result IS NOT NULL")) == 1
                    and not runner.journal_path.exists(), [task])
        assert lost[0] and len(site.published) == 1
        assert service.database.rows("SELECT question_count FROM members")[0]["question_count"] == 1
    finally:
        runner.control.stop.set()
        await task


async def test_offline_client_preserves_browser_until_stop(app, service, config):
    aid, token = service.create_account("a")
    requests = [0]
    events = []
    original = flask_transport(app)

    def handler(request):
        requests[0] += 1
        if requests[0] > 1:
            raise httpx.ConnectError("离线")
        return original.handle_request(request)

    runner = ClientRunner(client_config(config, "a", token), control=FastControl(), progress=events.append,
                          browser_factory=Website().factory, transport=httpx.MockTransport(handler))
    task = asyncio.create_task(runner.run())
    await until(lambda: any(event["detail"] == "等待连接服务端" for event in events), [task])
    assert not task.done()
    runner.control.stop.set()
    assert await asyncio.wait_for(task, timeout=3) == "stopped"


async def test_client_rechecks_external_answer_threshold_before_sending(app, service, config):
    a, b = [enroll(service, name) for name in "ab"]
    task_id = service.create_task("外部新增回答", forum_url(), 1, 5, [a[0], b[0]])
    service.set_task_state(task_id, "running")
    qa = seed(service, task_id, a[0], a[1], 1, answers=3)[0]
    seed(service, task_id, b[0], b[1], 1, answers=5)
    while app.extensions["qa_worker"].step():
        pass
    site = Website()
    row = site.add(forum_url(), "a", qa["text"], answers=5)
    site.historical_answers[(forum_url(), "b")] = 1
    row["url"] = qa["url"]
    cfg = client_config(config, "b", b[2])
    private_json(cfg.data_directory / "identity.json", {"client_id": "device-b"})
    runner = ClientRunner(cfg, control=FastControl(), browser_factory=site.factory, transport=flask_transport(app))
    running = asyncio.create_task(runner.run())
    try:
        await until(lambda: service.database.rows("SELECT state FROM tasks")[0]["state"] == "complete", [running])
        assert not site.published
        result = json.loads(service.database.rows("SELECT result FROM operations")[0]["result"])
        assert result["status"] == "threshold_met" and result["answer_count"] == 5
    finally:
        runner.control.stop.set()
        await running


async def test_captcha_keeps_heartbeats_and_other_client_running(app, service, config):
    accounts = [service.create_account(name) for name in "ab"]
    task_id = service.create_task("验证码", forum_url(), 1, 1, [aid for aid, _ in accounts])
    service.set_task_state(task_id, "running")
    site = Website()
    site.captcha_user = "a"
    runners = [ClientRunner(client_config(config, name, token), control=FastControl(), browser_factory=site.factory,
                            transport=flask_transport(app)) for name, (_, token) in zip("ab", accounts)]
    app.extensions["qa_worker"].start()
    running = [asyncio.create_task(r.run()) for r in runners]
    try:
        await until(lambda: service.database.rows("SELECT client_status FROM accounts WHERE username='a'")[0]["client_status"]
                    == "等待人工验证" and any(p[0] == "question" and p[1] == "b" for p in site.published), running)
        assert service.database.rows("SELECT state FROM operations WHERE account_id=?", (accounts[0][0],))[0]["state"] == "submitting"
        site.captcha_release.set()
        await until(lambda: service.database.rows("SELECT state FROM tasks")[0]["state"] == "complete", running)
    finally:
        for runner in runners:runner.control.stop.set()
        await asyncio.gather(*running)
