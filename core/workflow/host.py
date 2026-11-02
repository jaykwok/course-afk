"""A single application event loop and a thin DBOS lifecycle/authorization adapter.

Dispatch and outcome transactions live in the business repository. DBOS alone
owns step replay; this module never interprets its checkpoint tables.
"""

from __future__ import annotations

import asyncio
import atexit
from contextvars import ContextVar
import json
import logging
from pathlib import Path
import threading

from dbos import DBOS, DBOSClient, SetWorkflowID
from sqlalchemy import URL

from core.tasks.identity import account_id, parse_entry
from core.tasks.repository import TaskRepository
from core.workflow.contracts import Status, TaskOutcome

VERSION = "course-afk-v1"
activity_context: ContextVar[dict | None] = ContextVar("activity_context", default=None)
_host = None
_host_lock = threading.Lock()


def current_host():
    if _host is None:
        raise RuntimeError("工作流执行器尚未启动")
    return _host


def get_host():
    global _host
    with _host_lock:
        if _host is None:
            from core.config import DATA_DIR

            _host = WorkflowHost(DATA_DIR / "workflows")
            try:
                _host.start()
            except BaseException:
                _host = None
                raise
    return _host


def shutdown_host():
    global _host
    if _host is not None:
        _host.close()
        _host = None


atexit.register(shutdown_host)


