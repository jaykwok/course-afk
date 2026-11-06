"""Application lifetime and operation boundaries, independent of browser contexts."""
from __future__ import annotations

import threading
import time

from core.abort import UserCancelRequested


class ControlSession:
    def __init__(self, *, clock=time.monotonic):
        self.lock = threading.RLock()
        self.clock = clock
        self.operation_id = None
        self.kind = None
        self.cancelled = False
        self.stop_after_current = False
        self.exit_requested = False
        self.exit_deadline = None

    def begin(self, operation_id, kind):
        with self.lock:
            if self.exit_requested or self.operation_id:
                raise RuntimeError("应用正在退出或已有操作")
            self.operation_id, self.kind = operation_id, kind
            self.cancelled = self.stop_after_current = False

    def finish(self):
        with self.lock:
            self.operation_id = self.kind = None
            if not self.exit_requested:
                self.cancelled = False

    def check_cancelled(self):
        with self.lock:
            if self.cancelled:
                raise UserCancelRequested("已取消本轮，未确认任务保留")

    def can_dispatch(self):
        with self.lock:
            self.check_cancelled()
            return not self.stop_after_current and not self.exit_requested

    def stop(self):
        with self.lock:
            self.stop_after_current = True

    def cancel(self):
        with self.lock:
            self.cancelled = True

    def request_exit(self, *, graceful=False):
        with self.lock:
            self.exit_requested = True
            self.stop_after_current = True
            # A closed management window may let one learning activity finish,
            # but cannot strand the application on platform/human waits forever.
            deadline = self.clock() + (1800 if graceful and self.kind == "learn" else 0)
            self.exit_deadline = min(self.exit_deadline, deadline) if self.exit_deadline is not None else deadline
            if not graceful or self.kind != "learn":
                self.cancelled = True

    def snapshot(self):
        with self.lock:
            return dict(operation_id=self.operation_id, kind=self.kind,
                        cancelled=self.cancelled, stop_after_current=self.stop_after_current,
                        exit_requested=self.exit_requested)
