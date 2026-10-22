import argparse
import asyncio
import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk
from tkinter.scrolledtext import ScrolledText

from .config import DEFAULT_CONFIG, load_config
from .control import Control
from .workflow import Workflow


class App:
    def __init__(self, root: tk.Tk, path: Path):
        self.root, self.config = root, None
        self.messages = queue.Queue()
        self.worker = None
        self.stop = threading.Event()
        self.preview_window = None
        self.decision = None
        self.closing = False
        root.title("智慧树问答工具")
        root.geometry("900x720")
        root.minsize(660, 480)
        root.protocol("WM_DELETE_WINDOW", self.close)
        container = ttk.Frame(root, padding=12)
        container.pack(fill="both", expand=True)
        row = ttk.Frame(container)
        row.pack(fill="x", pady=(0, 8))
        self.path = tk.StringVar(value=str(path))
        self.path_entry = ttk.Entry(row, textvariable=self.path)
        self.path_entry.pack(side="left", fill="x", expand=True)
        self.path_entry.bind("<Return>", lambda _: self.load())
        self.choose = ttk.Button(row, text="选择配置", command=self.select)
        self.choose.pack(side="left", padx=(8, 0))
        self.reload = ttk.Button(row, text="加载", command=self.load)
        self.reload.pack(side="left", padx=(8, 0))
        self.config_view = ScrolledText(container, height=13, wrap="word", state="disabled")
        self.config_view.pack(fill="x", pady=(0, 8))
        actions = ttk.Frame(container)
        actions.pack(fill="x", pady=(0, 8))
        self.start_button = ttk.Button(actions, text="开始", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="停止", command=self.request_stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.logs = ScrolledText(container, wrap="word", state="disabled")
        self.logs.pack(fill="both", expand=True)
        self.load()
        self.root.after(100, self.poll)

    @staticmethod
    def set_text(widget, text, *, append=False):
        widget.configure(state="normal")
        if not append:
            widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.configure(state="disabled")
        if append:
            widget.see("end")

    def log(self, text):
        self.set_text(self.logs, text + "\n", append=True)

    def select(self):
        path = filedialog.askopenfilename(
            initialdir=str(Path(self.path.get()).expanduser().parent),
            filetypes=[("JSON配置", "*.json"), ("所有文件", "*")],
        )
        if path:
            self.path.set(path)
            self.load()

    def load(self):
        if self.worker and self.worker.is_alive():
            return False
        try:
            self.config = load_config(self.path.get())
        except Exception as error:
            self.config = None
            self.set_text(self.config_view, f"配置尚未加载：{error}\n\n可参考 config.example.json。")
            self.start_button.configure(state="disabled")
            return False
        self.set_text(self.config_view, json.dumps(self.config.public_dict(), ensure_ascii=False, indent=2))
        self.start_button.configure(state="normal")
        return True

    def start(self):
        if not self.load():
            return
        config = self.config
        self.stop = threading.Event()
        self.set_running(True)

        def run():
            async def confirm(text):
                decision = threading.Event()
                answer = [False]
                self.messages.put(("preview", (text, decision, answer)))
                while not decision.is_set():
                    await asyncio.sleep(0.1)
                return answer[0]

            async def execute():
                workflow = Workflow(config, confirm, lambda line: self.messages.put(("log", line)),
                                    control=Control(self.stop))
                result = await workflow.run()
                return result, workflow.directory

            try:
                result, directory = asyncio.run(execute())
                self.messages.put(("done", (result.status, str(directory))))
            except Exception as error:
                self.messages.put(("log", config.redact(f"运行失败：{error}")))
                self.messages.put(("done", ("failed", "")))

        self.worker = threading.Thread(target=run, name="forum-worker", daemon=False)
        self.worker.start()

    def set_running(self, active):
        for widget in (self.path_entry, self.choose, self.reload, self.start_button):
            widget.configure(state="disabled" if active else "normal")
        self.stop_button.configure(state="normal" if active else "disabled")

    def show_preview(self, payload):
        text, event, answer = payload
        if self.stop.is_set() or self.closing:
            event.set()
            return
        window = tk.Toplevel(self.root)
        self.preview_window = window
        self.decision = (event, answer)
        window.title("确认生成内容")
        window.geometry("880x680")
        window.transient(self.root)
        ttk.Label(window, text="确认后发布以下内容；放弃则结束任务。", padding=10).pack(anchor="w")
        preview = ScrolledText(window, wrap="word")
        preview.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        preview.insert("end", text)
        preview.configure(state="disabled")
        buttons = ttk.Frame(window, padding=(10, 0, 10, 10))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="确认并开始发布", command=lambda: self.finish_preview(True)).pack(side="left")
        ttk.Button(buttons, text="放弃", command=lambda: self.finish_preview(False)).pack(side="left", padx=10)
        window.protocol("WM_DELETE_WINDOW", lambda: self.finish_preview(False))

    def finish_preview(self, accepted):
        if self.decision:
            event, answer = self.decision
            answer[0] = accepted
            event.set()
            self.decision = None
        if self.preview_window:
            self.preview_window.destroy()
            self.preview_window = None

    def request_stop(self):
        self.stop.set()
        self.finish_preview(False)
        self.stop_button.configure(state="disabled")
        self.log("正在停止并释放浏览器资源…")

    def close(self):
        self.closing = True
        if self.worker and self.worker.is_alive():
            self.request_stop()
        else:
            self.root.destroy()

    def poll(self):
        while True:
            try:
                kind, payload = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.log(payload)
            elif kind == "preview":
                self.show_preview(payload)
            elif kind == "done":
                self.finish_preview(False)
                status, directory = payload
                labels = {"complete": "全部完成", "partial": "部分账号失败", "failed": "失败",
                          "stopped": "已停止", "cancelled": "已放弃"}
                self.log(f"任务结果：{labels.get(status, status)}" + (f"，产物：{directory}" if directory else ""))
                self.set_running(False)
        if self.closing and (not self.worker or not self.worker.is_alive()):
            self.root.destroy()
            return
        self.root.after(100, self.poll)


def main():
    parser = argparse.ArgumentParser(description="智慧树问答工具")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="JSON配置路径")
    args = parser.parse_args()
    root = tk.Tk()
    App(root, args.config)
    root.mainloop()
