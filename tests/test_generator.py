import json
from dataclasses import replace

import httpx
import pytest

from qa_util.control import Control
from qa_util.generator import Generator
from qa_util.models import Question


async def test_batches_use_real_old_titles_and_aliases_without_credentials(config):
    config = replace(config, accounts=config.accounts[:2])
    questions = [Question("a", "旧问题？" if i == 0 else "", key=f"q{i}") for i in range(12)]
    requests = []

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.headers["Authorization"] == "Bearer " + config.llm.api_key
        prompt = body["messages"][1]["content"]
        assert config.llm.api_key not in prompt
        assert all(a.password not in prompt for a in config.accounts)
        payload = json.loads(prompt)
        rows = [dict(key=q["key"], text=q["text"] or f"课程新题{q['key']}？",
                     answers={alias: f"对应{q['key']}的回答" for alias in q["respondents"]})
                for q in reversed(payload["questions"])]
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"questions": rows}, ensure_ascii=False)}}]})

    result = await Generator(config, Control(), lambda _: None,
                             transport=httpx.MockTransport(handler)).generate("测试课程", questions)
    assert len(requests) == 2
    assert len(json.loads(requests[0]["messages"][1]["content"])["questions"]) == 10
    assert result[0].text == "旧问题？"
    assert all(q.answers == {"b": f"对应{q.key}的回答"} for q in result)


@pytest.mark.parametrize("invalid", ["count", "keys", "answers", "duplicate", "changed_old", "truncated"])
async def test_malformed_generation_stops(config, invalid):
    config = replace(config, accounts=config.accounts[:2])
    questions = [Question("a", "旧问题？", key="q0"), Question("b", "", key="q1")]

    async def handler(request):
        rows = [dict(key="q0", text="旧问题？", answers={"a1": "正确回答"}),
                dict(key="q1", text="新问题？", answers={"a0": "正确回答"})]
        if invalid == "count":
            rows.pop()
        elif invalid == "keys":
            rows[1]["key"] = "q0"
        elif invalid == "answers":
            rows[1]["answers"] = {"a1": "自己回答自己"}
        elif invalid == "duplicate":
            rows[1]["text"] = "旧问题？"
        elif invalid == "changed_old":
            rows[0]["text"] = "改写旧问题？"
        return httpx.Response(200, json={"choices": [{
            "finish_reason": "length" if invalid == "truncated" else "stop",
            "message": {"content": json.dumps({"questions": rows})},
        }]})

    with pytest.raises(ValueError):
        await Generator(config, Control(), lambda _: None,
                        transport=httpx.MockTransport(handler)).generate("课程", questions)
