import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.abort import CoursePlaybackNotStartedError
from core.learning import playback
from core.queues.learning import RETRIABLE_LEARNING_FAILURE_REASONS


def state(**changes):
    return {'available': True, 'target_found': True, 'target_focused': True,
            'start_gate_visible': False, 'content_visible': True, 'videos': [], **changes}


class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.button = SimpleNamespace(click=AsyncMock())
        self.controls = SimpleNamespace(count=AsyncMock(return_value=0), first=self.button)
        self.page = SimpleNamespace(locator=lambda selector: SimpleNamespace(first=self.button) if 'registerMask' in selector else self.controls,
                                    wait_for_timeout=AsyncMock(), evaluate=AsyncMock(return_value=True))
        self.wrapper = SimpleNamespace(click=AsyncMock())
        self.box = SimpleNamespace(get_attribute=AsyncMock(return_value='section-id'), locator=lambda _: self.wrapper)

    async def test_visible_document_is_not_ready_while_start_gate_remains(self):
        with patch.object(playback, 'read_course_playback_state', new=AsyncMock(side_effect=[state(start_gate_visible=True), state()])):
            result = await playback.ensure_course_started(self.page, self.box)
        self.button.click.assert_awaited_once_with(timeout=3000)
        self.assertFalse(result['start_gate_visible'])

    async def test_only_clicks_actual_button_and_never_toggles_an_already_started_course(self):
        selectors = []
        self.page.locator = lambda selector: (selectors.append(selector), SimpleNamespace(first=self.button))[1]
        with patch.object(playback, 'read_course_playback_state', new=AsyncMock(side_effect=[state(start_gate_visible=True), state(), state()])):
            await playback.ensure_course_started(self.page, self.box)
            await playback.ensure_course_started(self.page, self.box)
        self.assertEqual(selectors, [".register-mask-layer:visible [id$='registerMask']"])
        self.button.click.assert_awaited_once()

    async def test_loading_race_retries_button_and_verifies_after_last_click(self):
        with patch.object(playback, 'read_course_playback_state', new=AsyncMock(side_effect=[state(start_gate_visible=True), state(start_gate_visible=True), state()])):
            await playback.ensure_course_started(self.page, self.box, attempts=2)
        self.assertEqual(self.button.click.await_count, 2)

    async def test_focus_before_content_mount_waits_for_actual_content(self):
        content = SimpleNamespace(wait_for=AsyncMock())
        self.page.locator = lambda selector: SimpleNamespace(first=content)
        with patch.object(playback, 'read_course_playback_state', new=AsyncMock(side_effect=[state(content_visible=False), state()])):
            await playback.ensure_course_started(self.page, self.box)
        content.wait_for.assert_awaited_once_with(state='visible', timeout=8000)
        self.button.click.assert_not_awaited()

    async def test_failed_click_does_not_claim_the_course_started(self):
        self.button.click.side_effect = RuntimeError('loading overlay intercepts click')
        with patch.object(playback, 'read_course_playback_state', new=AsyncMock(return_value=state(start_gate_visible=True))):
            with self.assertRaises(CoursePlaybackNotStartedError) as caught:
                await playback.ensure_course_started(self.page, self.box, attempts=2)
        self.assertEqual(caught.exception.reason, 'playback_not_started')
        self.assertTrue(caught.exception.detail['playback_state']['start_gate_visible'])
        self.assertIn(caught.exception.reason, RETRIABLE_LEARNING_FAILURE_REASONS)

    async def test_wrong_focus_reselects_only_target_then_checks_start_gate(self):
        with patch.object(playback, 'read_course_playback_state', new=AsyncMock(side_effect=[state(target_focused=False), state(start_gate_visible=True), state()])):
            await playback.ensure_course_started(self.page, self.box)
        self.assertEqual(self.wrapper.click.await_count, 2)
        self.button.click.assert_awaited_once()

    async def test_video_native_pause_is_resumed_and_read_back(self):
        paused = state(videos=[{'paused': True, 'ended': False, 'ready_state': 4}])
        playing = state(videos=[{'paused': False, 'ended': False, 'ready_state': 4}])
        with patch.object(playback, 'read_course_playback_state', new=AsyncMock(side_effect=[paused, playing])):
            await playback.ensure_course_started(self.page, self.box)
        self.page.evaluate.assert_awaited_once()
        self.assertIn('await video.play()', self.page.evaluate.await_args.args[0])

    async def test_visible_video_control_is_used_when_available(self):
        self.controls.count.return_value = 1
        with patch.object(playback, 'read_course_playback_state', new=AsyncMock(side_effect=[state(videos=[{'paused': True, 'ended': False, 'ready_state': 4}]), state()])):
            await playback.ensure_course_started(self.page, self.box)
        self.button.click.assert_awaited_once()
        self.page.evaluate.assert_not_awaited()

    async def test_play_promise_success_without_state_change_is_not_accepted(self):
        paused = state(videos=[{'paused': True, 'ended': False, 'ready_state': 4}])
        with patch.object(playback, 'read_course_playback_state', new=AsyncMock(return_value=paused)):
            with self.assertRaises(CoursePlaybackNotStartedError):
                await playback.ensure_course_started(self.page, self.box, attempts=1)

    async def test_ended_video_is_not_replayed(self):
        with patch.object(playback, 'read_course_playback_state', new=AsyncMock(return_value=state(videos=[{'paused': True, 'ended': True, 'ready_state': 4}]))):
            await playback.ensure_course_started(self.page, self.box)
        self.page.evaluate.assert_not_awaited()
        self.button.click.assert_not_awaited()

    async def test_blurred_document_is_refocused_and_state_is_rechecked(self):
        self.page.bring_to_front = AsyncMock()
        with patch.object(playback, 'read_course_playback_state', new=AsyncMock(side_effect=[state(document_focused=False), state(document_focused=True)])):
            await playback.ensure_course_started(self.page, self.box)
        self.page.bring_to_front.assert_awaited_once()

    async def test_failed_document_focus_is_not_reported_as_running(self):
        self.page.bring_to_front = AsyncMock()
        with patch.object(playback, 'read_course_playback_state', new=AsyncMock(return_value=state(document_focused=False))):
            with self.assertRaises(CoursePlaybackNotStartedError):
                await playback.ensure_course_started(self.page, self.box, attempts=1)

    async def test_unreadable_state_is_retained_as_failure(self):
        with patch.object(playback, 'read_course_playback_state', new=AsyncMock(return_value={'available': False})):
            with self.assertRaises(CoursePlaybackNotStartedError):
                await playback.ensure_course_started(self.page, self.box)
        self.button.click.assert_not_awaited()

    async def test_raw_state_reader_does_not_infer_paused_from_progress(self):
        self.page.evaluate.return_value = state(start_gate_visible=True, timer_seconds=0)
        result = await playback.read_course_playback_state(self.page, self.box)
        self.assertTrue(result['start_gate_visible'])
        self.assertEqual(self.page.evaluate.await_args.args[1], 'section-id')
        self.assertIn('video.paused', self.page.evaluate.await_args.args[0])
        self.assertNotIn('authorization', self.page.evaluate.await_args.args[0])

    async def test_close_and_cancellation_propagate(self):
        class TargetClosedError(Exception):
            pass
        for error in (TargetClosedError('Target page, context or browser has been closed'), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__), patch.object(playback, 'read_course_playback_state', new=AsyncMock(return_value=state(start_gate_visible=True))):
                self.button.click.side_effect = error
                with self.assertRaises(type(error)):
                    await playback.ensure_course_started(self.page, self.box)


if __name__ == '__main__':
    unittest.main()
