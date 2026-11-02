"""Activity/application boundary harness; DBOS itself is tested in subprocesses."""

import asyncio
import json
from pathlib import Path

from core.tasks.identity import parse_entry
from core.tasks.repository import TaskRepository
from core.workflow.host import WorkflowHost, VERSION


class ActivityHost:
    authorize = WorkflowHost.authorize
    perform = WorkflowHost.perform

    def __init__(self, root, activities, account="test-account"):
        self.repository = TaskRepository(Path(root) / "state.sqlite")
        self.loop = asyncio.get_running_loop()
        self.activities = activities
        self.identity_provider = lambda: account
        self.scopes = {}
        self.active = {}
        self.stopping = False
        self.status_callback = None
        self.calls = []

    async def execute(self, url, kind, *, options=None, status_callback=None):
        self.calls.append(url)
        task_id = self.repository.upsert(parse_entry(url, self.identity_provider()))
        run = self.repository.plan(task_id, kind, options or {}, VERSION)
        payload = json.loads(run["payload"])
        self.scopes[run["run_id"]] = payload
        self.status_callback = status_callback
        try:
            result = await self.perform(payload)
            return self.repository.save_outcome(run["run_id"], result, finalized=True)
        finally:
            self.scopes.pop(run["run_id"], None)


async def new_learning_batch(test, name):
    from tempfile import TemporaryDirectory
    from unittest.mock import patch
    from core.learning.afk_runner import run_afk_once
    from core.workflow.contracts import TaskOutcome, Status
    from core.abort import UserCancelRequested, UserAbortRequested, WafBlockError

    urls = [
        "https://kc.zhixueyun.com/#/study/course/detail/"
        + f"{i:08d}-0000-0000-0000-000000000000"
        for i in (1, 2, 3)
    ]
    with TemporaryDirectory() as tmp:

        async def activity(host, payload):
            index = urls.index(host.repository.task(payload["task_id"])["url"])
            if "heartbeat" in name and index == 0:
                return TaskOutcome(
                    payload["task_id"], Status.COMPLETED, stop_scope="batch"
                )
            if "waf" in name:
                raise WafBlockError()
            if "failed_url" in name and index == 0:
                return TaskOutcome(
                    payload["task_id"], Status.FAILED, ["retryable_error"]
                )
            if "only_course_tab" in name and index == 0:
                return TaskOutcome(
                    payload["task_id"], Status.INTERRUPTED, ["course_page_closed"]
                )
            if "keyboard_interrupt" in name and index == 1:
                raise KeyboardInterrupt
            if "abort_with_save" in name and index == 1:
                raise UserAbortRequested("stop")
            if any(
                word in name
                for word in ("browser_window", "during_setup", "cancelled_error")
            ):
                raise UserCancelRequested("closed")
            return TaskOutcome(
                payload["task_id"], Status.COMPLETED, evidence_refs=["synthetic_server"]
            )

        host = ActivityHost(tmp, {"learn": activity})
        for url in urls:
            task_id = host.repository.upsert(parse_entry(url, "test-account"))
            with host.repository.connect() as db:
                db.execute(
                    "INSERT INTO queue_items VALUES(?,?,?,?,?)",
                    ("课程链接.json", task_id, "queue", json.dumps({"url": url}), 0),
                )
        interrupted = any(
            word in name
            for word in (
                "heartbeat",
                "waf",
                "keyboard_interrupt",
                "abort_with_save",
                "browser_window",
                "during_setup",
                "cancelled_error",
            )
        )
        with (
            patch("core.workflow.host.get_host", return_value=host),
            patch("core.queues.learning.read_learning_urls", return_value=urls),
            patch("core.queues.learning.read_learning_failures", return_value=[]),
        ):
            if interrupted:
                with test.assertRaises(
                    (UserCancelRequested, UserAbortRequested, KeyboardInterrupt)
                ):
                    await run_afk_once()
            else:
                await run_afk_once()
        rows = host.repository.export()["queue_items"]
        if "heartbeat" in name:
            test.assertEqual(len(host.calls), 1)
            test.assertEqual(len(rows), 2)
        elif "keyboard_interrupt" in name or "abort_with_save" in name:
            test.assertEqual(len(rows), 2)
        elif interrupted:
            test.assertEqual(len(rows), 3)
            test.assertEqual(len(host.calls), 1)
        elif "failed_url" in name or "only_course_tab" in name:
            test.assertEqual(len(rows), 1)
        else:
            test.assertEqual(len(rows), 0)
            test.assertEqual(len(host.calls), 3)


