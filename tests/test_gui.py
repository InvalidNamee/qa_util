import asyncio
import json
import time
import tkinter as tk
from dataclasses import replace

from qa_util.gui import App
from client_fakes import FastControl, Website, flask_transport
from server_support import forum_url


def pump(root, predicate, timeout=5):
    end = time.monotonic() + timeout
    while not predicate():
        root.update()
        if time.monotonic() > end:
            raise AssertionError("Tk界面没有及时响应")
        time.sleep(0.01)
    root.update()


def write_config(config, path):
    raw = config.public_dict()
    raw["access_token"] = config.access_token
    raw["account"]["password"] = config.account.password
    path.write_text(json.dumps(raw))


def test_single_account_gui_displays_progress_without_logs_or_preview(config, tmp_path, monkeypatch):
    path = tmp_path / "client.json"
    write_config(config, path)
    root = tk.Tk()
    root.withdraw()
    app = App(root, path)
    try:
        assert config.account.password not in app.config_view.get("1.0", "end")
        assert config.access_token not in app.config_view.get("1.0", "end")
        assert not hasattr(app, "logs") and not hasattr(app, "preview_window")
        app.messages.put(("progress", {"phase": "补答", "detail": "等待人工验证", "task_name": "网络课程",
                                       "question_current": 20, "question_total": 20,
                                       "pool_current": 3, "pool_total": 5, "answers_done": 8}))
        pump(root, lambda: "等待人工验证" in app.status.get())
        assert app.question_bar["value"] == app.question_bar["maximum"] == 20
        assert app.pool_bar["value"] == 3 and app.pool_bar["maximum"] == 5
        app.directory = tmp_path
        (tmp_path / "debug.log").write_text("日志")
        opened = []
        monkeypatch.setattr("qa_util.gui.sys.platform", "darwin")
        monkeypatch.setattr("qa_util.gui.subprocess.Popen", opened.append)
        app.open_log()
        assert opened == [["open", str(tmp_path / "debug.log")]]
    finally:
        root.destroy()


def test_gui_drives_real_client_protocol_and_keeps_complete_progress(app, service, config, tmp_path, monkeypatch):
    from qa_util.workflow import ClientRunner
    aid, token = service.create_account("a")
    task = service.create_task("GUI任务", forum_url(), 1, 1, [aid])
    service.set_task_state(task, "running")
    site = Website()
    site.add(forum_url(), "a", "已经有回答的问题是什么？", answers=1)
    site.historical_answers[(forum_url(), "a")] = 1
    cfg = replace(config, access_token=token)
    path = tmp_path / "client.json"
    write_config(cfg, path)

    def runner(*args, **kwargs):
        kwargs["control"] = FastControl(kwargs["control"].stop)
        return ClientRunner(*args, **kwargs, forum_factory=site.factory, transport=flask_transport(app))

    monkeypatch.setattr("qa_util.gui.ClientRunner", runner)
    root = tk.Tk()
    root.withdraw()
    ui = App(root, path)
    try:
        ui.start()
        pump(root, lambda: ui.pool_bar["value"] == 1)
        assert ui.question_bar["value"] == 1
        assert ui.worker.is_alive()  # 完成后等待管理员的新任务。
        ui.request_stop()
        pump(root, lambda: not ui.worker.is_alive())
        pump(root, lambda: "已停止" in ui.status.get())
        assert ui.pool_bar["value"] == ui.pool_bar["maximum"] == 1
        assert (ui.directory / "debug.log").exists()
    finally:
        ui.stop.set()
        if ui.worker:ui.worker.join(timeout=3)
        root.destroy()


def test_close_during_wait_stops_background_worker(config, tmp_path, monkeypatch):
    path = tmp_path / "client.json"
    write_config(config, path)

    class WaitingRunner:
        def __init__(self, config, control, progress):
            self.control, self.directory = control, tmp_path

        async def run(self):
            while not self.control.stop.is_set():
                await asyncio.sleep(0.01)
            return "stopped"

    monkeypatch.setattr("qa_util.gui.ClientRunner", WaitingRunner)
    root = tk.Tk()
    root.withdraw()
    ui = App(root, path)
    ui.start()
    root.update()
    ui.close()
    ui.worker.join(timeout=3)
    assert not ui.worker.is_alive()
    root.update()
    root.destroy()
