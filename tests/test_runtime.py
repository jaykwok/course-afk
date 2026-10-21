from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest

from core.runtime import close_safely, interrupt_running_async, run_operation


def process_alive(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_finishes_before_propagating_cancellation(self):
        entered, finished = asyncio.Event(), asyncio.Event()
        async def close():
            entered.set()
            await asyncio.sleep(0.03)
            finished.set()
        task = asyncio.create_task(close_safely(close(), timeout=1))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())


class OperationTests(unittest.TestCase):
    def test_repeated_cancel_does_not_interrupt_cleanup(self):
        ready, cleaning, cleaned = threading.Event(), threading.Event(), threading.Event()
        async def operation():
            ready.set()
            try:
                await asyncio.sleep(10)
            finally:
                cleaning.set()
                await asyncio.sleep(0.15)
                cleaned.set()
        def worker():
            try:
                run_operation(operation())
            except asyncio.CancelledError:
                pass
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(ready.wait(3))
            self.assertTrue(interrupt_running_async())
            self.assertTrue(cleaning.wait(3))
            self.assertTrue(interrupt_running_async())
        finally:
            thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertTrue(cleaned.is_set())

    @unittest.skipUnless(os.name == "nt", "Windows Job Object integration")
    def test_application_job_reaps_descendant_after_parent_termination(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "child.pid"
            code = (
                "from core.runtime import protect_application_children; "
                "protect_application_children(); import subprocess,sys,time; from pathlib import Path; "
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(5)'],creationflags=subprocess.CREATE_NO_WINDOW); "
                "Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(5)"
            )
            parent = subprocess.Popen([sys.executable, "-c", code, str(pidfile)], cwd=root, creationflags=subprocess.CREATE_NO_WINDOW, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 3
                while not pidfile.exists() and parent.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(pidfile.exists(), "isolated job owner did not start")
                child_pid = int(pidfile.read_text())
                self.assertTrue(process_alive(child_pid))
                parent.terminate()
                parent.wait(timeout=3)
                deadline = time.monotonic() + 2
                while process_alive(child_pid) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(process_alive(child_pid))
            finally:
                if parent.poll() is None:
                    parent.terminate()
                parent.communicate(timeout=3)
