import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from core.abort import UserCancelRequested
from core.exam import runner
from core.learning import flows


class SharedCourseEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_rating_is_checked_first_with_or_without_a_popup(self):
        for present in (True, False):
            with self.subTest(present=present):
                calls = []
                async def loaded(*args):
                    calls.append("load")
                async def rating(*args):
                    calls.append("rating")
                    return present
                async def archive(*args):
                    calls.append("archive")
                async def ready(*args):
                    calls.append("ready")
                async def view(*args):
                    calls.append("view")
                page = SimpleNamespace(wait_for_load_state=loaded)
                with patch.object(flows, "handle_rating_popup", new=rating), patch.object(flows, "handle_archive_continue_popup", new=archive), patch.object(flows, "ensure_course_page_ready", new=ready), patch.object(flows, "_ensure_course_learning_view", new=view):
                    await flows.prepare_course_page(page)
                self.assertEqual(calls, ["load", "rating", "archive", "ready", "view"])

    async def test_learning_calls_shared_preparation(self):
        page = SimpleNamespace(url="course", locator=lambda _: SimpleNamespace(inner_text=AsyncMock(return_value="title")))
        with patch.object(flows, "prepare_course_page", new=AsyncMock()) as prepare, patch.object(flows, "_is_course_completed", new=AsyncMock(return_value=True)):
            self.assertTrue(await flows.course_learning(page))
        prepare.assert_awaited_once_with(page)


class ExamCourseEntryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.target = SimpleNamespace(click=AsyncMock())
        self.sections = SimpleNamespace(first=SimpleNamespace(wait_for=AsyncMock()), count=AsyncMock(return_value=1), locator=lambda _: self.target)
        self.all_sections = SimpleNamespace(first=SimpleNamespace(wait_for=AsyncMock()))
        self.panel = SimpleNamespace(wait_for=AsyncMock())
        def locator(selector):
            return {"dl.chapter-list-box": self.all_sections,
                    'dl.chapter-list-box[data-sectiontype="9"]': self.sections,
                    ".tab-container": self.panel}[selector]
        self.page = SimpleNamespace(locator=locator, wait_for_timeout=AsyncMock())
        self.prepare = self.enterContext(patch.object(flows, "prepare_course_page", new=AsyncMock()))
        self.rating = self.enterContext(patch.object(runner, "handle_rating_popup", new=AsyncMock(return_value=False)))

    async def test_exam_uses_shared_preparation_and_exact_section_without_header_click(self):
        await runner._open_course_exam_tab(self.page)
        self.prepare.assert_awaited_once_with(self.page)
        self.target.click.assert_awaited_once_with(timeout=5000)
        self.panel.wait_for.assert_awaited_once()

    async def test_late_rating_popup_is_handled_before_retrying_same_section(self):
        self.target.click.side_effect = [PlaywrightTimeoutError("rating blocks pointer"), None]
        self.rating.side_effect = [False, True]
        await runner._open_course_exam_tab(self.page)
        self.assertEqual(self.target.click.await_count, 2)

    async def test_other_click_timeouts_are_not_forced_through_modal(self):
        self.target.click.side_effect = PlaywrightTimeoutError("other popup")
        with self.assertRaises(PlaywrightTimeoutError):
            await runner._open_course_exam_tab(self.page)
        self.assertEqual(self.target.click.await_count, 1)
        self.panel.wait_for.assert_not_awaited()

    async def test_multiple_exam_sections_preserve_queue_for_explicit_selection(self):
        self.sections.count.return_value = 2
        with self.assertRaises(UserCancelRequested):
            await runner._open_course_exam_tab(self.page)
        self.target.click.assert_not_awaited()
