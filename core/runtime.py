"""Operation cancellation, bounded cleanup, and ownership of native children.

No configuration or UI imports: browser lifecycle helpers stay independent.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import threading


@dataclass
class _Operation:
    loop: asyncio.AbstractEventLoop
    task: asyncio.Task | None = None
    stopping: bool = False


_operations: dict[int, _Operation] = {}
_lock = threading.Lock()


def run_operation(awaitable, *, exception_handler_factory=None):
    thread_id = threading.get_ident()
    try:
        with asyncio.Runner() as runner:
            loop = runner.get_loop()
            if exception_handler_factory:
                loop.set_exception_handler(exception_handler_factory(loop.get_exception_handler()))
            operation = _Operation(loop)
            with _lock:
                _operations[thread_id] = operation

            async def tracked():
                operation.task = asyncio.current_task()
                if operation.stopping:
                    operation.task.cancel()
                return await awaitable

            return runner.run(tracked())
    finally:
        # Keep registration throughout Runner.__exit__: a second ESC must not
        # abandon the UI while asynchronous generators/resources are closing.
        with _lock:
            _operations.pop(thread_id, None)
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()


def interrupt_running_async(*, include_current: bool = False) -> bool:
    """Idempotently request cancellation; repeated requests cannot cancel cleanup."""
    current = threading.get_ident()
    with _lock:
        candidates = [op for tid, op in _operations.items() if include_current or tid != current]
        for operation in candidates:
            if operation.stopping:
                continue
            operation.stopping = True

            def cancel(op=operation):
                if op.task is not None and not op.task.done():
                    op.task.cancel()

            try:
                operation.loop.call_soon_threadsafe(cancel)
            except RuntimeError:
                # The loop can finish between the registry snapshot and dispatch.
                pass
    return bool(candidates)


async def close_safely(awaitable, *, timeout: float = 5, label: str = "resource") -> None:
    """Drain a close operation with a deadline, preserving a caller's cancellation."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        while not task.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError
            try:
                await asyncio.wait_for(asyncio.shield(task), remaining)
            except asyncio.CancelledError:
                if task.cancelled():
                    break
                cancelled = True
    except Exception as exc:
        logging.warning("Closing %s failed: %s", label, type(exc).__name__)
    finally:
        if not task.done():
            task.cancel()
            done, pending = await asyncio.wait({task}, timeout=0.5)
            if pending:
                logging.error("Closing %s did not stop within its cleanup deadline", label)
            for finished in done:
                if not finished.cancelled():
                    finished.exception()
        elif not task.cancelled():
            task.exception()
    if cancelled:
        raise asyncio.CancelledError


class WindowsJob:
    """A kill-on-close Windows job; its handle is never inherited by children."""

    def __init__(self, pid: int):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_longlong) for name in ("TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime")] + [(name, wintypes.DWORD) for name in ("TotalPageFaultCount", "TotalProcesses", "ActiveProcesses", "TotalTerminatedProcesses")]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        kernel.QueryInformationJobObject.restype = wintypes.BOOL
        self._kernel = kernel
        self._accounting_type = Accounting
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        process = None
        try:
            limits = ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            process = kernel.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
            if not process or not kernel.AssignProcessToJobObject(handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle = handle
        except BaseException:
            kernel.CloseHandle(handle)
            raise
        finally:
            if process:
                kernel.CloseHandle(process)

    def close(self) -> None:
        if self.handle:
            self._kernel.CloseHandle(self.handle)
            self.handle = None

    async def terminate(self, timeout: float = 3) -> None:
        """Termination is asynchronous in Windows; wait for the entire job."""
        if not self.handle:
            return
        import ctypes
        try:
            if not self._kernel.TerminateJobObject(self.handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())
            async with asyncio.timeout(timeout):
                while True:
                    accounting = self._accounting_type()
                    if not self._kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                        raise ctypes.WinError(ctypes.get_last_error())
                    if accounting.ActiveProcesses == 0:
                        break
                    await asyncio.sleep(0.02)
        finally:
            self.close()


_application_job: WindowsJob | None = None


def protect_application_children() -> None:
    """Guard the whole application before any driver is spawned.

    Do not explicitly close this job: it also owns the application itself.
    Windows closes the non-inherited handle on process death, killing descendants
    even when Python cannot run finally (Task Manager / TerminateProcess).
    """
    global _application_job
    if os.name == "nt" and _application_job is None:
        _application_job = WindowsJob(os.getpid())


def own_playwright_driver(playwright) -> WindowsJob | None:
    """Isolate the Playwright transport detail in one adapter."""
    implementation = getattr(playwright, "_impl_obj", playwright)
    connection = getattr(implementation, "_connection", None)
    transport = getattr(connection, "_transport", None)
    process = getattr(transport, "_proc", None)
    pid = getattr(process, "pid", None)
    return WindowsJob(pid) if os.name == "nt" and isinstance(pid, int) else None


@contextmanager
def application_lock(path: Path):
    """One UI process per data directory; prevents conflicting queue writers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock_file:
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"0")
                lock_file.flush()
            lock_file.seek(0)
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("本项目已有运行实例，请先关闭原窗口") from exc
        else:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            lock_file.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
