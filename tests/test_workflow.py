import asyncio
import json
from dataclasses import replace

import pytest

from comment_util.control import Control
from comment_util.workflow import Workflow
from conftest import FakeGenerator, FakePool


async def accept(_):
    return True


def make_workflow(config, tmp_path, pool, *, confirm=accept, generator=None, control=None):
    return Workflow(config, confirm, lambda _: None, output_root=tmp_path,
                    browser_factory=lambda *_: pool, generator=generator or FakeGenerator(), control=control)


async def test_six_accounts_two_workers_all_targets_and_existing_answers(config, tmp_path):
    pool = FakePool(config)
    for user, count in zip("abcdef", (0, 1, 2, 3, 0, 2)):
        pool.seed(user, count)
    pool.no_button.add(("a", "https://fixture/c/0"))
    workflow = make_workflow(config, tmp_path, pool)
    result = await asyncio.wait_for(workflow.run(), timeout=3)
    assert result.status == "complete"
    assert pool.maximum == 2
    assert pool.active == 0 and pool.closed
    assert pool.released == 18
    assert len(pool.posts) == 5
    assert len(pool.answers) == 60
    assert all(r.covered == 10 for r in result.accounts)
    assert result.accounts[0].skipped == 1
    final = json.loads((workflow.directory / "questions.json").read_text())
    assert len(final) == 12
    for user in "abcdef":
        expected = [q["url"] for q in final if q["owner"] != user]
        actual = [url for owner, url, _ in pool.answers if owner == user]
        assert actual == expected
    for user, url, answer in pool.answers:
        q = next(q for q in final if q["url"] == url)
        assert q["owner"] != user
        assert q["answers"][user] == answer
    assert all("/d/2" not in q["url"] for q in final)


async def test_declining_preview_has_no_publications(config, tmp_path):
    pool = FakePool(config)

    async def decline(preview):
        assert "待发布提问" in preview
        assert not pool.active
        return False

    workflow = make_workflow(config, tmp_path, pool, confirm=decline)
    result = await workflow.run()
    assert result.status == "cancelled"
    assert not pool.posts and not pool.answers
    assert (workflow.directory / "preview.txt").exists()
    assert not (workflow.directory / "questions.json").exists()


@pytest.mark.parametrize("phase", ["preflight", "question"])
async def test_first_phase_failure_blocks_answer_phase(config, tmp_path, phase):
    pool = FakePool(config)
    getattr(pool, "fail_" + phase).add("b")
    result = await make_workflow(config, tmp_path, pool).run()
    assert result.status == "failed"
    assert not pool.answers and pool.active == 0
    assert result.accounts[1].error


async def test_answer_failure_does_not_stop_other_accounts(config, tmp_path):
    pool = FakePool(config)
    pool.fail_answer.add("b")
    workflow = make_workflow(config, tmp_path, pool)
    result = await workflow.run()
    assert result.status == "partial"
    assert result.accounts[1].error
    assert all(r.covered == 10 for r in result.accounts if r.username != "b")
    debug = (workflow.directory / "debug.log").read_text()
    assert "[b] 账号失败，阶段=互答" in debug
    assert "回答目标：a0q0 https://fixture/a/new0" in debug
    assert "Traceback" in debug and "conftest.py" in debug and "RuntimeError: 回答失败" in debug
    assert "Traceback" not in (workflow.directory / "run.log").read_text()


async def test_invalid_generated_data_cannot_reach_confirmation(config, tmp_path):
    class Invalid(FakeGenerator):
        async def generate(self, course, questions):
            return (await super().generate(course, questions))[:-1]

    async def never(_):
        pytest.fail("无效生成结果不能进入确认")

    pool = FakePool(config)
    result = await make_workflow(config, tmp_path, pool, confirm=never, generator=Invalid()).run()
    assert result.status == "failed" and not pool.posts


async def test_stop_during_confirmation_releases_all_contexts(config, tmp_path):
    control = Control()
    pool = FakePool(config)

    async def waiting(_):
        control.stop.set()
        await asyncio.Event().wait()

    result = await make_workflow(config, tmp_path, pool, confirm=waiting, control=control).run()
    assert result.status == "stopped"
    assert pool.closed and pool.active == 0 and not pool.posts


async def test_single_account_no_answers(config, tmp_path):
    config = replace(config, accounts=config.accounts[:1])
    pool = FakePool(config)
    result = await make_workflow(config, tmp_path, pool).run()
    assert result.status == "complete"
    assert not pool.answers


async def test_credentials_are_redacted_in_logs_and_result(config, tmp_path):
    class BadPool(FakePool):
        async def __aenter__(self):
            raise RuntimeError(config.accounts[0].password + " " + config.llm.api_key)

    workflow = make_workflow(config, tmp_path, BadPool(config))
    await workflow.run()
    text = (workflow.directory / "run.log").read_text()
    assert config.accounts[0].password not in text and config.llm.api_key not in text
    debug = (workflow.directory / "debug.log").read_text()
    assert "Traceback" in debug and "任务失败，阶段=初始化" in debug
    assert config.accounts[0].password not in debug and config.llm.api_key not in debug
