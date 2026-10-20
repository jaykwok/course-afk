"""运行界面的空间分配回归：日志须可见，缩放后卡片样式须恢复。"""

import asyncio
import unittest

from rich.color import Color
from rich.text import Text
from textual.geometry import Offset
from textual.widgets import RichLog

from core.auth.credential import CredentialMetadata
from core.palette import BG_PANEL, GREEN, HAIRLINE
from core.state import ProjectState
from core.ui.tui_app import CourseTuiApp
from core.ui.tui_render import (
    build_account_chip,
    build_action_line,
    build_dashboard_meta,
    build_stat_tiles,
)


class _LayoutApp(CourseTuiApp):
    def _spawn_launcher_thread(self):
        pass


def _start_operation(app):
    state = ProjectState(True, False, 8, 1, 2, 1)
    metadata = CredentialMetadata(
        "2026-10-20T10:00:00+08:00", "2026-11-17T10:00:00+08:00",
        "测试用户", "test-user", "测试用户（test-user）",
    )
    app.update_dashboard(
        build_account_chip(state, metadata), build_stat_tiles(state),
        build_dashboard_meta(state, metadata), build_action_line("仅挂课"),
    )
    app.set_operation_status("课程学习", "正在处理课程")
    app.set_progress("正在学习课程章节" * 15, 30, 60)
    app.emit_log(Text("运行日志：测试进度与错误信息仍可见"))


class RunningLayoutTests(unittest.TestCase):
    def assert_log_visible(self, app):
        log = app.query_one("#log", RichLog)
        self.assertGreaterEqual(log.content_size.height, 3)
        self.assertGreaterEqual(log.region.y, 0)
        self.assertLessEqual(log.region.bottom, app.screen.region.bottom)
        # 进度摘要再长也不能将双行进度区挤高。
        progress = app.query_one("#progress")
        self.assertEqual(progress.content_size.height, 2)

    def test_running_log_visible_when_starting_in_small_terminal(self):
        async def scenario():
            for size in ((60, 20), (50, 15)):
                with self.subTest(size=size):
                    app = _LayoutApp()
                    async with app.run_test(size=size) as pilot:
                        _start_operation(app)
                        await pilot.pause()
                        self.assert_log_visible(app)

        asyncio.run(scenario())

    def test_running_layout_adapts_to_resize_and_restores_card_borders(self):
        async def scenario():
            app = _LayoutApp()
            async with app.run_test(size=(120, 40)) as pilot:
                _start_operation(app)
                await pilot.pause()
                dashboard = app.query_one("#dashboard")
                wide_gutter = dashboard.styles.gutter.height
                for size in ((60, 20), (90, 22), (50, 15), (120, 40)):
                    with self.subTest(size=size):
                        await pilot.resize_terminal(*size)
                        await pilot.pause()
                        self.assert_log_visible(app)
                        if size == (120, 40):
                            self.assertEqual(dashboard.styles.gutter.height, wide_gutter)
                        else:
                            self.assertLess(dashboard.styles.gutter.height, wide_gutter)

        asyncio.run(scenario())

    def test_scrollbar_renders_theme_colors_for_idle_hover_and_drag(self):
        async def scenario():
            app = _LayoutApp()
            async with app.run_test(size=(80, 28)) as pilot:
                log = app.query_one("#log", RichLog)
                for index in range(100):
                    log.write(f"运行日志 {index}")
                await pilot.pause()
                self.assertTrue(log.show_vertical_scrollbar)
                bar = log.vertical_scrollbar
                for mouse_over, grabbed, expected in (
                    (False, None, HAIRLINE),
                    (True, None, GREEN),
                    (False, Offset(1, 1), GREEN),
                ):
                    with self.subTest(mouse_over=mouse_over, grabbed=grabbed):
                        bar.mouse_over = mouse_over
                        bar.grabbed = grabbed
                        style = bar.render().style
                        self.assertEqual(style.color.triplet, Color.parse(expected).triplet)
                        self.assertEqual(style.bgcolor.triplet, Color.parse(BG_PANEL).triplet)
                bar.grabbed = None

        asyncio.run(scenario())