class WorkflowHost:
    def __init__(
        self, directory: Path, *, activities=None, identity_provider=account_id
    ):
        self.directory = Path(directory).resolve()
        self.repository = TaskRepository(self.directory / "state.sqlite")
        self.url = URL.create(
            "sqlite", database=str(self.directory / "execution.sqlite")
        ).render_as_string(hide_password=False)
        self.identity_provider = identity_provider
        self.activities = activities
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self._run, name="workflow-loop", daemon=True
        )
        self.scopes = {}
        self.active = {}
        self.stopping = False
        self.status_callback = None

    def start(self):
        self.thread.start()
        try:
            self.submit(self.initialize()).result(45)
        except BaseException:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(10)
            raise

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()
        self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        self.loop.run_until_complete(self.loop.shutdown_default_executor(timeout=5))
        self.loop.close()

    def submit(self, awaitable):
        return asyncio.run_coroutine_threadsafe(awaitable, self.loop)

    async def initialize(self):
        global _host
        _host = self
        from core.workflow.definitions import WORKFLOWS

        self.definitions = WORKFLOWS

        # Public client, before launch: an interrupted browser must never reopen
        # merely because the user opened the menu with a different account.
        engine_file = self.directory / "execution.sqlite"
        if engine_file.exists() and engine_file.stat().st_size:
            client = DBOSClient(
                system_database_url=self.url, application_name="course-afk"
            )
            try:
                pending = await asyncio.to_thread(
                    client.list_workflows,
                    status=["PENDING", "ENQUEUED"],
                    load_input=False,
                    load_output=False,
                )
                if pending:
                    await asyncio.to_thread(
                        client.cancel_workflows, [w.workflow_id for w in pending]
                    )
                    for workflow in pending:
                        self.repository.dispatch(workflow.workflow_id, "INTERRUPTED")
            finally:
                await asyncio.to_thread(client.destroy)
        DBOS(
            config={
                "name": "course-afk",
                "application_version": VERSION,
                "system_database_url": self.url,
                "enable_otlp": False,
                "log_level": "WARNING",
                "max_executor_threads": 4,
            }
        )
        DBOS.launch()
        self.dispatch_lock = asyncio.Lock()

    def authorize(self, payload):
        scope = self.scopes.get(payload["run_id"])
        if (
            self.stopping
            or scope is None
            or scope["task_id"] != payload["task_id"]
            or payload["account"] == "unbound"
            or self.identity_provider() != payload["account"]
            or scope["account"] != payload["account"]
        ):
            raise PermissionError("当前账号/操作未授权恢复此任务，请核对凭证与任务归属")
        return scope

    async def execute(
        self, url: str, kind: str, *, options=None, status_callback=None
    ) -> dict:
        from core.workflow.definitions import WORKFLOWS

        async with self.dispatch_lock:
            account = self.identity_provider()
            if account == "unbound":
                raise ValueError(
                    "缺少可验证的账号标识，请先更新登录凭证；旧数据可用迁移工具明确关联"
                )
            entry = parse_entry(
                url, account, kind="references" if kind == "references" else None
            )
            task_id = self.repository.upsert(entry)
            run = self.repository.plan(task_id, kind, options or {}, VERSION)
            if run["version"] != VERSION:
                outcome = TaskOutcome(
                    task_id, Status.NEEDS_REVIEW, ["workflow_version_incompatible"]
                ).to_dict()
                return self.repository.incompatible(run["run_id"], outcome)
            payload = json.loads(run["payload"])
            self.scopes[run["run_id"]] = {**payload, "options": options or {}}
            self.status_callback = status_callback
            try:
                status = await DBOS.get_workflow_status_async(run["run_id"])
                if status is None:
                    with SetWorkflowID(run["run_id"]):
                        handle = await DBOS.start_workflow_async(
                            WORKFLOWS[kind], payload
                        )
                elif status.status in {
                    "CANCELLED",
                    "ERROR",
                    "MAX_RECOVERY_ATTEMPTS_EXCEEDED",
                }:
                    handle = await DBOS.resume_workflow_async(run["run_id"])
                else:
                    handle = await DBOS.retrieve_workflow_async(run["run_id"])
                self.repository.dispatch(run["run_id"], "DISPATCHED")
                result = await handle.get_result()
                # Also closes the dispatch/checkpoint window if a terminal DBOS
                # result already existed when the UI was interrupted.
                return self.repository.save_outcome(
                    run["run_id"], result, finalized=True
                )
            except asyncio.CancelledError:
                await self.cancel(run["run_id"])
                raise
            finally:
                self.scopes.pop(run["run_id"], None)
                self.status_callback = None

    async def perform(self, payload: dict) -> dict:
        scope = self.authorize(payload)  # Never a cached, separate precondition step.
        run_id = payload["run_id"]
        saved = self.repository.run(run_id)
        if saved["outcome"]:
            return json.loads(saved["outcome"])
        self.active[run_id] = asyncio.current_task()
        self.repository.running(payload["task_id"])
        token = activity_context.set(payload)
        try:
            if self.activities is None:
                from core.workflow.activities import ACTIVITIES

                activity = ACTIVITIES[payload["kind"]]
            else:
                activity = self.activities[payload["kind"]]
            effective = {**payload, "options": dict(payload["options"])}
            # Resuming cannot silently restore an old auto-submit permission.
            if payload["kind"] == "exam":
                effective["options"]["auto_submit"] = bool(
                    payload["options"].get("auto_submit")
                    and scope["options"].get("auto_submit")
                )
            result = await activity(self, effective)
            if isinstance(result, TaskOutcome):
                result = result.to_dict()
            return self.repository.save_outcome(run_id, result)
        except asyncio.CancelledError:
            self.repository.dispatch(run_id, "INTERRUPTED")
            raise
        except Exception as exc:
            from core.workflow.errors import error_outcome

            result = error_outcome(payload["task_id"], exc)
            logging.exception("工作流活动失败: %s", payload["kind"])
            return self.repository.save_outcome(run_id, result.to_dict())
        finally:
            activity_context.reset(token)
            self.active.pop(run_id, None)

    async def cancel(self, run_id: str):
        from core.runtime import close_safely

        await DBOS.cancel_workflow_async(run_id)
        task = self.active.get(run_id)
        if task is not None:

            async def drain():
                await asyncio.gather(task, return_exceptions=True)

            await close_safely(drain(), timeout=25, label="workflow activity cleanup")
        self.repository.dispatch(run_id, "INTERRUPTED")

    async def shutdown(self):
        self.stopping = True
        for run_id in tuple(self.active):
            await self.cancel(run_id)
        DBOS.destroy()

    def close(self):
        if not self.thread.is_alive():
            return
        self.submit(self.shutdown()).result(40)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        if self.thread.is_alive():
            raise RuntimeError("工作流清理未在退出期限内完成")
