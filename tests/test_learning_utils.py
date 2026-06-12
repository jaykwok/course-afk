import asyncio
import unittest
from unittest.mock import AsyncMock, patch


class LearningUtilityTests(unittest.TestCase):
    def test_is_learned_detects_pending_text(self):
        from core.learning_common import is_learned

        self.assertFalse(is_learned("第一节 需学 12:30"))
        self.assertFalse(is_learned("第二节 需再学 03:00"))
        self.assertTrue(is_learned("第三节 已完成 12:30"))

    def test_time_to_seconds_rounds_up_to_tens(self):
        from core.learning_common import time_to_seconds

        self.assertEqual(time_to_seconds("00:01"), 10)
        self.assertEqual(time_to_seconds("01:01"), 70)
        self.assertEqual(time_to_seconds("1:01:01"), 3670)

    def test_calculate_remaining_time_rounds_up_to_minutes(self):
        from core.learning_common import calculate_remaining_time

        self.assertEqual(
            calculate_remaining_time("总时长 10:00 剩余 03:31"),
            (240, 600),
        )

    def test_calculate_remaining_time_caps_wait_at_video_duration(self):
        from core.learning_common import calculate_remaining_time

        self.assertEqual(
            calculate_remaining_time("总时长 04:00 剩余 01:01"),
            (120, 240),
        )

    def test_get_video_status_interval_uses_dense_updates_for_short_videos(self):
        from core.learning_common import get_video_status_interval

        self.assertEqual(get_video_status_interval(180), 1)
        self.assertEqual(get_video_status_interval(300), 1)

    def test_get_video_status_interval_balances_medium_and_long_videos(self):
        from core.learning_common import get_video_status_interval

        self.assertEqual(get_video_status_interval(301), 5)
        self.assertEqual(get_video_status_interval(1800), 5)
        self.assertEqual(get_video_status_interval(1801), 10)

    def test_build_video_timing_plan_uses_distinct_fallback_and_sync_poll_intervals(self):
        from core.learning_common import build_video_timing_plan

        plan = build_video_timing_plan("总时长 50:00 剩余 33:31")

        self.assertEqual(plan.learning_wait_time, 2040)
        self.assertEqual(plan.learning_fallback_interval, 10)
        self.assertEqual(plan.sync_wait_time, 60)
        self.assertEqual(plan.sync_poll_interval, 1)

    def test_calculate_video_sync_wait_time_uses_theoretical_sync_boundary(self):
        from core.learning_common import calculate_video_sync_wait_time

        self.assertEqual(calculate_video_sync_wait_time(240, 600), 60)
        self.assertEqual(calculate_video_sync_wait_time(300, 600), 0)

    def test_calculate_video_sync_wait_time_caps_wait_when_video_itself_is_shorter(self):
        from core.learning_common import calculate_video_sync_wait_time

        self.assertEqual(calculate_video_sync_wait_time(240, 240), 0)
        self.assertEqual(calculate_video_sync_wait_time(120, 180), 0)

    def test_timer_returns_immediately_when_duration_is_zero(self):
        from core.learning_common import timer

        with patch("core.ui.wait_with_progress", new_callable=AsyncMock) as wait_with_progress:
            asyncio.run(timer(0, fallback_interval=5, description="视频学习进度"))

        wait_with_progress.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
