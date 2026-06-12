import unittest
from types import SimpleNamespace
from unittest.mock import patch


class LauncherEntryTests(unittest.TestCase):
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

        afk_choice = launcher.MENU_OPTIONS.index("仅挂课") + 1

        with (
            patch("core.config.setup_logging"),
            patch("core.state.collect_project_state", return_value=fake_state),
            patch("core.ui.show_title"),
            patch("core.ui.render_dashboard"),
            patch("core.ui.show_menu", return_value=afk_choice),
            patch("core.ui.show_warning") as mock_warning,
            patch(
                "core.launcher_controller.handle_afk",
                side_effect=UserAbortRequested("已保存当前和剩余学习链接，程序退出"),
            ),
        ):
            result = launcher.main()

        self.assertEqual(result, 0)
        mock_warning.assert_called_once_with("已保存当前和剩余学习链接，程序退出")


if __name__ == "__main__":
    unittest.main()
