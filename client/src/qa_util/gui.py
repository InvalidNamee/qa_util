import argparse
import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, ttk
from tkinter.scrolledtext import ScrolledText

from .config import DEFAULT_CONFIG, load_config
from .control import Control
from .workflow import ClientRunner


class App:
    def __init__(self, root, path):
        self.root, self.config = root, None
        self.messages = queue.Queue()
        self.worker = None
        self.stop = threading.Event()
        self.closing = False
        self.directory = None
        root.title("qa_util · 单账号客户端")
        root.geometry("820x560")
        root.minsize(640, 480)
        root.protocol("WM_DELETE_WINDOW", self.close)
        container = ttk.Frame(root, padding=16)
        container.pack(fill="both", expand=True)
        row = ttk.Frame(container)
        row.pack(fill="x")
        self.path = tk.StringVar(value=str(path))
        self.path_entry = ttk.Entry(row, textvariable=self.path)
        self.path_entry.pack(side="left", fill="x", expand=True)
        self.choose = ttk.Button(row, text="选择配置", command=self.select)
        self.choose.pack(side="left", padx=8)
        self.reload = ttk.Button(row, text="加载", command=self.load)
        self.reload.pack(side="left")
        self.config_view = ScrolledText(container, height=9, wrap="word", state="disabled")
        self.config_view.pack(fill="x", pady=12)
        buttons = ttk.Frame(container)
        buttons.pack(fill="x")
        self.start_button = ttk.Button(buttons, text="开始", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, text="停止", command=self.request_stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.log_button = ttk.Button(buttons, text="打开日志", command=self.open_log, state="disabled")
        self.log_button.pack(side="right")
        self.status = tk.StringVar(value="等待开始")
        self.task_name = tk.StringVar(value="服务端自动分配已参与的任务")
        ttk.Label(container, textvariable=self.status, wraplength=760).pack(anchor="w", pady=(18, 6))
        ttk.Label(container, textvariable=self.task_name).pack(anchor="w")
        self.question_label = tk.StringVar(value="我的提问：0 / 0")
        ttk.Label(container, textvariable=self.question_label).pack(anchor="w", pady=(12, 3))
        self.question_bar = ttk.Progressbar(container, maximum=1)
        self.question_bar.pack(fill="x")
        self.pool_label = tk.StringVar(value="任务题池回答达标：0 / 0")
        ttk.Label(container, textvariable=self.pool_label).pack(anchor="w", pady=(12, 3))
        self.pool_bar = ttk.Progressbar(container, maximum=1)
        self.pool_bar.pack(fill="x")
        self.answers_label = tk.StringVar(value="我的累计回答：0 / 0")
        ttk.Label(container, textvariable=self.answers_label).pack(anchor="w", pady=8)
        self.load()
        root.after(100, self.poll)

    def load(self):
        if self.worker and self.worker.is_alive():
            return False
        try:
            self.config = load_config(self.path.get())
            content = json.dumps(self.config.public_dict(), ensure_ascii=False, indent=2)
        except Exception as error:
            self.config = None
            content = str(error) + "\n参考 client.example.json。"
        self.config_view.configure(state="normal")
        self.config_view.delete("1.0", "end")
        self.config_view.insert("end", content)
        self.config_view.configure(state="disabled")
        self.start_button.configure(state="normal" if self.config else "disabled")
        return self.config is not None

    def select(self):
        path = filedialog.askopenfilename(initialdir=str(Path(self.path.get()).expanduser().parent),
                                          filetypes=[("JSON 配置", "*.json")])
        if path:
            self.path.set(path)
            self.load()

    def set_running(self, active):
        for widget in (self.start_button, self.path_entry, self.choose, self.reload):
            widget.configure(state="disabled" if active else "normal")
        self.stop_button.configure(state="normal" if active else "disabled")

    def start(self):
        if not self.load():
            return
        self.stop = threading.Event()
        self.directory = None
        self.log_button.configure(state="disabled")
        self.question_bar.configure(value=0)
        self.pool_bar.configure(value=0)
        self.status.set("连接服务端")
        self.set_running(True)
        config = self.config

        def run():
            runner = None
            try:
                runner = ClientRunner(config, control=Control(self.stop),
                                      progress=lambda event: self.messages.put(("progress", event)))
                self.messages.put(("directory", runner.directory))
                result = asyncio.run(runner.run())
            except Exception as error:
                if runner:
                    runner.log(traceback.format_exc())
                self.messages.put(("progress", {"phase": "失败", "detail": config.redact(str(error))}))
                result = "failed"
            self.messages.put(("done", result))

        self.worker = threading.Thread(target=run, name="qa-client", daemon=False)
        self.worker.start()

    def update_progress(self, progress):
        detail = progress.get("detail", "")
        self.status.set(progress.get("phase", "运行中") + (" · " + detail if detail else ""))
        self.task_name.set(progress.get("task_name") or "等待服务端任务")
        current, total = progress.get("question_current", 0), progress.get("question_total", 0)
        self.question_bar.configure(maximum=max(1, total), value=current)
        self.question_label.set(f"我的提问：{current} / {total}")
        current, total = progress.get("pool_current", 0), progress.get("pool_total", 0)
        self.pool_bar.configure(maximum=max(1, total), value=current)
        self.pool_label.set(f"任务题池回答达标：{current} / {total}")
        target = progress.get("answer_total", 0)
        if target:
            self.answers_label.set(f"我的累计回答：{progress.get('answer_current', 0)} / {target} · 本任务已覆盖：{progress.get('answers_done', 0)}")
        else:
            self.answers_label.set(f"本任务已覆盖回答：{progress.get('answers_done', 0)}")

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

    def request_stop(self):
        self.stop.set()
        self.stop_button.configure(state="disabled")
        self.status.set("正在停止并保存本地状态…")

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
            elif kind == "done":
                if payload != "failed":
                    self.status.set("已停止；保留本地登录和待核对记录")
                self.set_running(False)
        if self.closing and (not self.worker or not self.worker.is_alive()):
            self.root.destroy()
            return
        self.root.after(100, self.poll)


def main():
    parser = argparse.ArgumentParser(prog="qa_util", description="qa_util 单账号客户端")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    root = tk.Tk()
    App(root, args.config)
    root.mainloop()
