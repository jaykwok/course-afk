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

    async def click(self):
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
        from core.learning_handlers import handle_video

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
            patch("core.learning_handlers.timer", new=never_finishing_timer),
            patch(
                "core.learning_handlers.check_rating_popup_periodically",
                new=never_finishing_popup_check,
            ),
            patch(
                "core.learning_handlers.check_and_handle_rating_popup",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "core.learning_handlers.asyncio.create_task",
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

        self.assertEqual(len(created_tasks), 2)
        self.assertEqual(task_states_before_cleanup, [True, True])


if __name__ == "__main__":
    unittest.main()
