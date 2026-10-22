import argparse
import asyncio
import json
import os
import queue
import subprocess
import sys
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
        self.directory = None
        self.progress_rows = {}
        root.title("qa_util · 问答工具")
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
        self.log_button = ttk.Button(actions, text="打开日志", command=self.open_log, state="disabled")
        self.log_button.pack(side="right")
        self.status = tk.StringVar(value="等待开始")
        ttk.Label(container, textvariable=self.status).pack(anchor="w", pady=(0, 8))
        progress_area = ttk.Frame(container)
        progress_area.pack(fill="both", expand=True)
        canvas = tk.Canvas(progress_area, highlightthickness=0)
        scroll = ttk.Scrollbar(progress_area, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self.progress_frame = ttk.Frame(canvas)
        window = canvas.create_window(0, 0, window=self.progress_frame, anchor="nw")
        self.progress_frame.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        self.load()
        self.root.after(100, self.poll)

    @staticmethod
    def set_text(widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.configure(state="disabled")

    def reset_progress(self):
        for row in self.progress_rows.values():
            row["bar"].stop()
        for child in self.progress_frame.winfo_children():
            child.destroy()
        self.progress_rows = {}
        for account in self.config.accounts:
            row = ttk.Frame(self.progress_frame, padding=(0, 6, 12, 6))
            row.pack(fill="x")
            ttk.Label(row, text=account.username).pack(anchor="w")
            label = tk.StringVar(value="等待开始")
            ttk.Label(row, textvariable=label).pack(anchor="w", pady=3)
            bar = ttk.Progressbar(row, mode="determinate", maximum=1, value=0)
            bar.pack(fill="x")
            self.progress_rows[account.username] = {"bar": bar, "label": label, "busy": False}

    def update_progress(self, progress):
        row = self.progress_rows.get(progress.username)
        if row is None:
            return
        busy = progress.state == "running" and progress.total == 0 and not progress.detail
        bar = row["bar"]
        if row["busy"] != busy:
            bar.stop()
            bar.configure(mode="indeterminate" if busy else "determinate")
            if busy:
                bar.start(25)
            row["busy"] = busy
        if not busy:
            bar.configure(maximum=max(1, progress.total), value=progress.current)
            if progress.state == "complete" and not progress.total:
                bar.configure(value=1)
        count = f" {progress.current}/{progress.total}" if progress.total else ""
        detail = f" · {progress.detail}" if progress.detail else ""
        row["label"].set(progress.phase + count + detail)

    def open_log(self):
        path = self.directory / "debug.log" if self.directory else None
        if not path or not path.exists():
            self.status.set("日志尚未生成")
            return
        try:
            if sys.platform == "win32":
                os.startfile(str(path))
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
        except OSError:
            self.status.set(f"请手动打开日志：{path}")

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
        self.reset_progress()
        self.start_button.configure(state="normal")
        return True

    def start(self):
        if not self.load():
            return
        config = self.config
        self.stop = threading.Event()
        self.directory = None
        self.log_button.configure(state="disabled")
        self.status.set("运行中")
        self.set_running(True)

        def run():
            workflow = None

            async def confirm(text):
                decision = threading.Event()
                answer = [False]
                self.messages.put(("preview", (text, decision, answer)))
                while not decision.is_set():
                    await asyncio.sleep(0.1)
                return answer[0]

            async def execute():
                nonlocal workflow
                workflow = Workflow(config, confirm, lambda _: None, control=Control(self.stop),
                                    progress=lambda event: self.messages.put(("progress", event)))
                self.messages.put(("directory", workflow.directory))
                result = await workflow.run()
                return result, workflow.directory

            try:
                result, directory = asyncio.run(execute())
                self.messages.put(("done", (result.status, str(directory))))
            except Exception as error:
                if workflow and workflow.directory.exists():
                    workflow.record_error("后台运行失败", error)
                self.messages.put(("done", ("failed", str(workflow.directory) if workflow else "")))

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
        self.status.set("正在停止并释放浏览器资源…")

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
            if kind == "progress":
                self.update_progress(payload)
            elif kind == "directory":
                self.directory = Path(payload)
                self.log_button.configure(state="normal")
            elif kind == "preview":
                self.show_preview(payload)
            elif kind == "done":
                self.finish_preview(False)
                status, directory = payload
                labels = {"complete": "全部完成", "partial": "部分账号失败", "failed": "失败",
                          "stopped": "已停止", "cancelled": "已放弃"}
                self.status.set(f"任务结果：{labels.get(status, status)}")
                if directory:
                    self.directory = Path(directory)
                    self.log_button.configure(state="normal")
                for row in self.progress_rows.values():
                    if row["busy"]:
                        row["bar"].stop()
                        row["busy"] = False
                        row["bar"].configure(mode="determinate", value=0)
                self.set_running(False)
        if self.closing and (not self.worker or not self.worker.is_alive()):
            self.root.destroy()
            return
        self.root.after(100, self.poll)


def main():
    parser = argparse.ArgumentParser(prog="qa_util", description="智慧树问答工具")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="JSON配置路径")
    args = parser.parse_args()
    root = tk.Tk()
    App(root, args.config)
    root.mainloop()
