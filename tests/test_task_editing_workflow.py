import asyncio

from qa_util.workflow import ClientRunner
from client_fakes import FastControl, Website, flask_transport
from server_support import forum_url
from test_workflow import client_config, until


async def test_existing_clients_continue_after_completed_task_is_edited(app, service, config):
    accounts = [service.create_account(name) for name in "ab"]
    members = [aid for aid, _ in accounts]
    task = service.create_task("原目标", forum_url(), 1, 1, members)
    service.set_task_state(task, "running")
    site = Website()
    runners = [ClientRunner(client_config(config, name, token), control=FastControl(),
                            forum_factory=site.factory, transport=flask_transport(app))
               for name, (_, token) in zip("ab", accounts)]
    app.extensions["qa_worker"].start()
    running = [asyncio.create_task(runner.run()) for runner in runners]
    try:
        await until(lambda: service.database.rows("SELECT state FROM tasks")[0]["state"] == "complete", running)
        assert len(site.published) == 4
        old_urls = {row["url"] for row in site.questions}
        service.update_task(task, "追加目标", forum_url(), 2, 1, members)
        assert service.database.rows("SELECT state FROM tasks")[0]["state"] == "paused"
        service.set_task_state(task, "running")
        await until(lambda: service.database.rows("SELECT state FROM tasks")[0]["state"] == "complete", running)
        assert len([item for item in site.published if item[0] == "question"]) == 4
        assert len([item for item in site.published if item[0] == "answer"]) == 4
        assert old_urls <= {row["url"] for row in site.questions}
        assert all(len(row["authors"]) == 1 for row in site.questions)
    finally:
        for runner in runners:
            runner.control.stop.set()
        await asyncio.gather(*running)
