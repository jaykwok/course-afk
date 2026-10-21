"""统一考试一级入口与自动交卷默认值的实际 TUI 路由。"""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from core.state import ProjectState
from core.ui.tui_app import OptionScreen, PauseScreen, YesNoScreen
from core.ui.tui_bridge import TuiFrontend
from tests.test_tui_smoke import _CapturingApp, _wait_for


class ExamTuiTests(unittest.TestCase):
    def test_direct_exam_entry_prompts_default_no_and_forwards_explicit_yes(self):
        async def scenario():
            with patch("core.config.setup_logging"), patch("core.state.collect_project_state", return_value=ProjectState(True, False, 0, 0, 1, 0)), patch("core.config.is_ai_configured", return_value=True), patch("core.queues.exam.read_exam_urls", return_value=["https://example.com/exam"]), patch("core.app.workflows.run_ai_exam_workflow", new=AsyncMock(return_value=0)) as workflow:
                app = _CapturingApp()
                frontend = TuiFrontend(app)
                frontend.install()
                try:
                    async with app.run_test(size=(90, 28)) as pilot:
                        def main_menu():
                            return isinstance(app.screen, OptionScreen) and app.screen._title == "主菜单"
                        for key, expected in (("enter", False), ("y", True)):
                            self.assertTrue(await _wait_for(main_menu, pilot))
                            await pilot.press("2")
                            self.assertTrue(await _wait_for(lambda:isinstance(app.screen, YesNoScreen), pilot))
                            self.assertEqual(app.focused.id, "no")
                            await pilot.press(key)
                            self.assertTrue(await _wait_for(lambda:isinstance(app.screen, PauseScreen), pilot))
                            self.assertIs(workflow.await_args.kwargs["auto_submit"], expected)
                            await pilot.press("enter")
                        self.assertTrue(await _wait_for(main_menu, pilot))
                        await pilot.press("0")
                        self.assertTrue(await _wait_for(lambda:not app._test_worker.is_alive(), pilot))
                    self.assertEqual(app._worker_result, "ok")
                finally:
                    frontend.restore()
        asyncio.run(asyncio.wait_for(scenario(), 30))
