import asyncio
import json
import time
import tkinter as tk

from qa_util.gui import App
from qa_util.models import AccountProgress, RunResult


def pump(root, predicate, timeout=4):
    end = time.monotonic() + timeout
    while not predicate():
        root.update()
        if time.monotonic() > end:
            raise AssertionError("Tk界面没有及时响应")
        time.sleep(0.01)
    root.update()


def test_gui_preview_cancel_is_responsive_and_redacts(config, tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    from dataclasses import asdict
    path.write_text(json.dumps(asdict(config)))

    class PreviewWorkflow:
        def __init__(self, config, confirm, log, control, progress):
            self.confirm, self.log, self.control = confirm, log, control
            self.directory = tmp_path

        async def run(self):
            self.log("准备预览")
            return RunResult("complete" if await self.confirm("生成的问题及回答") else "cancelled")

    monkeypatch.setattr("qa_util.gui.Workflow", PreviewWorkflow)
    root = tk.Tk()
    root.withdraw()
    app = App(root, path)
    try:
        assert config.accounts[0].password not in app.config_view.get("1.0", "end")
        assert config.llm.api_key not in app.config_view.get("1.0", "end")
        app.start()
        pump(root, lambda: app.preview_window is not None)
        assert app.worker.is_alive()
        app.finish_preview(False)
        pump(root, lambda: not app.worker.is_alive())
        pump(root, lambda: "已放弃" in app.status.get())
        assert not hasattr(app, "logs")
    finally:
        app.stop.set()
        app.finish_preview(False)
        if app.worker:
            app.worker.join(timeout=3)
        root.destroy()


def test_close_during_background_wait_stops_worker(config, tmp_path, monkeypatch):
    from dataclasses import asdict
    path = tmp_path / "config.json"
    path.write_text(json.dumps(asdict(config)))

    class WaitingWorkflow:
        def __init__(self, config, confirm, log, control, progress):
            self.control, self.directory = control, tmp_path

        async def run(self):
            while not self.control.stop.is_set():
                await asyncio.sleep(0.01)
            return RunResult("stopped")

    monkeypatch.setattr("qa_util.gui.Workflow", WaitingWorkflow)
    root = tk.Tk()
    root.withdraw()
    app = App(root, path)
    app.start()
    root.update()
    app.close()
    app.worker.join(timeout=3)
    assert not app.worker.is_alive() and app.stop.is_set()
    root.update()
    root.destroy()


def test_progress_retains_count_during_captcha_and_opens_log(config, tmp_path, monkeypatch):
    from dataclasses import asdict, replace
    path = tmp_path / "config.json"
    path.write_text(json.dumps(asdict(config)))
    root = tk.Tk()
    root.withdraw()
    app = App(root, path)
    try:
        progress = AccountProgress(config.accounts[0].username, "互答", 3, 10, "running")
        app.messages.put(("progress", progress))
        pump(root, lambda: "3/10" in app.progress_rows[progress.username]["label"].get())
        row = app.progress_rows[progress.username]
        assert row["bar"]["value"] == 3 and row["bar"]["maximum"] == 10
        app.update_progress(replace(progress, detail="等待人工验证"))
        assert row["bar"]["value"] == 3
        assert "等待人工验证" in row["label"].get()
        assert app.progress_rows[config.accounts[1].username]["label"].get() == "等待开始"
        app.directory = tmp_path
        log = tmp_path / "debug.log"
        log.write_text("诊断信息只存在文件")
        opened = []
        monkeypatch.setattr("qa_util.gui.sys.platform", "darwin")
        monkeypatch.setattr("qa_util.gui.subprocess.Popen", opened.append)
        app.open_log()
        assert opened == [["open", str(log)]]
        app.update_progress(replace(progress, total=0, current=0, phase="生成内容"))
        assert row["busy"]
        app.messages.put(("done", ("failed", str(tmp_path))))
        pump(root, lambda: "失败" in app.status.get())
        assert not row["busy"]
    finally:
        root.destroy()


def test_gui_receives_real_workflow_progress_and_keeps_logs_in_files(config, tmp_path, monkeypatch):
    from dataclasses import asdict
    from qa_util.workflow import Workflow
    from conftest import FakeGenerator, FakePool

    path = tmp_path / "config.json"
    path.write_text(json.dumps(asdict(config)))
    pool = FakePool(config)

    def workflow_factory(*args, **kwargs):
        return Workflow(*args, **kwargs, output_root=tmp_path, browser_factory=lambda *_: pool,
                        generator=FakeGenerator())

    monkeypatch.setattr("qa_util.gui.Workflow", workflow_factory)
    root = tk.Tk()
    root.withdraw()
    app = App(root, path)
    try:
        app.start()
        pump(root, lambda: app.preview_window is not None)
        app.finish_preview(True)
        pump(root, lambda: not app.worker.is_alive())
        pump(root, lambda: "全部完成" in app.status.get())
        for account in config.accounts:
            row = app.progress_rows[account.username]
            assert row["bar"]["value"] == row["bar"]["maximum"] == 10
            assert "已完成" in row["label"].get()
        assert (app.directory / "run.log").exists() and (app.directory / "debug.log").exists()
        assert "全部账号已完成" in (app.directory / "run.log").read_text()
        assert str(app.log_button["state"]) == "normal"
    finally:
        app.stop.set()
        app.finish_preview(False)
        if app.worker:
            app.worker.join(timeout=3)
        root.destroy()
