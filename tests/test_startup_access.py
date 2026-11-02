import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from core.auth.access import CredentialUnavailableError, require_task_account
from core.auth.credential import extract_account_profile_from_context, save_credential_bundle
from core.tasks.identity import account_id


class StartupAccessTests(unittest.IsolatedAsyncioTestCase):
    def test_recoverable_identity_error_returns_to_menu(self):
        import launcher
        import core.ui as ui

        choices = iter((3, 7))
        state = SimpleNamespace()
        with patch("core.config.setup_logging"), patch("core.state.collect_project_state", return_value=state), patch("core.app.launcher_controller.handle_manual_selection", side_effect=CredentialUnavailableError("请重新登录")), patch.object(ui, "show_menu", side_effect=lambda _: next(choices)), patch.object(ui, "show_title"), patch.object(ui, "render_dashboard"), patch.object(ui, "prepare_menu_loading"), patch.object(ui, "show_success"), patch.object(ui, "show_warning") as warning, patch.object(ui, "pause") as pause:
            self.assertEqual(launcher.main(), 0)
        warning.assert_called_once_with("请重新登录")
        pause.assert_called_once()

    async def test_real_profile_extraction_produces_executable_distinct_identities(self):
        for user_id in ("identity-a", "identity-b"):
            page = SimpleNamespace(goto=AsyncMock(), wait_for_url=AsyncMock(), wait_for_function=AsyncMock(), evaluate=AsyncMock(return_value={"id": user_id, "name": "相同显示名"}), close=AsyncMock())
            with TemporaryDirectory() as tmp, patch("core.auth.credential.prepare_page_after_navigation_async", new=AsyncMock()):
                path = Path(tmp) / "cookies.json"
                profile = await extract_account_profile_from_context(SimpleNamespace(new_page=AsyncMock(return_value=page)))
                from datetime import datetime
                save_credential_bundle(datetime.now().astimezone(), profile, [{"name": "test", "value": "test"}], cookies_path=path)
                with patch("core.auth.credential.COOKIES_FILE", path), patch("core.state.COOKIES_FILE", path):
                    identity = require_task_account()
                    self.assertEqual(identity, account_id())
                    if user_id == "identity-a":
                        first = identity
                    else:
                        self.assertNotEqual(first, identity)
            page.close.assert_awaited_once()

    async def test_no_identity_leaves_all_task_queues_untouched(self):
        from core.app.workflows import run_manual_course_selection
        with patch("core.auth.access.load_missing", create=True), patch("core.auth.credential.load_credential_metadata", return_value=None), patch("core.state.has_valid_credential", return_value=(False, True)), patch("core.app.workflows.append_learning_urls") as learn, patch("core.app.workflows.append_exam_urls") as exam, patch("core.app.workflows.remember_discovery_entries") as discover:
            with self.assertRaises(CredentialUnavailableError):
                await run_manual_course_selection("https://example.invalid/course")
            learn.assert_not_called()
            exam.assert_not_called()
            discover.assert_not_called()

    async def test_host_import_failure_closes_unstarted_coroutine(self):
        from core.runtime import run_operation
        async def work():
            pass
        coro = work()
        with patch("core.workflow.host.get_host", side_effect=ModuleNotFoundError("dbos")):
            with self.assertRaises(ModuleNotFoundError):
                run_operation(coro)
        self.assertIsNone(coro.cr_frame)

    def test_missing_dependency_reports_stderr_log_path_and_nonzero_exit(self):
        root = Path(__file__).resolve().parents[1]
        code = "import importlib.util,runpy; original=importlib.util.find_spec; importlib.util.find_spec=lambda name,*a,**k: None if name=='dbos' else original(name,*a,**k); runpy.run_path('launcher.py',run_name='__main__')"
        with TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable, "-X", "utf8", "-c", code], cwd=root, env={**os.environ, "COURSE_AFK_DATA_DIR": tmp}, capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 1)
            self.assertIn("dbos", result.stderr)
            self.assertIn("uv sync", result.stderr)
            self.assertIn(str(Path(tmp) / "logs/app-error.log"), result.stderr)
            self.assertNotIn("never awaited", result.stderr)

    def test_runtime_fatal_error_reaches_terminal_after_tui_returns(self):
        code = "import sys;sys.argv=['launcher.py'];import core.ui.tui_bridge as b;b.launch_tui=lambda: (_ for _ in ()).throw(RuntimeError('operation exploded'));import launcher;raise SystemExit(launcher.entrypoint())"
        with TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable, "-X", "utf8", "-c", code], cwd=Path(__file__).resolve().parents[1], env={**os.environ, "COURSE_AFK_DATA_DIR": tmp}, capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 1)
            self.assertIn("operation exploded", result.stderr)
            self.assertIn(str(Path(tmp) / "logs/app-error.log"), result.stderr)
