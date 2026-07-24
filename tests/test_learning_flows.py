import asyncio
import unittest
from unittest.mock import AsyncMock, patch


class SubjectLearningFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_subject_learning_skips_closed_popup_course_and_continues(self):
        from core.learning.flows import subject_learning

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

            async def wait_for(self, *args, **kwargs):
                return None

            async def scroll_into_view_if_needed(self, *args, **kwargs):
                return None

            async def count(self):
                return self._count_value

            async def inner_text(self):
                return self._inner_text_value

            async def click(self, *args, **kwargs):
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

            async def wait_for_timeout(self, _ms):
                return None

            def locator(self, selector):
                if selector == ".item.current-hover":
                    return FakeCountLocator(self._items)
                raise AssertionError(f"unexpected selector: {selector}")

            def expect_popup(self, **kwargs):
                popup_page = self._popup_pages.pop(0)
                return FakePopupContextManager(popup_page)

        fake_context = type("FakeContext", (), {"browser": FakeBrowser()})()
        popup_pages = [FakePopupPage(fake_context), FakePopupPage(fake_context)]
        subject_page = FakeSubjectPage(popup_pages)

        with (
            patch(
                "core.learning.flows.ensure_course_page_ready",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "core.learning.flows.dismiss_topmost_overlays_async",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "core.learning.flows.course_learning",
                new=AsyncMock(
                    side_effect=[
                        TargetClosedError("Target page, context or browser has been closed"),
                        None,
                    ]
                ),
            ) as mock_course_learning,
            patch("core.learning.flows.record_learning_failure") as mock_record_failure,
        ):
            # 单课标签关闭：跳过该课并继续后续小节，结束时汇总为部分失败
            with self.assertRaisesRegex(Exception, "部分主题课程学习失败"):
                await subject_learning(subject_page)

        self.assertEqual(mock_course_learning.await_count, 2)
        self.assertFalse(mock_record_failure.called)
        self.assertTrue(all(page.closed for page in popup_pages))

    async def test_subject_learning_continues_after_retryable_course_failure(self):
        """主题内单课可恢复失败应记失败并继续后续小节，结束时再统一抛错。"""
        from core.abort import NoPermissionError
        from core.learning.flows import subject_learning

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

            async def wait_for(self, *args, **kwargs):
                return None

            async def scroll_into_view_if_needed(self, *args, **kwargs):
                return None

            async def count(self):
                return self._count_value

            async def inner_text(self):
                return self._inner_text_value

            async def click(self, *args, **kwargs):
                return None

            async def get_attribute(self, name):
                if name == "data-resource-id":
                    return "course-resource-id"
                raise AssertionError(f"unexpected attribute: {name}")

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

            async def get_attribute(self, name):
                if name == "data-resource-id":
                    return "course-resource-id"
                raise AssertionError(f"unexpected attribute: {name}")

        class FakeSubjectPage:
            def __init__(self, popup_pages):
                self.main_frame = object()
                self.url = "https://kc.zhixueyun.com/#/study/subject/detail/test-subject"
                self._items = [FakeLearnItem(), FakeLearnItem()]
                self._popup_pages = list(popup_pages)

            async def wait_for_load_state(self, _state):
                return None

            async def wait_for_timeout(self, _ms):
                return None

            def locator(self, selector):
                if selector == ".item.current-hover":
                    return FakeCountLocator(self._items)
                raise AssertionError(f"unexpected selector: {selector}")

            def expect_popup(self, **kwargs):
                popup_page = self._popup_pages.pop(0)
                return FakePopupContextManager(popup_page)

        fake_context = type("FakeContext", (), {"browser": FakeBrowser()})()
        popup_pages = [FakePopupPage(fake_context), FakePopupPage(fake_context)]
        subject_page = FakeSubjectPage(popup_pages)

        with (
            patch(
                "core.learning.flows.ensure_course_page_ready",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "core.learning.flows.dismiss_topmost_overlays_async",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "core.learning.flows.course_learning",
                new=AsyncMock(
                    side_effect=[
                        RuntimeError("sync timeout"),
                        None,
                    ]
                ),
            ) as mock_course_learning,
            patch("core.learning.flows.record_learning_failure") as mock_record_failure,
            patch(
                "core.learning.flows.get_course_url",
                new=AsyncMock(return_value="https://course.example/1"),
            ),
        ):
            with self.assertRaisesRegex(Exception, "部分主题课程学习失败"):
                await subject_learning(subject_page)

        self.assertEqual(mock_course_learning.await_count, 2)
        mock_record_failure.assert_called_once()
        self.assertTrue(all(page.closed for page in popup_pages))

        # 无权限失败不计入 has_failed_course 的「统一抛错」？当前实现：无权限只记失败不 raise 汇总
        # 上面是 retryable，应汇总抛错；无权限路径单独覆盖：
        popup_pages2 = [FakePopupPage(fake_context), FakePopupPage(fake_context)]
        subject_page2 = FakeSubjectPage(popup_pages2)
        with (
            patch(
                "core.learning.flows.ensure_course_page_ready",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "core.learning.flows.dismiss_topmost_overlays_async",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "core.learning.flows.course_learning",
                new=AsyncMock(
                    side_effect=[
                        NoPermissionError("无权限"),
                        None,
                    ]
                ),
            ) as mock_course_learning2,
            patch("core.learning.flows.record_learning_failure") as mock_record_failure2,
            patch(
                "core.learning.flows.get_course_url",
                new=AsyncMock(return_value="https://course.example/2"),
            ),
        ):
            await subject_learning(subject_page2)

        self.assertEqual(mock_course_learning2.await_count, 2)
        mock_record_failure2.assert_called_once()
        fail_kwargs = mock_record_failure2.call_args.kwargs
        self.assertEqual(fail_kwargs.get("reason"), "no_permission")


class SubjectItemCompletedTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_by_reload_icon(self):
        from core.learning.flows import is_subject_item_completed

        class Loc:
            def __init__(self, *, count_value=0, texts=None, inner=""):
                self._count = count_value
                self._texts = texts or []
                self._inner = inner

            async def count(self):
                return self._count

            async def all_inner_texts(self):
                return list(self._texts)

            async def inner_text(self):
                return self._inner

        class Item:
            def locator(self, selector):
                if selector == ".iconfont.m-right.icon-reload":
                    return Loc(count_value=1)
                if selector == "span.finished-status":
                    return Loc(texts=[])
                if selector == ".inline-block.operation":
                    return Loc(count_value=1, inner="重新学习")
                raise AssertionError(selector)

        self.assertTrue(await is_subject_item_completed(Item()))

    async def test_completed_exam_by_record_text_without_reload(self):
        from core.learning.flows import is_subject_item_completed

        class Loc:
            def __init__(self, *, count_value=0, texts=None, inner=""):
                self._count = count_value
                self._texts = texts or []
                self._inner = inner

            async def count(self):
                return self._count

            async def all_inner_texts(self):
                return list(self._texts)

            async def inner_text(self):
                return self._inner

        class Item:
            def locator(self, selector):
                if selector == ".iconfont.m-right.icon-reload":
                    return Loc(count_value=0)
                if selector == "span.finished-status":
                    return Loc(texts=["成绩：80", "已完成"])
                if selector == ".inline-block.operation":
                    return Loc(count_value=1, inner="考试记录")
                raise AssertionError(selector)

        self.assertTrue(await is_subject_item_completed(Item()))

    async def test_incomplete_start_learning(self):
        from core.learning.flows import is_subject_item_completed

        class Loc:
            def __init__(self, *, count_value=0, texts=None, inner=""):
                self._count = count_value
                self._texts = texts or []
                self._inner = inner

            async def count(self):
                return self._count

            async def all_inner_texts(self):
                return list(self._texts)

            async def inner_text(self):
                return self._inner

        class Item:
            def locator(self, selector):
                if selector == ".iconfont.m-right.icon-reload":
                    return Loc(count_value=0)
                if selector == "span.finished-status":
                    return Loc(texts=[])
                if selector == ".inline-block.operation":
                    return Loc(count_value=1, inner="开始学习")
                raise AssertionError(selector)

        self.assertFalse(await is_subject_item_completed(Item()))


if __name__ == "__main__":
    unittest.main()
