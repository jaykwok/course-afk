import asyncio
import unittest
from unittest.mock import AsyncMock, patch


class LearningUtilityTests(unittest.TestCase):
    def test_is_learned_detects_pending_text(self):
        from core.learning.common import is_learned

        self.assertFalse(is_learned("第一节 需学 12:30"))
        self.assertFalse(is_learned("第二节 需再学 03:00"))
        self.assertTrue(is_learned("第三节 已完成 12:30"))
        self.assertTrue(is_learned("必修 视频 19:55"))
        # 空文案不得视为已学（DOM 未渲染）
        self.assertFalse(is_learned(""))
        self.assertFalse(is_learned("   "))

    def test_match_concurrent_study_limit(self):
        from core.learning.common import match_concurrent_study_limit

        self.assertIsNotNone(
            match_concurrent_study_limit("您已打开新的课程详情页，点击按钮…")
        )
        self.assertIsNone(match_concurrent_study_limit("正常课程页"))

    def test_match_access_denial_classifies_reasons(self):
        from core.learning.common import match_access_denial

        self.assertEqual(
            match_access_denial("…该资源已不存在…"),
            ("resource_gone", "该资源已不存在，已从课程链接清理"),
        )
        self.assertEqual(
            match_access_denial("…该资源已下架…"),
            ("resource_delisted", "该资源已下架，已从课程链接清理"),
        )
        self.assertEqual(
            match_access_denial("…您没有权限查看该资源…"),
            ("no_permission", "无权限访问该学习资源，已从课程链接清理"),
        )
        self.assertIsNone(match_access_denial("正常课程内容"))

    def test_time_to_seconds_rounds_up_to_tens(self):
        from core.learning.common import time_to_seconds

        self.assertEqual(time_to_seconds("00:01"), 10)
        self.assertEqual(time_to_seconds("01:01"), 70)
        self.assertEqual(time_to_seconds("1:01:01"), 3670)

    def test_calculate_remaining_time_rounds_up_to_minutes(self):
        from core.learning.common import calculate_remaining_time

        self.assertEqual(
            calculate_remaining_time("总时长 10:00 剩余 03:31"),
            (240, 600),
        )

    def test_calculate_remaining_time_caps_wait_at_video_duration(self):
        from core.learning.common import calculate_remaining_time

        self.assertEqual(
            calculate_remaining_time("总时长 04:00 剩余 01:01"),
            (120, 240),
        )

    def test_build_video_timing_plan_includes_sync_poll_interval(self):
        from core.config import VIDEO_SYNC_POLL_INTERVAL
        from core.learning.common import build_video_timing_plan

        plan = build_video_timing_plan("总时长 50:00 剩余 33:31")

        self.assertEqual(plan.learning_wait_time, 2040)
        self.assertEqual(plan.sync_wait_time, 60)
        self.assertEqual(plan.sync_poll_interval, VIDEO_SYNC_POLL_INTERVAL)
        self.assertFalse(hasattr(plan, "learning_fallback_interval"))

    def test_build_video_timing_plan_applies_min_sync_wait_when_theory_is_zero(self):
        from core.config import VIDEO_SYNC_MIN_WAIT, VIDEO_SYNC_POLL_INTERVAL
        from core.learning.common import build_video_timing_plan

        # 理论同步窗为 0（剩余已对齐 5 分钟边界），仍给最短宽限
        plan = build_video_timing_plan("总时长 10:00 剩余 05:00")

        self.assertEqual(plan.learning_wait_time, 300)
        self.assertEqual(plan.sync_wait_time, VIDEO_SYNC_MIN_WAIT)
        self.assertEqual(plan.sync_poll_interval, VIDEO_SYNC_POLL_INTERVAL)

    def test_calculate_video_sync_wait_time_uses_theoretical_sync_boundary(self):
        from core.learning.common import calculate_video_sync_wait_time

        self.assertEqual(calculate_video_sync_wait_time(240, 600), 60)
        self.assertEqual(calculate_video_sync_wait_time(300, 600), 0)

    def test_calculate_video_sync_wait_time_caps_wait_when_video_itself_is_shorter(self):
        from core.learning.common import calculate_video_sync_wait_time

        self.assertEqual(calculate_video_sync_wait_time(240, 240), 0)
        self.assertEqual(calculate_video_sync_wait_time(120, 180), 0)

    def test_timer_returns_immediately_when_duration_is_zero(self):
        from core.learning.common import timer

        with patch("core.ui.wait_with_progress", new_callable=AsyncMock) as wait_with_progress:
            asyncio.run(timer(0, description="视频学习进度"))

        wait_with_progress.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
