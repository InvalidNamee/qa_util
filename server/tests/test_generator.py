import json

import httpx
import pytest

from qa_util_server.generator import ContentGenerator, SYSTEM_PROMPT


def transport(mutate=None):
    def handler(request):
        body = json.loads(request.content)
        assert body["messages"][0]["content"] == SYSTEM_PROMPT
        data = json.loads(body["messages"][1]["content"])
        rows = [{"key": q["key"], "text": q["text"] or f"第{q['key']}个概念究竟是什么？",
                 "answers": {who: "可以探测可用带宽，这是原样返回的回答。" for who in q["respondents"]}}
                for q in data["questions"]]
        if mutate:
            mutate(rows)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"questions": rows}, ensure_ascii=False)}}]})
    return httpx.MockTransport(handler)


def test_server_generates_only_requested_question_and_answer_deficits(llm):
    generator = ContentGenerator(llm, transport=transport())
    questions = generator.questions("计算机网络", [], 3)
    assert len(questions) == 3 and all(q.endswith("？") for q in questions)
    answers = generator.answers("计算机网络", "什么是可用带宽？", 2)
    assert answers == ["可以探测可用带宽，这是原样返回的回答。"] * 2
    # 原有措辞限制仅在 prompt 中；不检查、不拦截正文中的词。
    assert "疑问语气鲜明" in SYSTEM_PROMPT and "不得为了肯定语气捏造事实" in SYSTEM_PROMPT


@pytest.mark.parametrize("mutation", [lambda rows: rows.pop(), lambda rows: rows[0].update(key="wrong"),
                                        lambda rows: rows[0].update(text="没有问号"),
                                        lambda rows: rows[0].update(answers={"wrong": "回答"})])
def test_bad_generation_structure_stops_job(llm, mutation):
    generator = ContentGenerator(llm, transport=transport(mutation))
    with pytest.raises(ValueError):
        generator.questions("课程", [], 1)


def test_generation_request_contains_no_account_or_credentials(llm):
    def handler(request):
        raw = request.content.decode()
        assert llm.api_key not in raw and "password" not in raw and "username" not in raw
        assert request.headers["Authorization"] == "Bearer " + llm.api_key
        return transport().handle_request(request)
    generator = ContentGenerator(llm, transport=httpx.MockTransport(handler))
    assert len(generator.answers("课程", "课程究竟是什么？", 3)) == 3
