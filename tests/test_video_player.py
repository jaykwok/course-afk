import unittest
from unittest.mock import AsyncMock, patch


class _Wrapper:
    def __init__(self, box):
        self.box = box

    async def scroll_into_view_if_needed(self, timeout=0):
        return None

    async def wait_for(self, state=None, timeout=0):
        return None

    async def click(self, timeout=0, force=False):
        self.box.clicks += 1

    async def inner_text(self, timeout=0):
        return "第八章节 必修 视频 01:40 需学 01:20"


class _Box:
    def __init__(self, *, focus_after_clicks=0):
        self.clicks = 0
        self.focus_after_clicks = focus_after_clicks
        self.wrapper = _Wrapper(self)

    def locator(self, selector):
        assert selector == ".section-item-wrapper"
        return self.wrapper

    async def get_attribute(self, attribute):
        if attribute == "class":
            focused = self.clicks >= self.focus_after_clicks
            return "chapter-list-box required focus" if focused else "chapter-list-box"
        if attribute == "id":
            return "chapter-video-8"
        if attribute == "data-sectiontype":
            return "6"
        return None


class _EmptyLocator:
    async def all(self):
        return []


class _Page:
    def __init__(self, states=None):
        self.states = list(states or [])
        self.waits = []

    def locator(self, selector):
        assert selector == ".register-mask-layer"
        return _EmptyLocator()

    async def evaluate(self, _script):
        if not self.states:
            return {"ready": False, "selectors": {}, "videos": []}
        if len(self.states) == 1:
            return self.states[0]
        return self.states.pop(0)

    async def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)


class CourseSectionActivationTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_class_without_focus_is_not_accepted(self):
        from core.learning.common import is_course_section_focused

        class ActiveOnlyBox:
            async def get_attribute(self, attribute):
                return "chapter-list-box active" if attribute == "class" else None

        self.assertFalse(await is_course_section_focused(ActiveOnlyBox()))

    async def test_activation_reclicks_same_box_until_focus_is_observed(self):
        from core.learning.flows import _activate_course_section

        page = _Page()
        box = _Box(focus_after_clicks=2)
        with (
            patch(
                "core.learning.flows.handle_rating_popup",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "core.learning.flows.dismiss_topmost_overlays_async",
                new=AsyncMock(return_value=0),
            ),
        ):
            await _activate_course_section(page, box)

        self.assertEqual(box.clicks, 2)


class VideoPlayerReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_player_is_tied_to_focused_target(self):
        from core.learning.handlers import _wait_for_video_player_ready

        page = _Page(
            states=[
                {
                    "ready": True,
                    "selectors": {"video": 1},
                    "videos": [{"readyState": 4}],
                }
            ]
        )
        box = _Box(focus_after_clicks=0)

        state = await _wait_for_video_player_ready(page, box=box, timeout_ms=0)

        self.assertTrue(state["ready"])
        self.assertTrue(state["target_focused"])

    async def test_retry_reclicks_exact_target_box(self):
        from core.learning.handlers import _ensure_video_player_ready

        page = _Page(
            states=[
                {"ready": False, "selectors": {}, "videos": []},
                {
                    "ready": True,
                    "selectors": {"video": 1},
                    "videos": [{"readyState": 4}],
                },
            ]
        )
        box = _Box(focus_after_clicks=0)
        with (
            patch(
                "core.learning.handlers.check_and_handle_rating_popup",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "core.browser.overlays.dismiss_topmost_overlays_async",
                new=AsyncMock(return_value=0),
            ),
        ):
            await _ensure_video_player_ready(
                page,
                box=box,
                attempts=2,
                wait_timeout_ms=0,
            )

        self.assertEqual(box.clicks, 1)

    async def test_final_failure_has_structured_player_diagnostics(self):
        from core.abort import VideoPlayerNotReadyError
        from core.learning.handlers import _ensure_video_player_ready

        page = _Page(
            states=[{"ready": False, "selectors": {"video": 0}, "videos": []}]
        )
        box = _Box(focus_after_clicks=0)
        with (
            patch(
                "core.learning.handlers.check_and_handle_rating_popup",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "core.browser.overlays.dismiss_topmost_overlays_async",
                new=AsyncMock(return_value=0),
            ),
        ):
            with self.assertRaises(VideoPlayerNotReadyError) as caught:
                await _ensure_video_player_ready(
                    page,
                    box=box,
                    attempts=1,
                    wait_timeout_ms=0,
                )

        self.assertEqual(caught.exception.reason, "video_player_not_ready")
        self.assertEqual(caught.exception.detail["target"]["section_type"], "6")
        self.assertTrue(
            caught.exception.detail["player_state"]["target_focused"]
        )


if __name__ == "__main__":
    unittest.main()
