import unittest
from unittest.mock import Mock, patch
from contextlib import ExitStack
from core import config
from core.app.runtime import dispatch

class LauncherEntryTests(unittest.TestCase):
    def test_workbench_shortcuts_dispatch_without_secondary_menus(self):
        state = object()
        ui = Mock()
        ui.prompt_choice.side_effect = AssertionError("工作台快捷操作不应显示二级菜单")
        handlers = (
            "handle_refresh_credential", "handle_clear_credentials",
            "handle_clear_links", "handle_show_output_state",
            "handle_account_management", "handle_task_management",
        )
        cases = (
            ("login", "handle_refresh_credential", (state, ui), {}),
            ("switch_account", "handle_refresh_credential", (state, ui), {"switch_account": True}),
            ("clear_credentials", "handle_clear_credentials", (state, ui), {}),
            ("clear_courses", "handle_clear_links", (ui,), {"include_exams": False}),
            ("clear_all", "handle_clear_links", (ui,), {"include_exams": True}),
            ("tasks", "handle_show_output_state", (config.EXAM_URLS_FILE, config.LEARNING_URLS_FILE, config.MANUAL_EXAM_FILE, ui), {}),
        )
        for kind, target, args, kwargs in cases:
            with self.subTest(kind=kind), ExitStack() as stack:
                stack.enter_context(patch("core.state.collect_project_state", return_value=state))
                mocks = {name: stack.enter_context(patch("core.app.launcher_controller." + name)) for name in handlers}
                dispatch(kind, ui)
                mocks[target].assert_called_once_with(*args, **kwargs)
                for name, handler in mocks.items():
                    if name != target:
                        handler.assert_not_called()
        ui.prompt_choice.assert_not_called()

    def test_actions_are_independent_and_direct(self):
        for kind, name in [("learn", "handle_afk"),("exam", "handle_ai_exam"),("links", "handle_manual_selection"),("references", "handle_reference_collection")]:
            ui=Mock()
            with patch("core.app.launcher_controller." + name) as handle:
                dispatch(kind,ui)
            handle.assert_called_once()

    def test_removed_ui_flag_is_rejected(self):
        import launcher
        with patch.object(launcher.sys,"argv",["launcher.py","--ui","tui"]), patch("core.startup.report_fatal_error") as report:
            self.assertEqual(launcher.entrypoint(),1)
        self.assertIn("Web", str(report.call_args.args[0]))
