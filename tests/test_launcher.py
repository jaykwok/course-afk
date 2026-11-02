from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


class LauncherEntryTests(unittest.TestCase):
    def test_learning_and_exam_main_actions_are_independent_and_direct(self):
        import launcher

        for choice in (1, 2):
            ui = Mock()
            ui.show_menu.side_effect = [choice, 7]
            with (
                patch("core.config.setup_logging"),
                patch(
                    "core.state.collect_project_state", return_value=SimpleNamespace()
                ),
                patch("core.app.launcher_controller.handle_afk") as learning,
                patch("core.app.launcher_controller.handle_ai_exam") as exam,
            ):
                self.assertEqual(launcher.main(ui), 0)
            ui.prompt_choice.assert_not_called()
            self.assertEqual(learning.call_count, int(choice == 1))
            self.assertEqual(exam.call_count, int(choice == 2))

    def test_add_links_main_action_opens_input_directly(self):
        import launcher

        ui = Mock()
        ui.show_menu.side_effect = [3, 7]
        with (
            patch("core.config.setup_logging"),
            patch("core.state.collect_project_state", return_value=SimpleNamespace()),
            patch("core.app.launcher_controller.handle_manual_selection") as select,
        ):
            self.assertEqual(launcher.main(ui), 0)
        select.assert_called_once()

    def test_main_returns_zero_and_shows_warning_when_user_aborts_afk(self):
        import launcher
        from core.abort import UserAbortRequested

        ui = Mock()
        ui.show_menu.return_value = 1
        with (
            patch("core.config.setup_logging"),
            patch("core.state.collect_project_state", return_value=SimpleNamespace()),
            patch(
                "core.app.launcher_controller.handle_afk",
                side_effect=UserAbortRequested("已保存待办"),
            ),
        ):
            self.assertEqual(launcher.main(ui), 0)
        ui.show_warning.assert_called_once_with("已保存待办")
