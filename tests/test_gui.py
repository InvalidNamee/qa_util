import asyncio
import json
import time
import tkinter as tk

from comment_util.gui import App
from comment_util.models import RunResult


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
        def __init__(self, config, confirm, log, control):
            self.confirm, self.log, self.control = confirm, log, control
            self.directory = tmp_path

        async def run(self):
            self.log("准备预览")
            return RunResult("complete" if await self.confirm("生成的问题及回答") else "cancelled")

    monkeypatch.setattr("comment_util.gui.Workflow", PreviewWorkflow)
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
        pump(root, lambda: "已放弃" in app.logs.get("1.0", "end"))
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
        def __init__(self, config, confirm, log, control):
            self.control, self.directory = control, tmp_path

        async def run(self):
            while not self.control.stop.is_set():
                await asyncio.sleep(0.01)
            return RunResult("stopped")

    monkeypatch.setattr("comment_util.gui.Workflow", WaitingWorkflow)
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
