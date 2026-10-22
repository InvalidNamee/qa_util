import asyncio
import threading
from collections.abc import Awaitable
from typing import TypeVar

from .models import Stopped

T = TypeVar("T")


class Control:
    def __init__(self, stop: threading.Event | None = None):
        self.stop = stop if stop is not None else threading.Event()

    def check(self):
        if self.stop.is_set():
            raise Stopped()

    async def sleep(self, seconds: float = 0.2):
        remaining = seconds
        while remaining > 0:
            self.check()
            step = min(remaining, 0.2)
            await asyncio.sleep(step)
            remaining -= step
        self.check()

    async def interruptible(self, awaitable: Awaitable[T]) -> T:
        """用于 HTTP/确认等可取消操作；不要用于 Playwright 调用。"""
        task = asyncio.ensure_future(awaitable)
        try:
            while not task.done():
                self.check()
                await asyncio.wait({task}, timeout=0.2)
            self.check()
            return task.result()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
