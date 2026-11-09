import argparse
import atexit
import hmac
import logging
import secrets
import sqlite3
from logging.handlers import RotatingFileHandler

from flask import Flask, abort, g, redirect, render_template, request, session, url_for

from .config import load_server_settings
from .generator import ContentGenerator
from .content import GenerationWorker
from .database import Database
from .service import ApiError, Service


def create_app(settings=None, *, generator=None, start_worker=True):
    settings = settings or load_server_settings()
    settings.data_directory.mkdir(parents=True, exist_ok=True)
    app = Flask(__name__)
    app.config.update(SECRET_KEY=settings.secret_key, MAX_CONTENT_LENGTH=2 * 1024 * 1024,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
    database = Database(settings.data_directory / "qa.sqlite3")
    service = Service(database, interval=settings.publish_interval_seconds)
    app.extensions["qa_service"] = service
    handler = RotatingFileHandler(settings.data_directory / "server.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger(f"qa-server-{id(app)}")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False
    worker = GenerationWorker(service, generator or ContentGenerator(settings.llm, settings.generation_prompt), logger)
    app.extensions["qa_worker"] = worker
    app.extensions["qa_logger"] = logger
    if start_worker:
        worker.start()
        atexit.register(worker.close)

    @app.errorhandler(ApiError)
    def api_error(error):
        if request.path.startswith("/api/"):
            return {"error": str(error), "code": error.code}, error.status
        return render_template("error.html", message=str(error)), error.status

    @app.errorhandler(ValueError)
    @app.errorhandler(sqlite3.IntegrityError)
    def validation_error(error):
        message = "账号或记录重复，请检查输入" if isinstance(error, sqlite3.IntegrityError) else str(error)
        if request.path.startswith("/api/"):
            return {"error": message, "code": "invalid"}, 400
        return render_template("error.html", message=message), 400

    @app.errorhandler(500)
    def unexpected_error(error):
        logger.error("服务端异常：%s", error, exc_info=error.original_exception)
        if request.path.startswith("/api/"):
            return {"error": "服务端执行失败，详情见 server.log", "code": "server_error"}, 500
        return render_template("error.html", message="服务端执行失败，详情见 server.log"), 500

    def csrf():
        return session.setdefault("csrf", secrets.token_urlsafe(24))

    app.jinja_env.globals["csrf"] = csrf

    @app.before_request
    def protect():
        if request.path.startswith("/api/"):
            authorization = request.headers.get("Authorization", "")
            token = authorization[7:] if authorization.startswith("Bearer ") else ""
            g.account_id = service.authenticate(token)
            return None
        if request.method == "POST" and not hmac.compare_digest(request.form.get("csrf", "").encode(),
                                                                 session.get("csrf", "missing").encode()):
            abort(400)
        if request.endpoint not in {"login", "static"} and not session.get("admin"):
            return redirect(url_for("login"))

    def payload():
        body = request.get_json()
        if not isinstance(body, dict) or {"password", "cookies", "storage_state"} & body.keys():
            raise ValueError("请求结构无效；不要上传论坛凭据")
        return body

    def sid():
        return request.headers.get("X-Session-ID", "")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = ""
        if request.method == "POST":
            if hmac.compare_digest(request.form.get("password", "").encode(), settings.admin_password.encode()):
                session.clear()
                session["admin"] = True
                return redirect(url_for("index"))
            error = "管理密码不正确"
        return render_template("login.html", error=error)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def index():
        return render_template("index.html", **service.dashboard())

    @app.post("/accounts")
    def create_account():
        _, token = service.create_account(request.form.get("username", ""), request.form.get("alias", ""))
        return render_template("index.html", **service.dashboard(), issued_token=token,
                               issued_username=request.form["username"])

    @app.post("/accounts/<int:account_id>/alias")
    def set_alias(account_id):
        service.set_alias(account_id, request.form.get("alias", ""))
        return redirect(url_for("index"))

    @app.post("/accounts/<int:account_id>/delete")
    def delete_account(account_id):
        service.delete_account(account_id)
        return redirect(url_for("index"))

    @app.post("/accounts/<int:account_id>/token")
    def rotate_token(account_id):
        token = service.rotate_token(account_id)
        name = database.rows("SELECT username FROM accounts WHERE id=?", (account_id,))[0]["username"]
        return render_template("index.html", **service.dashboard(), issued_token=token, issued_username=name)

    @app.post("/tasks")
    def create_task():
        service.create_task(request.form.get("name", ""), request.form.get("forum_url", ""),
                            int(request.form.get("question_target", "0")), int(request.form.get("answer_target", "0")),
                            [int(value) for value in request.form.getlist("accounts")],
                            account_answer_target=int(request.form.get("account_answer_target", "0")))
        return redirect(url_for("index"))

    @app.post("/tasks/<int:task_id>/<action>")
    def task_action(task_id, action):
        if action == "retry":
            service.retry(task_id)
        elif action in {"start", "pause"}:
            service.set_task_state(task_id, "running" if action == "start" else "paused")
        else:
            abort(404)
        return redirect(url_for("index"))

    @app.post("/api/v1/session")
    def api_session():
        body = payload()
        return service.open_session(g.account_id, body.get("client_id"), body.get("username"))

    @app.post("/api/v1/heartbeat")
    def api_heartbeat():
        return service.heartbeat(g.account_id, sid(), payload().get("status", "在线"))

    @app.post("/api/v1/session/close")
    def api_close():
        service.close_session(g.account_id, sid())
        return {"closed": True}

    @app.post("/api/v1/tasks/<int:task_id>/sync")
    def api_sync(task_id):
        return service.sync(g.account_id, sid(), task_id, payload())

    @app.post("/api/v1/tasks/<int:task_id>/counts")
    def api_counts(task_id):
        return service.sync_counts(g.account_id, sid(), task_id, payload())

    @app.post("/api/v1/work/claim")
    def api_claim():
        return service.claim(g.account_id, sid())

    @app.get("/api/v1/work/<operation_id>")
    def api_operation(operation_id):
        return service.operation_status(g.account_id, sid(), operation_id)

    @app.post("/api/v1/work/<operation_id>/start")
    def api_start(operation_id):
        return service.start_operation(g.account_id, sid(), operation_id)

    @app.post("/api/v1/work/<operation_id>/result")
    def api_result(operation_id):
        return service.finish(g.account_id, sid(), operation_id, payload())

    return app


def main():
    from waitress import serve

    parser = argparse.ArgumentParser(description="qa_util 管理服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    parser.add_argument("--env-file", default=".env", help="服务端环境配置路径，默认当前目录的 .env")
    args = parser.parse_args()
    logging.basicConfig(format="%(message)s")
    logging.getLogger("waitress").setLevel(logging.INFO)
    print(f"正在加载服务端配置：{args.env_file}", flush=True)
    app = create_app(load_server_settings(args.env_file))
    print(f"日志文件：{app.extensions['qa_service'].database.path.parent / 'server.log'}；按 Ctrl+C 停止服务。", flush=True)
    try:
        serve(app, host=args.host, port=args.port, threads=8)
    finally:
        app.extensions["qa_worker"].close()
