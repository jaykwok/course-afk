import unittest
from types import SimpleNamespace
from unittest.mock import patch


class LauncherEntryTests(unittest.TestCase):
    def test_learning_and_exam_main_actions_are_independent_and_direct(self):
        import launcher
        fake_state = SimpleNamespace(has_credential=True, credential_expired=False,
            learning_count=1, exam_count=1, manual_exam_count=0)
        for choice in (1, 2):
            with self.subTest(choice=choice), patch("core.config.setup_logging"), patch("core.state.collect_project_state", return_value=fake_state), patch("core.ui.show_title"), patch("core.ui.render_dashboard"), patch("core.ui.prepare_menu_loading"), patch("core.ui.show_success"), patch("core.ui.show_menu", side_effect=[choice, 7]), patch("core.ui.prompt_choice") as submenu, patch("core.app.launcher_controller.handle_afk") as learning, patch("core.app.launcher_controller.handle_ai_exam") as exam:
                self.assertEqual(launcher.main(), 0)
                submenu.assert_not_called()
                self.assertEqual(learning.call_count, int(choice == 1))
                self.assertEqual(exam.call_count, int(choice == 2))

    def test_add_links_main_action_opens_input_directly(self):
        import launcher
        fake_state = SimpleNamespace(has_credential=True, credential_expired=False,
            learning_count=0, exam_count=0, manual_exam_count=0)
        with patch("core.config.setup_logging"), patch("core.state.collect_project_state", return_value=fake_state), patch("core.ui.show_title"), patch("core.ui.render_dashboard"), patch("core.ui.prepare_menu_loading"), patch("core.ui.show_success"), patch("core.ui.show_menu", side_effect=[3, 7]), patch("core.ui.prompt_choice", return_value=2), patch("core.app.launcher_controller.handle_manual_selection") as select:
            self.assertEqual(launcher.main(), 0)
        select.assert_called_once()

    def test_launcher_module_does_not_expose_startup_trace_helpers(self):
        import launcher

        self.assertFalse(hasattr(launcher, "_append_startup_trace"))
        self.assertFalse(hasattr(launcher, "_startup_trace_path"))

    def test_main_returns_zero_and_shows_warning_when_user_aborts_afk(self):
        import launcher
        from core.abort import UserAbortRequested

        fake_state = SimpleNamespace(
            has_credential=True,
            credential_expired=False,
            learning_count=1,
            exam_count=0,
            manual_exam_count=0,
        )

        afk_choice = launcher.MENU_OPTIONS.index("开始挂课") + 1

        with (
            patch("core.config.setup_logging"),
            patch("core.state.collect_project_state", return_value=fake_state),
            patch("core.ui.show_title"),
            patch("core.ui.render_dashboard"),
            patch("core.ui.show_menu", return_value=afk_choice),
            patch("core.ui.prompt_choice", return_value=1),
            patch("core.ui.show_warning") as mock_warning,
            patch(
                "core.app.launcher_controller.handle_afk",
                side_effect=UserAbortRequested("已保存当前和剩余学习链接，程序退出"),
            ),
        ):
            result = launcher.main()

        self.assertEqual(result, 0)
        mock_warning.assert_called_once_with("已保存当前和剩余学习链接，程序退出")


if __name__ == "__main__":
    unittest.main()
