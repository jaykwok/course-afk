import asyncio
import time
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
            patch(
                "core.learning.handlers._ensure_video_player_ready",
                new=AsyncMock(return_value=None),
            ),
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
            # watch / timer / popup 三个并行任务；页面关闭后均应被 finally 清理
            # （timer 与 popup 是永不结束的假实现，只能靠取消收尾）
            task_states_after_cleanup = [task.done() for task in created_tasks]

        self.assertEqual(len(created_tasks), 3)
        self.assertEqual(task_states_after_cleanup, [True, True, True])

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
        # 挂机上限在 DOCUMENT_WAIT 附近抖动（±30%），不再是固定 60 秒
        self.assertGreaterEqual(kwargs.get("max_wait"), 42)
        self.assertLessEqual(kwargs.get("max_wait"), 78)

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


def _video_state(**overrides):
    video = {
        "visible": True,
        "readyState": 4,
        "paused": False,
        "duration": 600.0,
        "currentTime": 30.0,
        "errorCode": None,
    }
    video.update(overrides)
    return {"ready": True, "selectors": {}, "videos": [video]}


class _SleepingPage:
    """wait_for_timeout 真的睡：让巡检循环按墙钟推进。"""

    def __init__(self, state):
        self.state = state
        self.resume_calls = 0
        self.waits: list[int] = []

    async def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)
        await asyncio.sleep(milliseconds / 1000)

    async def evaluate(self, script):
        if "video.play()" in script:
            self.resume_calls += 1
            return True
        return self.state


class VideoWatchLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_watch_leaves_early_when_section_becomes_learned(self):
        from core.learning import handlers

        page = _SleepingPage(_video_state())

        with (
            patch.object(handlers, "VIDEO_WATCH_SLICE_MIN", 0.1),
            patch.object(handlers, "VIDEO_WATCH_SLICE_MAX", 0.1),
            patch.object(
                handlers,
                "read_section_progress_text",
                new=AsyncMock(return_value="必修 视频 19:55"),
            ),
        ):
            started = time.monotonic()
            await handlers._watch_video_playback(page, object(), watch_seconds=30)
            elapsed = time.monotonic() - started

        # 一段就发现已学完，不再空等剩下的 30 秒
        self.assertEqual(len(page.waits), 1)
        self.assertLess(elapsed, 5)

    async def test_watch_does_not_extend_after_video_reaches_the_end(self):
        """片尾之后播放位置不会再动：不能把它当成「停滞」无限顺延，
        但原计划时长要等满（平台可能还差几十秒才回写进度）。"""
        from core.learning import handlers

        page = _SleepingPage(_video_state(currentTime=599.5, duration=600.0))

        with (
            patch.object(handlers, "VIDEO_WATCH_SLICE_MIN", 0.1),
            patch.object(handlers, "VIDEO_WATCH_SLICE_MAX", 0.1),
            patch.object(handlers, "_VIDEO_LAG_TOLERANCE", 0.01),
            patch.object(handlers, "VIDEO_STALL_MAX_EXTRA_WAIT", 5),
            patch.object(
                handlers,
                "read_section_progress_text",
                new=AsyncMock(return_value="必修 视频 19:55\n需学 05:00"),
            ),
        ):
            started = time.monotonic()
            await handlers._watch_video_playback(page, object(), watch_seconds=1)
            elapsed = time.monotonic() - started

        self.assertGreaterEqual(elapsed, 1.0)
        self.assertLess(elapsed, 2.0)

    async def test_watch_extends_deadline_and_resumes_while_stalled(self):
        from core.learning import handlers

        page = _SleepingPage(_video_state(paused=True, currentTime=12.0))

        with (
            patch.object(handlers, "VIDEO_WATCH_SLICE_MIN", 0.1),
            patch.object(handlers, "VIDEO_WATCH_SLICE_MAX", 0.1),
            patch.object(handlers, "_VIDEO_LAG_TOLERANCE", 0.01),
            patch.object(handlers, "VIDEO_STALL_MAX_EXTRA_WAIT", 0.5),
            patch.object(
                handlers,
                "read_section_progress_text",
                new=AsyncMock(return_value="必修 视频 19:55\n需学 05:00"),
            ),
            patch.object(
                handlers,
                "check_and_handle_rating_popup",
                new=AsyncMock(return_value=False),
            ),
        ):
            started = time.monotonic()
            await handlers._watch_video_playback(page, object(), watch_seconds=1)
            elapsed = time.monotonic() - started

        # 播放位置不动 → 按落后量顺延，封顶 0.5 秒后收工；期间尝试恢复播放
        self.assertGreater(elapsed, 1.0)
        self.assertLess(elapsed, 4.0)
        self.assertGreater(page.resume_calls, 0)

    async def test_watch_does_not_extend_when_playback_keeps_up(self):
        from core.learning import handlers

        class AdvancingPage(_SleepingPage):
            async def evaluate(self, script):
                if "video.play()" in script:
                    self.resume_calls += 1
                    return True
                # 播放位置随墙钟同步推进
                self.state["videos"][0]["currentTime"] += sum(self.waits[-1:]) / 1000
                return self.state

        page = AdvancingPage(_video_state())

        with (
            patch.object(handlers, "VIDEO_WATCH_SLICE_MIN", 0.1),
            patch.object(handlers, "VIDEO_WATCH_SLICE_MAX", 0.1),
            patch.object(handlers, "_VIDEO_LAG_TOLERANCE", 0.01),
            patch.object(
                handlers,
                "read_section_progress_text",
                new=AsyncMock(return_value="必修 视频 19:55\n需学 05:00"),
            ),
        ):
            started = time.monotonic()
            await handlers._watch_video_playback(page, object(), watch_seconds=1)
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2.0)
        self.assertEqual(page.resume_calls, 0)
        # 每段随机切分：1 秒被切成多段而不是一次死等
        self.assertGreater(len(page.waits), 1)


class VideoHandlerPacingTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_video_adds_random_overshoot_and_cancels_helpers(self):
        from core.learning import handlers

        created_tasks = []
        original_create_task = asyncio.create_task

        async def never_finishing(*_args, **_kwargs):
            await asyncio.Future()

        def tracking_create_task(coro):
            task = original_create_task(coro)
            created_tasks.append(task)
            return task

        class Page:
            def locator(self, _selector):
                return _FakeLocator()

            async def wait_for_timeout(self, _ms):
                return None

        watch_calls = {}

        async def fake_watch(_page, _box, *, watch_seconds):
            watch_calls["watch_seconds"] = watch_seconds

        with (
            patch.object(
                handlers, "_ensure_video_player_ready", new=AsyncMock(return_value=None)
            ),
            patch.object(
                handlers,
                "check_and_handle_rating_popup",
                new=AsyncMock(return_value=False),
            ),
            patch.object(handlers, "_watch_video_playback", new=fake_watch),
            patch.object(handlers, "timer", new=never_finishing),
            patch.object(
                handlers, "check_rating_popup_periodically", new=never_finishing
            ),
            patch.object(
                handlers, "wait_until_learned", new=AsyncMock(return_value=None)
            ) as mock_sync,
            patch.object(
                handlers.asyncio, "create_task", side_effect=tracking_create_task
            ),
        ):
            await handlers.handle_video(_FakeBox(), Page())

        # 剩余 00:31 → 取整到 60 秒，再加 5~45 秒随机余量，不再卡整分钟
        self.assertGreater(watch_calls["watch_seconds"], 60)
        self.assertLessEqual(watch_calls["watch_seconds"], 105)
        # 巡检提前结束时，进度条与弹窗巡检必须被取消
        self.assertEqual(len(created_tasks), 3)
        self.assertTrue(all(task.done() for task in created_tasks))
        mock_sync.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
