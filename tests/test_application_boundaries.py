import asyncio
from contextlib import asynccontextmanager, ExitStack
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from core.app.service import ApplicationService
from core.workflow.activities import discover
from core.workflow.contracts import TaskOutcome, Status
from tests.workflow_harness import ActivityHost
from tests.test_train_class import FakeTrainClassPage, FakeTrainClassContext


CLASS = "https://kc.zhixueyun.com/#/train-new/class-detail/6b5003f9-9163-4384-9240-8f3225ab373d"
COURSE = "https://kc.zhixueyun.com/#/study/course/detail/11111111-1111-4111-8111-111111111111"


class ApplicationBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_deduplicates_aliases_but_preserves_subject_sources(self):
        async def completed(host, payload):
            return TaskOutcome(payload["task_id"], Status.COMPLETED)
        with TemporaryDirectory() as tmp:
            host = ActivityHost(tmp, {"learn": completed})
            sourced = COURSE + "/6/1/22222222-2222-4222-8222-222222222222"
            await ApplicationService(host).batch([COURSE, " " + COURSE + " ", COURSE + "?x=1", sourced], "learn")
            self.assertEqual(host.calls, [COURSE, sourced])
            self.assertEqual(len(host.repository.export()["runs"]), 2)

    async def exercise_class(self, page, *, enqueue=None):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        host = ActivityHost(self.temp.name, {"discover": discover})
        @asynccontextmanager
        async def browser():
            yield None, FakeTrainClassContext(page)
        with ExitStack() as stack:
            for target, value in (
                ("core.tasks.store.repository_for", host.repository),
                ("core.tasks.store.account_id", "test-account"),
                ("core.tasks.identity.account_id", "test-account"),
            ):
                stack.enter_context(patch(target, return_value=value))
            stack.enter_context(patch("core.workflow.host._host", host))
            stack.enter_context(patch("core.browser.session.create_browser_context", browser))
            stack.enter_context(patch("core.discovery.train_class.prepare_page_after_navigation_async", new=AsyncMock()))
            stack.enter_context(patch("core.discovery.train_class.wait_for_authorization_header", new=AsyncMock(return_value="test")))
            if enqueue:
                stack.enter_context(patch("core.discovery.subject_parse.enqueue_learning_links_with_subject_expand", new=enqueue))
            result = await host.execute(CLASS, "discover")
            from core.queues.learning import read_learning_failures
            failures = read_learning_failures()
        return host, result, failures

    async def test_enrollment_failure_remains_blocked_and_closes_page(self):
        page = FakeTrainClassPage()
        page.context.request.registration_status = 1
        enqueue = AsyncMock()
        host, result, failures = await self.exercise_class(page, enqueue=enqueue)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("enrollment_blocked", [f.reason for f in failures])
        enqueue.assert_not_awaited()
        self.assertTrue(page.closed)
        self.assertEqual(host.repository.export()["queue_items"][0]["collection"], "挂课失败链接.json")

    async def test_cancelled_enrollment_disposes_response_and_closes_page(self):
        page = FakeTrainClassPage()
        page.context.request.registration_status = 0
        response = SimpleNamespace(ok=True, json=AsyncMock(side_effect=asyncio.CancelledError()), dispose=AsyncMock())
        page.context.request.post = AsyncMock(return_value=response)
        with self.assertRaises(asyncio.CancelledError):
            await self.exercise_class(page)
        response.dispose.assert_awaited_once()
        self.assertTrue(page.closed)

    async def test_class_activity_paginates_all_items_before_enqueue(self):
        page = FakeTrainClassPage()
        enqueue = AsyncMock(return_value={"complete": True, "learning_added": 13})
        _, result, _ = await self.exercise_class(page, enqueue=enqueue)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(len(enqueue.call_args.args[0]), 13)
        self.assertTrue(page.closed)
