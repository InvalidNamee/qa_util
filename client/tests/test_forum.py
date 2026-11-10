import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from qa_util import forum
from qa_util.control import Control
from qa_util.models import ManualVerification
from qa_util.storage import private_json


@pytest.mark.parametrize("outcome", ["posted", "captcha", "gateway"])
async def test_http_answer_submits_once_and_verifies_result(config, monkeypatch, outcome):
    key = b"0123456789abcdef"
    requests = []
    gates = []
    answered = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            nonlocal answered
            fields = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            decryptor = Cipher(algorithms.AES(key), modes.CBC(forum.IV)).decryptor()
            body = decryptor.update(base64.b64decode(fields["secretStr"][0])) + decryptor.finalize()
            unpadder = padding.PKCS7(128).unpadder()
            params = json.loads(unpadder.update(body) + unpadder.finalize())
            requests.append((self.path, params, self.headers["Authorization"], len(gates)))
            status = 200
            if self.path == "/getQuestionInfo":
                payload = {"status": "200", "rt": {"isMyQuestion": False, "questionInfo": {
                    "courseId": 123, "questionId": 42, "content": "问题？",
                    "answerNum": int(answered), "isAnswer": answered,
                }}}
            elif outcome == "captcha":
                payload = {"status": "400", "code": 1010}
            elif outcome == "gateway":
                status, payload = 504, {}
            else:
                answered = True
                payload = {"status": "200", "rt": {"answerId": "81"}}
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

    private_json(config.data_directory / "login.json", {"username": "a", "state": {"cookies": [
        {"name": "jt-cas", "value": "fixture-auth", "domain": ".zhihuishu.com", "path": "/", "expires": -1},
        {"name": "CASLOGC", "value": "{}", "domain": ".zhihuishu.com", "path": "/", "expires": -1},
    ]}})
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    monkeypatch.setattr(forum, "QA_API", f"http://127.0.0.1:{server.server_port}/")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def before_send():
        gates.append(True)

    try:
        async with forum.ForumSession(config, Control(), lambda _: None, before_send=before_send) as session:
            session.key = key
            task = {"forum_url": "https://qah5.zhihuishu.com/qa.html#/web/home/123?recruitId=456&role=2"}
            course = await session.open(task)
            url = course.question_url(42)
            if outcome == "posted":
                assert (await course.answer(url, "回答")).status == "posted"
                assert (await course.answer(url, "回答")).status == "already_answered"
            elif outcome == "captcha":
                with pytest.raises(ManualVerification, match="1010") as error:
                    await course.answer(url, "回答")
                assert error.value.rejected
            else:
                with pytest.raises(RuntimeError, match="504"):
                    await course.answer(url, "回答")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    posts = [(params, auth, gate) for path, params, auth, gate in requests if path == "/saveAnswer"]
    assert posts == [({
        "courseId": "123", "recruitId": "456", "annexs": "[]", "qid": "42",
        "source": "2", "aContent": "回答", "saveSource": 1,
    }, "fixture-auth", 1)]
