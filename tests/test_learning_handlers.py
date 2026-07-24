import asyncio
import unittest
from unittest.mock import AsyncMock, patch


class _FakeLocator:
    def __init__(self, *, inner_text_value="", all_values=None):
        self._inner_text_value = inner_text_value
        self._all_values = all_values or []

    @property
    def first(self):
        return self

    async def all(self):
        return self._all_values

    async def wait_for(self, *args, **kwargs):
        return None

    async def inner_text(self):
        return self._inner_text_value

    async def click(self, *args, **kwargs):
        return None


class _FakeBox:
    def locator(self, _selector):
        return _FakeLocator(inner_text_value="总时长 01:00 剩余 00:31")


class _FakePage:
    def __init__(self, error_type):
        self._error_type = error_type

    def locator(self, selector):
        if selector == ".register-mask-layer":
            return _FakeLocator(all_values=[])
        return _FakeLocator()

    async def wait_for_timeout(self, _milliseconds):
        raise self._error_type("Target page, context or browser has been closed")


class LearningHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_video_cleans_up_background_tasks_when_page_closes(self):
        from core.learning.handlers import handle_video

        class TargetClosedError(Exception):
            pass

        created_tasks = []
        original_create_task = asyncio.create_task

        async def never_finishing_timer(*args, **kwargs):
            await asyncio.Future()

        async def never_finishing_popup_check(*args, **kwargs):
            await asyncio.Future()

        def tracking_create_task(coro):
            task = original_create_task(coro)
            created_tasks.append(task)
            return task

        with (
            patch("core.learning.handlers.timer", new=never_finishing_timer),
            patch(
                "core.learning.handlers.check_rating_popup_periodically",
                new=never_finishing_popup_check,
            ),
            patch(
                "core.learning.handlers.check_and_handle_rating_popup",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "core.browser.overlays.dismiss_topmost_overlays_async",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "core.learning.handlers.asyncio.create_task",
                side_effect=tracking_create_task,
            ),
        ):
            with self.assertRaises(TargetClosedError):
                await handle_video(_FakeBox(), _FakePage(TargetClosedError))
            await asyncio.sleep(0)
            task_states_before_cleanup = [task.done() for task in created_tasks]

        for task in created_tasks:
            if not task.done():
                task.cancel()
        if created_tasks:
            await asyncio.gather(*created_tasks, return_exceptions=True)

        # timeout / timer / popup 三个并行任务；页面关闭后均应被清理
        self.assertEqual(len(created_tasks), 3)
        self.assertEqual(task_states_before_cleanup, [True, True, True])

    async def test_handle_document_leaves_quietly_when_sync_times_out(self):
        """文档到点未同步：不抛错、不记失败，直接走人。"""
        from core.abort import SyncTimeoutError
        from core.learning.handlers import handle_document

        class DocBox:
            def locator(self, selector):
                return _FakeLocator(inner_text_value="必修 文档 05:00\n需学 00:05")

        class DocPage:
            def locator(self, selector):
                return _FakeLocator()

            async def wait_for_timeout(self, _ms):
                return None

        with (
            patch(
                "core.learning.handlers.wait_until_learned",
                new=AsyncMock(
                    side_effect=SyncTimeoutError(
                        "课程进度未能在 60 秒内同步完成",
                        reason_text="timeout",
                    )
                ),
            ) as mock_wait,
            patch(
                "core.browser.overlays.dismiss_topmost_overlays_async",
                new=AsyncMock(return_value=0),
            ),
        ):
            # 不得向外抛 SyncTimeoutError
            await handle_document(DocPage(), DocBox())

        mock_wait.assert_awaited_once()
        kwargs = mock_wait.await_args.kwargs
        self.assertEqual(kwargs.get("max_wait"), 60)

    async def test_handle_document_returns_early_when_synced(self):
        from core.learning.handlers import handle_document

        with (
            patch(
                "core.learning.handlers.wait_until_learned",
                new=AsyncMock(return_value=None),
            ) as mock_wait,
            patch(
                "core.browser.overlays.dismiss_topmost_overlays_async",
                new=AsyncMock(return_value=0),
            ),
        ):
            await handle_document(_FakePage(RuntimeError), _FakeBox())

        mock_wait.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
