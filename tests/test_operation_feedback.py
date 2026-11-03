import logging
import unittest
from unittest.mock import patch

from rich.console import Console
from rich.text import Text
from textual.widgets import Static

from core.ui.tui_app import CourseTuiApp, OptionScreen, PauseScreen
from core.ui.tui_bridge import TextualLogHandler, TuiFrontend


class OperationFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_shows_ui_and_logged_errors_then_clears_for_next_action(self):
        for size in ((100, 30), (60, 20)):
            with self.subTest(size=size), patch.object(
                CourseTuiApp, "_spawn_launcher_thread"
            ):
                app = CourseTuiApp()
                frontend = TuiFrontend(app)
                handler = TextualLogHandler(app)
                self.addCleanup(frontend.close)
                with patch.object(frontend, "_refresh_dashboard"):
                    async with app.run_test(size=size) as pilot:
                        frontend.show_warning("登录未完成：缺少稳定账号标识")
                        handler.emit(logging.LogRecord(
                            "login", logging.ERROR, __file__, 1,
                            "账号接口失败 token=private-value", (), None,
                        ))
                        screen = PauseScreen(details_renderable=Text("已有结果摘要"))
                        await app.push_prompt(screen, cancellable=True)
                        await pilot.pause()
                        widget = screen.query_one("#pause-details", Static)
                        console = Console(width=60, color_system=None)
                        with console.capture() as capture:
                            console.print(widget.content)
                        text = capture.get()
                        self.assertIn("已有结果摘要", text)
                        self.assertIn("缺少稳定账号标识", text)
                        self.assertIn("账号接口失败", text)
                        self.assertNotIn("private-value", text)
                        self.assertTrue(widget.display)
                        self.assertGreater(widget.region.height, 0)
                        await pilot.press("enter")
                        menu = OptionScreen("主菜单", ["下一项", "退出"])
                        await app.push_prompt(menu)
                        await pilot.pause()
                        app.resolve_prompt(menu, 1)
                        next_screen = PauseScreen()
                        await app.push_prompt(next_screen)
                        await pilot.pause()
                        self.assertIsNone(next_screen._details_renderable)

    async def test_success_and_summary_are_in_the_result_dialog(self):
        with patch.object(CourseTuiApp, "_spawn_launcher_thread"):
            app = CourseTuiApp()
            frontend = TuiFrontend(app)
            self.addCleanup(frontend.close)
            with patch.object(frontend, "_refresh_dashboard"):
                async with app.run_test() as pilot:
                    frontend.show_success("登录凭证已更新")
                    frontend.show_summary("当前账号", [("状态", "有效")])
                    screen = PauseScreen()
                    await app.push_prompt(screen)
                    await pilot.pause()
                    console = Console(color_system=None)
                    with console.capture() as capture:
                        console.print(screen.query_one("#pause-details", Static).content)
                    self.assertIn("登录凭证已更新", capture.get())
                    self.assertIn("有效", capture.get())
