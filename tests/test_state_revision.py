from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core.tasks.identity import parse_entry
from core.tasks.repository import TaskRepository
from core.ui.web_frontend import WebFrontend
from core.web.session import WebSession
from core.app.control_session import ControlSession


class StateRevisionTests(unittest.TestCase):
    def test_sqlite_only_change_refreshes_dashboard_once_without_json_export(self):
        from core.state import ProjectState

        with (
            TemporaryDirectory() as tmp,
            patch("core.config.DATA_DIR", Path(tmp)),
            patch("core.config.COOKIES_FILE", Path(tmp) / "cookies.json"),
        ):
            repo = TaskRepository(Path(tmp) / "workflows/state.sqlite")
            frontend = WebFrontend(WebSession(ControlSession()))
            frontend.render_dashboard = Mock()
            try:
                with patch(
                    "core.state.collect_project_state",
                    return_value=ProjectState(True, False, 1, 0, 0, 0),
                ) as collect:
                    frontend.refresh_dashboard()
                    frontend.refresh_dashboard()
                    self.assertEqual(collect.call_count, 1)
                    repo.upsert(parse_entry("https://example.invalid/task", "test"))
                    frontend.refresh_dashboard()
                    self.assertEqual(collect.call_count, 2)
                    frontend.refresh_dashboard()
                    self.assertEqual(collect.call_count, 2)
                self.assertFalse((Path(tmp) / "links").exists())
            finally:
                frontend.close()