async def new_selection(test, name):
    from contextlib import asynccontextmanager, ExitStack
    from unittest.mock import patch, AsyncMock
    from core.config import DATA_DIR
    from core.app.workflows import run_manual_course_selection
    from core.workflow.activities import discover
    from core.queues.learning import (
        write_learning_urls,
        append_learning_urls,
        write_learning_failures,
    )
    from core.queues.exam import write_exam_urls
    from core.config import (
        ZHIXUEYUN_COURSE_PREFIX,
        ZHIXUEYUN_SUBJECT_PREFIX,
        ZHIXUEYUN_TRAIN_CLASS_PREFIX,
    )

    uid = "11111111-1111-4111-8111-111111111111"
    course = ZHIXUEYUN_COURSE_PREFIX + uid
    subject = ZHIXUEYUN_SUBJECT_PREFIX + uid
    training = ZHIXUEYUN_TRAIN_CLASS_PREFIX + uid
    zone = "https://www.mylearning.cn/topic/offline"
    contexts = []
    closed = []

    @asynccontextmanager
    async def browser(**kwargs):
        context = AsyncMock()
        contexts.append(context)
        try:
            yield None, context
        finally:
            closed.append(context)

    async def subject_adapter(*args, **kwargs):
        append_learning_urls([course])
        return {"learning_added": 1, "exam_added": 0}

    async def zone_adapter(*args, **kwargs):
        append_learning_urls([course])
        return 1

    with ExitStack() as stack:
        stack.enter_context(
            patch("core.tasks.store.account_id", return_value="test-account")
        )
        write_learning_urls([])
        write_exam_urls([])
        write_learning_failures([])
        host = ActivityHost(DATA_DIR / "workflows", {"discover": discover})
        stack.enter_context(patch("core.workflow.host.get_host", return_value=host))
        stack.enter_context(patch("core.workflow.host._host", host))
        stack.enter_context(
            patch("core.browser.session.create_browser_context", browser)
        )
        subject_call = stack.enter_context(
            patch(
                "core.discovery.subject_parse.expand_and_append_subject_urls",
                new=AsyncMock(side_effect=subject_adapter),
            )
        )
        zone_call = stack.enter_context(
            patch(
                "core.learning.zone.collect_learning_links_from_learning_zone_urls",
                new=AsyncMock(side_effect=zone_adapter),
            )
        )
        class_call = stack.enter_context(
            patch(
                "core.discovery.train_class.collect_learning_links_from_class_page",
                new=AsyncMock(return_value=[course]),
            )
        )
        manual_call = stack.enter_context(
            patch(
                "core.app.workflows.collect_learning_links_from_entry_urls",
                new=AsyncMock(return_value=(0, 0, 0)),
            )
        )
        if "across_jobs" in name or "entry_phase" in name:
            inputs = [subject, zone, training]
        elif "train_class" in name:
            inputs = [training]
        elif "subject" in name:
            inputs = [subject]
        else:
            inputs = [zone]
        mode = "manual" if "manual_mode" in name else "auto"
        result = await run_manual_course_selection(
            "\n".join(inputs), learning_zone_mode=mode
        )
        test.assertEqual(result["input_url_count"], len(inputs))
        test.assertEqual(len(host.calls), len(inputs))
        test.assertEqual(len(contexts), len(inputs))
        test.assertEqual(len(closed), len(contexts))
        if subject in inputs:
            subject_call.assert_awaited_once()
        if training in inputs:
            class_call.assert_awaited_once()
        if zone in inputs and mode == "auto":
            zone_call.assert_awaited_once()
        if mode == "manual":
            manual_call.assert_awaited_once()
            zone_call.assert_not_awaited()
