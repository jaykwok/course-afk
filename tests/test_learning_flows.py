import asyncio
import unittest
from unittest.mock import AsyncMock, patch


class SubjectLearningFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_subject_learning_skips_closed_popup_course_and_continues(self):
        from core.learning_flows import subject_learning

        class TargetClosedError(Exception):
            pass

        class FakeBrowser:
            def is_connected(self):
                return True

        class FakePopupPage:
            def __init__(self, context):
                self.context = context
                self.closed = False

            async def close(self):
                self.closed = True

        class FakePopupInfo:
            def __init__(self, popup_page):
                self.value = asyncio.Future()
                self.value.set_result(popup_page)

        class FakePopupContextManager:
            def __init__(self, popup_page):
                self._popup_page = popup_page

            async def __aenter__(self):
                return FakePopupInfo(self._popup_page)

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class FakeCountLocator:
            def __init__(self, items):
                self._items = items

            @property
            def last(self):
                return self

            async def wait_for(self):
                return None

            async def count(self):
                return len(self._items)

            def locator(self, _selector):
                return self

            def nth(self, index):
                return self._items[index]

        class FakeStaticLocator:
            def __init__(self, *, count_value=0, inner_text_value=""):
                self._count_value = count_value
                self._inner_text_value = inner_text_value

            @property
            def last(self):
                return self

            async def wait_for(self):
                return None

            async def count(self):
                return self._count_value

            async def inner_text(self):
                return self._inner_text_value

            async def click(self):
                return None

        class FakeLearnItem:
            def __init__(self):
                self.operation_locator = FakeStaticLocator()

            def locator(self, selector):
                if selector == ".iconfont.m-right.icon-reload":
                    return FakeStaticLocator(count_value=0)
                if selector == ".section-type":
                    return FakeStaticLocator(inner_text_value="课程")
                if selector == ".inline-block.operation":
                    return self.operation_locator
                raise AssertionError(f"unexpected selector: {selector}")

        class FakeSubjectPage:
            def __init__(self, popup_pages):
                self.main_frame = object()
                self.url = "https://kc.zhixueyun.com/#/study/subject/detail/test-subject"
                self._items = [FakeLearnItem(), FakeLearnItem()]
                self._popup_pages = list(popup_pages)

            async def wait_for_load_state(self, _state):
                return None

            def locator(self, selector):
                if selector == ".item.current-hover":
                    return FakeCountLocator(self._items)
                raise AssertionError(f"unexpected selector: {selector}")

            def expect_popup(self):
                popup_page = self._popup_pages.pop(0)
                return FakePopupContextManager(popup_page)

        fake_context = type("FakeContext", (), {"browser": FakeBrowser()})()
        popup_pages = [FakePopupPage(fake_context), FakePopupPage(fake_context)]
        subject_page = FakeSubjectPage(popup_pages)

        with (
            patch("core.learning_flows.check_permission", new=AsyncMock(return_value=True)),
            patch(
                "core.learning_flows.course_learning",
                new=AsyncMock(
                    side_effect=[
                        TargetClosedError("Target page, context or browser has been closed"),
                        None,
                    ]
                ),
            ) as mock_course_learning,
            patch("core.learning_flows.record_learning_failure") as mock_record_failure,
        ):
            await subject_learning(subject_page)

        self.assertEqual(mock_course_learning.await_count, 2)
        self.assertFalse(mock_record_failure.called)
        self.assertTrue(all(page.closed for page in popup_pages))


if __name__ == "__main__":
    unittest.main()
