import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from textual.widgets import OptionList, Static

from core.state import ProjectState
from core.ui.tui_app import OptionScreen, PauseScreen, YesNoScreen
from core.ui.tui_bridge import TuiFrontend
from tests.test_task_management import QueueFixture
from tests.test_tui_smoke import _CapturingApp, _wait_for


class TaskManagementTuiTests(QueueFixture):
    def test_navigation_clear_confirmation_and_dashboard_refresh_at_two_terminal_sizes(self):
        async def scenario():
            def state():
                counts = [len(json.loads(path.read_bytes())) for path in self.paths.values()]
                return ProjectState(False, True, *counts)

            with patch('core.config.setup_logging'), patch('core.state.collect_project_state', side_effect=state):
                app = _CapturingApp()
                frontend = TuiFrontend(app)
                frontend.install()
                try:
                    async with app.run_test(size=(120, 40)) as pilot:
                        def menu(title):
                            return isinstance(app.screen, OptionScreen) and app.screen._title == title
                        self.assertTrue(await _wait_for(lambda: menu('主菜单'), pilot))
                        options = app.screen.query_one(OptionList)
                        self.assertEqual(options.option_count, 7)
                        self.assertIn('\n', str(options.get_option_at_index(1).prompt))
                        self.assertGreaterEqual(options.scrollable_content_region.height, options.virtual_size.height)
                        previews = Path(__file__).resolve().parents[1]/'tools/capture/tui-preview'
                        previews.mkdir(parents=True, exist_ok=True)
                        app.save_screenshot('main-wide.svg', path=str(previews))
                        await pilot.resize_terminal(60, 20)
                        await pilot.pause()
                        dialog = app.screen.query_one('#opt-dialog')
                        self.assertGreaterEqual(dialog.region.y, 0)
                        self.assertLessEqual(dialog.region.bottom, 20)
                        self.assertNotIn('\n', str(options.get_option_at_index(1).prompt))
                        self.assertLess(app.screen.query_one('#opt-hint').region.y, 20)
                        app.save_screenshot('main-small.svg', path=str(previews))
                        await pilot.resize_terminal(120, 40)
                        await pilot.press('5')
                        self.assertTrue(await _wait_for(lambda: menu('任务管理'), pilot))
                        self.assertIn('主菜单 / 任务管理', str(app.screen.query_one('#opt-path', Static).render()))
                        app.save_screenshot('tasks.svg', path=str(previews))
                        await pilot.press('0')
                        self.assertTrue(await _wait_for(lambda: menu('主菜单'), pilot))
                        await pilot.press('5')
                        self.assertTrue(await _wait_for(lambda: menu('任务管理'), pilot))
                        await pilot.press('3')
                        self.assertTrue(await _wait_for(lambda: isinstance(app.screen, YesNoScreen), pilot))
                        self.assertEqual(app.focused.id, 'no')
                        app.save_screenshot('clear-confirmation.svg', path=str(previews))
                        await pilot.press('enter')
                        self.assertTrue(await _wait_for(lambda: menu('主菜单'), pilot))
                        self.assertEqual({p:p.read_bytes() for p in self.paths.values()}, self.original)
                        await pilot.press('5')
                        self.assertTrue(await _wait_for(lambda: menu('任务管理'), pilot))
                        await pilot.press('3')
                        self.assertTrue(await _wait_for(lambda: isinstance(app.screen, YesNoScreen), pilot))
                        await pilot.press('y')
                        self.assertTrue(await _wait_for(lambda: isinstance(app.screen, PauseScreen), pilot))
                        for name in ('LEARNING_URLS_FILE', 'LEARNING_FAILURES_FILE'):
                            self.assertEqual(json.loads(self.paths[name].read_bytes()), [])
                        self.assertEqual(self.paths['EXAM_URLS_FILE'].read_bytes(), self.original[self.paths['EXAM_URLS_FILE']])
                        await pilot.press('enter')
                        self.assertTrue(await _wait_for(lambda: menu('主菜单'), pilot))
                        status = str(app.screen.query_one('#opt-status', Static).render())
                        self.assertIn('课程 0', status)
                        self.assertIn('挂课失败 0', status)
                        await pilot.press('0')
                        self.assertTrue(await _wait_for(lambda: not app._test_worker.is_alive(), pilot))
                finally:
                    frontend.restore()
                self.assertEqual(app._worker_result, 'ok')
        asyncio.run(asyncio.wait_for(scenario(), timeout=45))
