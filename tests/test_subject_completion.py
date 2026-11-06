"""Subject completion uses course identities resolved after popup preparation."""

from contextlib import asynccontextmanager
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from core.abort import NoPermissionError
from core.queues.learning import append_learning_urls, read_learning_urls
from core.tasks.identity import parse_entry
from core.workflow.activities import learn
from tests.workflow_harness import ActivityHost


ACCOUNT = "subject-completion-test"
COURSE_ID = "11111111-1111-4111-8111-111111111111"
SUBJECT_ID = "22222222-2222-4222-8222-222222222222"
COURSE = "https://kc.zhixueyun.com/#/study/course/detail/" + COURSE_ID
SUBJECT = "https://kc.zhixueyun.com/#/study/subject/detail/" + SUBJECT_ID
TEMPORARY = "https://kc.zhixueyun.com/#/study/course/detail/detailInfoLoading"


class SubjectCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.host = ActivityHost(
            Path(temporary.name), {"learn": learn}, account=ACCOUNT
        )
        self.repo = self.host.repository
        for target, value in (
            ("core.tasks.store.account_id", ACCOUNT),
            ("core.tasks.identity.account_id", ACCOUNT),
            ("core.tasks.store.repository_for", self.repo),
        ):
            self.enterContext(patch(target, return_value=value))
        self.enterContext(patch("core.workflow.host._host", self.host))

    def subject_page(self, detail):
        page = MagicMock()
        page.url = SUBJECT
        page.goto = AsyncMock()
        page.wait_for_load_state = AsyncMock()
        page.close = AsyncMock()
        page.is_closed.return_value = False
        items = MagicMock()
        items.count = AsyncMock(return_value=1)
        items.last.wait_for = AsyncMock()
        items.locator.return_value.last.wait_for = AsyncMock()
        items.nth.return_value.locator.return_value.inner_text = AsyncMock(
            return_value="课程"
        )
        page.locator.return_value = items

        @asynccontextmanager
        async def browser():
            yield None, object()

        self.enterContext(
            patch("core.browser.session.create_browser_context", browser)
        )
        self.enterContext(
            patch(
                "core.learning.afk_runner._open_course_page",
                new=AsyncMock(return_value=page),
            )
        )
        self.enterContext(
            patch(
                "core.learning.afk_runner._wait_for_course_bootstrap", new=AsyncMock()
            )
        )
        for module in ("core.learning.afk_runner", "core.learning.flows"):
            self.enterContext(
                patch(module + ".ensure_course_page_ready", new=AsyncMock())
            )
        self.enterContext(
            patch(
                "core.learning.flows._open_subject_item_popup",
                new=AsyncMock(return_value=detail),
            )
        )

    def completed_document(self, prepare):
        box = SimpleNamespace(
            get_attribute=AsyncMock(
                side_effect=lambda name: (
                    "3"
                    if name == "data-sectiontype"
                    else "D201show-sections" + COURSE_ID
                )
            ),
            locator=lambda _: SimpleNamespace(inner_text=AsyncMock(return_value="文档")),
        )
        self.enterContext(
            patch(
                "core.learning.flows.prepare_course_page",
                new=AsyncMock(side_effect=prepare),
            )
        )
        self.enterContext(
            patch(
                "core.learning.flows._collect_chapter_boxes",
                new=AsyncMock(return_value=[("required", box)]),
            )
        )
        self.enterContext(
            patch(
                "core.learning.flows.read_server_section_states",
                new=AsyncMock(return_value={COURSE_ID: {"finish_status": 2}}),
            )
        )
        self.enterContext(
            patch(
                "core.learning.flows.queue_course_exams_from_api",
                new=AsyncMock(return_value=None),
            )
        )

    async def run_subject(self):
        append_learning_urls([SUBJECT])
        return await self.host.execute(SUBJECT, "learn")

    def assert_course_and_subject_complete(self, result, course_url):
        self.assertEqual(result["status"], "COMPLETED")
        children = self.repo.children(result["task_id"])
        self.assertEqual(
            [child["task_id"] for child in children],
            [parse_entry(course_url, ACCOUNT).key.task_id],
        )
        self.assertEqual(children[0]["status"], "COMPLETED")
        self.assertEqual(children[0]["url"], course_url)
        sections = self.repo.children(children[0]["task_id"])
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0]["status"], "COMPLETED")
        self.assertEqual(read_learning_urls(), [])
        with self.repo.connect() as db:
            local = db.execute(
                "SELECT payload FROM task_local_outcomes WHERE task_id=?",
                (children[0]["task_id"],),
            ).fetchone()
        self.assertTrue(json.loads(local[0])["details"]["local_work_complete"])

    async def test_navigation_resolves_course_after_popup_opens(self):
        detail = SimpleNamespace(
            url=TEMPORARY, close=AsyncMock(), evaluate=AsyncMock(return_value=None)
        )
        self.subject_page(detail)

        async def prepare(page):
            page.url = COURSE

        self.completed_document(prepare)
        result = await self.run_subject()
        self.assert_course_and_subject_complete(result, COURSE)
        detail.close.assert_awaited_once()

    async def test_temporary_route_parameters_resolve_to_same_course_as_progress(self):
        detail = SimpleNamespace(
            url=TEMPORARY, close=AsyncMock(), evaluate=AsyncMock(return_value=None)
        )
        self.subject_page(detail)

        async def prepare(page):
            # The SPA keeps its temporary URL; only its parameters become ready.
            page.evaluate.return_value = {
                "id": COURSE_ID, "type": 6, "sourceId": SUBJECT_ID, "required": 1
            }

        self.completed_document(prepare)
        result = await self.run_subject()
        self.assert_course_and_subject_complete(result, COURSE + "/6/1/" + SUBJECT_ID)
        detail.close.assert_awaited_once()

    async def test_unresolved_identity_retains_subject_for_retry(self):
        detail = SimpleNamespace(
            url=TEMPORARY, close=AsyncMock(), evaluate=AsyncMock(return_value=None)
        )
        self.subject_page(detail)
        self.completed_document(None)
        result = await self.run_subject()
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("partial_course_failure", result["reason_codes"])
        self.assertEqual(read_learning_urls(), [SUBJECT])
        detail.close.assert_awaited_once()

    async def test_permission_failure_keeps_dependency_created_before_learning(self):
        detail = SimpleNamespace(url=COURSE, close=AsyncMock())
        self.subject_page(detail)

        async def denied(*args):
            children = self.repo.children(parse_entry(SUBJECT, ACCOUNT).key.task_id)
            self.assertEqual([child["url"] for child in children], [COURSE])
            raise NoPermissionError()

        self.enterContext(
            patch("core.learning.flows.course_learning", new=AsyncMock(side_effect=denied))
        )
        result = await self.run_subject()
        self.assertEqual(result["status"], "WAITING_CHILDREN")
        self.assertEqual(
            self.repo.task(parse_entry(COURSE, ACCOUNT).key.task_id)["status"], "BLOCKED"
        )
        detail.close.assert_awaited_once()
