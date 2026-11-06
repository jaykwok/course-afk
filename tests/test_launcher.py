import unittest
from unittest.mock import Mock, patch
from core.app.runtime import dispatch

class LauncherEntryTests(unittest.TestCase):
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
