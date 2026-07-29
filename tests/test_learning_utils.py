import asyncio
import unittest
from unittest.mock import AsyncMock, patch


class LearningUtilityTests(unittest.TestCase):
    def test_is_learned_detects_pending_text(self):
        from core.learning.common import is_learned

        self.assertFalse(is_learned("第一节 需学 12:30"))
        self.assertFalse(is_learned("第二节 需再学 03:00"))
        self.assertFalse(is_learned("必修 视频 45:34\n需再学 36:00"))
        # 文档节常见：需学 00:05 —— 绝不能当已学（曾误匹配「需学 00」前缀）
        self.assertFalse(is_learned("必修 文档 05:00\n需学 00:05"))
        self.assertFalse(is_learned("必修 文档 05:00\n需学 0:05"))
        self.assertFalse(is_learned("需学 00:01"))
        self.assertTrue(is_learned("第三节 已完成 12:30"))
        self.assertTrue(is_learned("必修 视频 19:55"))
        # 平台学完后常短暂残留「需再学 0:00」——应视为已同步
        self.assertTrue(is_learned("必修 视频 19:55\n需再学 0:00"))
        self.assertTrue(is_learned("必修 视频 19:55 需学 00:00"))
        self.assertTrue(is_learned("需再学 0"))
        self.assertTrue(is_learned("必修 文档 05:00\n需学 0:00"))
        # 空文案不得视为已学（DOM 未渲染）
        self.assertFalse(is_learned(""))
        self.assertFalse(is_learned("   "))

    def test_wait_until_learned_accepts_zero_remaining_text(self):
        from core.learning.common import wait_until_learned

        class FakeWrapper:
            def __init__(self, text):
                self._text = text

            async def scroll_into_view_if_needed(self, timeout=0):
                return None

            async def inner_text(self, timeout=None):
                return self._text

        class Box:
            def __init__(self, text):
                self._wrapper = FakeWrapper(text)

            def locator(self, selector):
                assert selector == ".section-item-wrapper"
                return self._wrapper

        class FakePage:
            async def wait_for_timeout(self, _ms):
                return None

        asyncio.run(
            wait_until_learned(
                Box("必修 视频 10:00\n需再学 0:00"),
                FakePage(),
                max_wait=30,
                poll_interval=5,
            )
        )

    def test_wait_until_learned_final_recheck_before_timeout(self):
        from core.learning.common import wait_until_learned

        texts = [
            "必修 视频 10:00\n需再学 01:00",
            "必修 视频 10:00\n需再学 01:00",
            "必修 视频 10:00",  # 超时边界复核命中
        ]
        reads = {"n": 0}

        class FakeWrapper:
            async def scroll_into_view_if_needed(self, timeout=0):
                return None

            async def inner_text(self, timeout=None):
                idx = min(reads["n"], len(texts) - 1)
                reads["n"] += 1
                return texts[idx]

        class Box:
            def locator(self, selector):
                assert selector == ".section-item-wrapper"
                return FakeWrapper()

        class FakePage:
            async def wait_for_timeout(self, _ms):
                return None

        asyncio.run(
            wait_until_learned(Box(), FakePage(), max_wait=5, poll_interval=5)
        )
        # 初始 + 循环内 + 超时边界复核
        self.assertGreaterEqual(reads["n"], 3)

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

    def test_access_denial_read_failure_does_not_claim_no_permission(self):
        from core.learning.common import detect_access_denial

        class UnreadableFrame:
            async def content(self):
                raise RuntimeError("navigation in progress")

        self.assertIsNone(asyncio.run(detect_access_denial(UnreadableFrame())))

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

    def test_build_video_timing_plan_keeps_min_sync_when_learning_is_zero(self):
        from core.config import VIDEO_SYNC_MIN_WAIT, VIDEO_SYNC_POLL_INTERVAL
        from core.learning.common import build_video_timing_plan

        # 解析得到 learning=0 时也不能 0 秒直接判超时（DOM 可能仍挂「需再学」）
        plan = build_video_timing_plan("总时长 00:00 剩余 00:00")

        self.assertEqual(plan.learning_wait_time, 0)
        self.assertEqual(plan.sync_wait_time, VIDEO_SYNC_MIN_WAIT)
        self.assertEqual(plan.sync_poll_interval, VIDEO_SYNC_POLL_INTERVAL)

    def test_document_wait_config_is_fixed_cap_without_sync_phase(self):
        from core import config

        # 文档统一挂 DOCUMENT_WAIT；不再拆「学习 + 同步确认」两段
        self.assertEqual(config.DOCUMENT_WAIT, 60)
        self.assertEqual(config.DOCUMENT_POLL_INTERVAL, 10)
        self.assertFalse(hasattr(config, "DOCUMENT_INITIAL_WAIT"))
        self.assertFalse(hasattr(config, "DOCUMENT_SYNC_EXTRA_WAIT"))

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
