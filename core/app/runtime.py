"""One serial controller, one management browser, and one workflow host."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import queue
import secrets
import threading
import time

from core.abort import UserCancelRequested, UserAbortRequested
from core.app.control_session import ControlSession
from core.ui.frontend import use_frontend
from core.ui.web_frontend import WebFrontend, WebLogHandler
from core.web.session import WebSession, ProtocolError

OPERATIONS = {"learn": "课程学习", "exam": "考试", "links": "添加链接",
              "references": "课程资料", "tasks": "任务管理", "account": "账号与登录"}
MANUAL_SELECTION_PROMPTS = ["请粘贴课程、主题、考试、培训班或课程集合页链接，每行一条。",
    "课程集合页可全部解析或手动选择；需报名时请在业务浏览器中人工处理。"]


class ShutdownDeadlineExceeded(RuntimeError):
    pass


def dispatch(kind, frontend):
    from core.app import launcher_controller as c
    from core.config import EXAM_URLS_FILE, LEARNING_URLS_FILE, MANUAL_EXAM_FILE
    from core.state import collect_project_state
    actions = {
        "learn": lambda: c.handle_afk(frontend),
        "exam": lambda: c.handle_ai_exam(frontend),
        "links": lambda: c.handle_manual_selection(MANUAL_SELECTION_PROMPTS, frontend),
        "references": lambda: c.handle_reference_collection(frontend),
        "tasks": lambda: c.handle_task_management(EXAM_URLS_FILE, LEARNING_URLS_FILE, MANUAL_EXAM_FILE, frontend),
        "account": lambda: c.handle_account_management(collect_project_state(), frontend),
    }
    actions[kind]()


class ApplicationRuntime:
    def __init__(self, *, dispatcher=dispatch, clock=time.monotonic):
        self.control = ControlSession(clock=clock)
        self.session = WebSession(self.control)
        self.frontend = WebFrontend(self.session)
        self.dispatcher = dispatcher
        self.commands = {}
        self.work = queue.Queue(maxsize=1)
        self.worker = threading.Thread(target=self._worker, name="web-controller", daemon=True)
        self.worker_stopped = threading.Event()
        self.worker_error = None
        self._close_result = None
        self._closed = False
        self.connected = False
        self.disconnected_at = clock()
        self.last_tick = clock()
        self.clock = clock

    def start(self):
        self.frontend.refresh_dashboard(force=True)
        self.worker.start()

    def command(self, request_id, action, payload):
        if not isinstance(request_id, str) or not 16 <= len(request_id) <= 100:
            raise ProtocolError("缺少有效 request_id", 400)
        fingerprint = hashlib.sha256(json.dumps([action, payload], sort_keys=True).encode()).hexdigest()
        with self.control.lock:
            previous = self.commands.get(request_id)
            if previous:
                if previous[0] != fingerprint:
                    raise ProtocolError("request_id 已用于不同命令")
                return previous[1]
            # Never evict accepted command IDs: an old retransmission must not
            # acquire fresh authority. A generous per-instance cap is explicit.
            if len(self.commands) >= 10000:
                if action in {"exit", "cancel"}:
                    return self._command(action, payload)
                raise ProtocolError("本次运行命令数量达到上限，请退出后重新启动", 429)
            result = self._command(action, payload)
            self.commands[request_id] = (fingerprint, result)
            return result

    def _command(self, action, payload):
        if action == "operation":
            kind = payload.get("kind")
            if kind not in OPERATIONS:
                raise ProtocolError("未知操作", 400)
            if self.control.operation_id or self.control.exit_requested:
                raise ProtocolError("已有操作正在运行或应用正在退出")
            operation_id = secrets.token_hex(16)
            self.control.begin(operation_id, kind)
            self.session.update(title=OPERATIONS[kind], status="准备操作", summary=None, progress=None)
            self.work.put_nowait((operation_id, kind))
            return dict(operation_id=operation_id)
        if action == "answer":
            self.session.answer(payload.get("prompt_id"), payload.get("operation_id"),
                                payload.get("version"), payload.get("value"))
        elif action in {"cancel", "stop-after-current"}:
            if not self.control.operation_id or payload.get("operation_id") != self.control.operation_id:
                raise ProtocolError("操作已结束或已切换")
            if action == "cancel":
                self.cancel()
            else:
                if self.control.kind != "learn":
                    raise ProtocolError("仅学习支持本课程结束后停止", 400)
                self.control.stop()
                self.session.update(status="本课程结束后停止，未开始的任务保留")
        elif action == "exit":
            self.request_exit()
        else:
            raise ProtocolError("未知命令", 400)
        return {"accepted": True}

    def cancel(self):
        from core.runtime import interrupt_running_async
        with self.control.lock:
            self.control.cancel()
            self.session.release_waiters()
            interrupt_running_async()
            self.session.update(status="正在取消并清理资源，未确认任务保留")

    def request_exit(self, *, graceful=False):
        with self.control.lock:
            self.control.request_exit(graceful=graceful)
            if self.session.view["prompt"] is not None:
                self.control.cancel()
            if self.control.cancelled:
                self.cancel()
            self.session.release_waiters()
            self.session.update(status="正在退出；学习在当前课程结束后停止，人工等待将取消" if graceful else "正在退出并清理资源")

    def connection(self, connected):
        with self.control.lock:
            self.connected = connected
            if not connected:
                self.disconnected_at = self.clock()

    def tick(self):
        with self.control.lock:
            now = self.clock()
            # Give a suspended machine a fresh reconnect grace, rather than
            # interpreting the first delayed tick as a real browser closure.
            if now - self.last_tick > 15:
                self.disconnected_at = now
            self.last_tick = now
            if not self.connected and now - self.disconnected_at > 60 and not self.control.exit_requested:
                self.request_exit(graceful=True)
            if self.control.exit_deadline is not None and now >= self.control.exit_deadline and self.control.operation_id:
                self.cancel()
            return self.control.exit_requested and (self.control.operation_id is None or self.control.cancelled)

    def _worker(self):
        try:
            while True:
                item = self.work.get()
                if item is None:
                    break
                _, kind = item
                try:
                    with use_frontend(self.frontend):
                        self.control.check_cancelled()
                        self.dispatcher(kind, self.frontend)
                except (UserCancelRequested, UserAbortRequested, asyncio.CancelledError) as exc:
                    self.frontend.show_warning(str(exc) or "已取消，待办保留")
                except Exception as exc:
                    logging.exception("管理操作失败：%s", kind)
                    self.frontend.show_error(str(exc) or type(exc).__name__)
                finally:
                    with self.control.lock:
                        self.control.finish()
                        self.session.update(prompt=None, progress=None)
                    self.frontend.refresh_dashboard(force=True)
        except BaseException as exc:
            self.worker_error = exc
            self.request_exit()
        finally:
            self.worker_stopped.set()

    async def close(self):
        if self._closed:
            if self._close_result:
                raise self._close_result
            return
        self._closed = True
        self.request_exit()
        # Await the real thread, not a cancelled asyncio.to_thread wrapper.
        deadline = asyncio.get_running_loop().time() + 30
        while self.control.operation_id and self.worker.is_alive() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(.05)
        if self.worker.is_alive():
            try:
                self.work.put_nowait(None)
            except queue.Full:
                pass
            while self.worker.is_alive() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(.05)
        if self.worker.is_alive():
            self._close_result = ShutdownDeadlineExceeded("控制线程未在 30 秒内停止；已确认数据保留")
            raise self._close_result
        self.frontend.close()


async def serve_application():
    from concurrent.futures import ThreadPoolExecutor
    import signal
    import sys
    from core.browser.application import application_browser, require_application_browser
    from core.workflow.host import bind_application_loop, shutdown_host
    from core.web.server import LocalServer
    runtime = ApplicationRuntime()
    server = LocalServer(runtime)
    root = logging.getLogger()
    handler = WebLogHandler(runtime.session)
    from core.config import _RedactingFormatter, LOG_FORMAT, _get_console_log_level
    handler.setLevel(_get_console_log_level())
    handler.setFormatter(_RedactingFormatter(LOG_FORMAT))
    root.addHandler(handler)
    previous_term = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, lambda *_: runtime.request_exit())
    bound_loop = False
    # DBOS installs a default executor backed by its own pool. Restore an
    # application-owned pool before engine shutdown so browser cleanup can still
    # use asyncio.to_thread after DBOS has stopped its executor.
    application_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="app-cleanup")
    loop = asyncio.get_running_loop()

    async def stop_workflows():
        loop.set_default_executor(application_executor)
        try:
            await loop.run_in_executor(application_executor, shutdown_host)
        finally:
            loop.set_default_executor(application_executor)
    try:
        runtime.start()
        await server.start()
        # The controller stays synchronous on its worker, while all browser and
        # workflow coroutines use this one event loop. DBOS remains lazy.
        bind_application_loop(asyncio.get_running_loop())
        bound_loop = True
        require_application_browser(True)
        async with application_browser() as browser:
            page = browser.management
            page.on("close", lambda: runtime.request_exit(graceful=True))
            server.reopen = page.bring_to_front
            await page.goto(server.launch_url, wait_until="domcontentloaded", timeout=30000)
            server.write_instance()
            while not runtime.tick():
                await asyncio.sleep(.25)
            await runtime.close()
            await stop_workflows()
    finally:
        primary_failure = sys.exc_info()[0] is not None
        cleanup_error = None
        try:
            try:
                await runtime.close()
            except BaseException as exc:
                cleanup_error = exc
            if bound_loop:
                try:
                    await stop_workflows()
                    bind_application_loop(None)
                except BaseException as exc:
                    cleanup_error = cleanup_error or exc
            try:
                await server.close()
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
        finally:
            root.removeHandler(handler)
            handler.close()
            signal.signal(signal.SIGTERM, previous_term)
            if not runtime.worker.is_alive():
                require_application_browser(False)
            application_executor.shutdown(wait=False)
        if cleanup_error:
            if primary_failure:
                logging.getLogger(__name__).error("退出资源清理失败：%s", type(cleanup_error).__name__)
            else:
                raise cleanup_error
    if runtime.worker_error:
        raise runtime.worker_error
    return 0


def launch_web():
    import os
    from core.config import DATA_DIR, setup_logging
    from core.runtime import application_lock, AlreadyRunningError, protect_application_children
    from core.startup import shutdown_workflows
    from core.web.server import reopen_existing
    setup_logging()
    try:
        with application_lock(DATA_DIR / "application.lock"):
            protect_application_children()
            failure = None
            try:
                result = asyncio.run(serve_application())
            except BaseException as exc:
                failure = exc
            finally:
                try:
                    shutdown_workflows()
                except Exception as exc:
                    logging.exception("工作流清理失败")
                    failure = failure or exc
                    from core.startup import report_fatal_error
                    report_fatal_error(failure)
                    logging.shutdown()
                    os._exit(1)
            if isinstance(failure, ShutdownDeadlineExceeded):
                from core.startup import report_fatal_error
                report_fatal_error(failure)
                logging.shutdown()
                os._exit(1)
            if failure:
                raise failure
            return result
    except AlreadyRunningError:
        if reopen_existing(DATA_DIR / "web-instance.json"):
            return 0
        raise RuntimeError("已有实例正在启动或退出，请稍后重试") from None
