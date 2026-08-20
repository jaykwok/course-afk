import unittest
from unittest.mock import AsyncMock, patch

from core.learning.flows import _ensure_course_learning_view, course_learning


class _FakeTab:
    def __init__(self, *, visible=True, selected=False, click_error=None):
        self.visible = visible
        self.selected = selected
        self.click_error = click_error
        self.click_calls = []

    async def is_visible(self):
        return self.visible

    async def scroll_into_view_if_needed(self, **_kwargs):
        return None

    async def get_attribute(self, name):
        if name != "class":
            raise AssertionError(name)
        return "guide-tab--selected" if self.selected else ""

    async def click(self, **kwargs):
        self.click_calls.append(kwargs)
        if self.click_error is not None:
            raise self.click_error


class _FakeTabList:
    def __init__(self, tabs):
        self.tabs = list(tabs)

    async def count(self):
        return len(self.tabs)

    def filter(self, *, has_text):
        if has_text != "课程学习":
            raise AssertionError(has_text)
        return self

    def nth(self, index):
        return self.tabs[index]


class _FakePage:
    def __init__(self, tabs):
        self.tabs = _FakeTabList(tabs)
        self.waits = []
        self.url = "https://kc.zhixueyun.com/#/study/course/detail/test"

    def locator(self, selector):
        if selector != ".guide-tab > span":
            raise AssertionError(selector)
        return self.tabs

    async def wait_for_timeout(self, timeout):
        self.waits.append(timeout)


class CourseLearningViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_switches_the_visible_course_learning_tab(self):
        hidden_tab = _FakeTab(visible=False)
        visible_tab = _FakeTab()
        page = _FakePage([hidden_tab, visible_tab])

        switched = await _ensure_course_learning_view(page)

        self.assertTrue(switched)
        self.assertEqual(hidden_tab.click_calls, [])
        self.assertEqual(visible_tab.click_calls, [{"timeout": 5000}])
        self.assertEqual(page.waits, [800])

    async def test_old_page_without_view_tabs_keeps_existing_flow(self):
        page = _FakePage([])

        switched = await _ensure_course_learning_view(page)

        self.assertFalse(switched)
        self.assertEqual(page.waits, [])

    async def test_does_not_click_when_course_learning_is_already_selected(self):
        selected_tab = _FakeTab(selected=True)
        page = _FakePage([selected_tab])

        switched = await _ensure_course_learning_view(page)

        self.assertFalse(switched)
        self.assertEqual(selected_tab.click_calls, [])
        self.assertEqual(page.waits, [])

    async def test_course_learning_switches_view_before_completion_check(self):
        events = []
        page = _FakePage([_FakeTab()])

        async def ensure_ready(_page):
            events.append("ready")

        async def ensure_view(_page):
            events.append("course-view")
            return True

        async def completed(_page):
            events.append("completed-check")
            return True

        with (
            patch(
                "core.learning.flows.handle_archive_continue_popup",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "core.learning.flows.ensure_course_page_ready",
                new=ensure_ready,
            ),
            patch(
                "core.learning.flows._ensure_course_learning_view",
                new=ensure_view,
            ),
            patch(
                "core.learning.flows.handle_rating_popup",
                new=AsyncMock(return_value=False),
            ),
            patch("core.learning.flows._is_course_completed", new=completed),
        ):
            page.wait_for_load_state = AsyncMock(return_value=None)
            title = AsyncMock()
            title.inner_text = AsyncMock(return_value="测试课程")
            page.locator = lambda selector: title
            await course_learning(page)

        self.assertEqual(events, ["ready", "course-view", "completed-check"])


if __name__ == "__main__":
    unittest.main()
