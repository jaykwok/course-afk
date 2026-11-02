import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from core.auth.access import CredentialUnavailableError, require_task_account
from core.auth.credential import (
    extract_account_profile_from_context,
    save_credential_bundle,
)
from core.tasks.identity import account_id


class StartupAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_account_can_add_links_without_browser_or_orphan_tasks(
        self,
    ):
        from datetime import datetime
        from core.auth.credential import AccountProfile
        from core.app import workflows
        from core.queues.learning import read_learning_urls

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "cookies.json"
            queue = root / "课程链接.json"
            save_credential_bundle(
                datetime.now().astimezone(),
                AccountProfile("账号", "center:verified"),
                [{"name": "test", "value": "test"}],
                cookies_path=path,
            )
            with (
                patch("core.auth.credential.COOKIES_FILE", path),
                patch("core.state.COOKIES_FILE", path),
                patch.object(workflows, "LEARNING_URLS_FILE", queue),
            ):
                course = "https://kc.zhixueyun.com/#/study/course/detail/11111111-1111-4111-8111-111111111111"
                result = await workflows.run_manual_course_selection(course)
                self.assertEqual(result["direct_learning_count"], 1)
                self.assertEqual(read_learning_urls(queue), [course])

    def test_pending_owner_still_blocks_account_switch_after_credentials_cleared(self):
        from datetime import datetime
        from core.auth import login
        from core.auth.credential import AccountProfile, clear_local_credentials
        from core.tasks.repository import TaskRepository
        from core.queues.learning import append_learning_urls

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "cookies.json"
            repo = TaskRepository(root / "state.sqlite")
            save_credential_bundle(
                datetime.now().astimezone(),
                AccountProfile("同名", "center:one"),
                [{"name": "test", "value": "test"}],
                cookies_path=path,
            )
            with (
                patch("core.auth.credential.COOKIES_FILE", path),
                patch("core.state.COOKIES_FILE", path),
                patch("core.tasks.store.repository_for", return_value=repo),
            ):
                append_learning_urls(
                    ["https://example.invalid/pending"],
                    file_path=root / "课程链接.json",
                )
                clear_local_credentials(preserve_pending_owner=True, cookies_path=path)
                with self.assertRaises(login.LoginNotCompletedError):
                    login._validate_pending_account(
                        AccountProfile("同名", "center:two")
                    )

    def test_fatal_worker_error_survives_cleanup_and_restores_log_handlers(self):
        from contextlib import redirect_stderr
        import io
        import logging
        from unittest.mock import Mock
        from core.ui import tui_bridge

        with TemporaryDirectory() as tmp:
            app = SimpleNamespace(
                fatal_error=RuntimeError("worker exploded"),
                _launcher_thread=None,
                _exit_status=1,
                run=Mock(return_value=1),
                exit=Mock(),
            )
            handlers = list(logging.getLogger().handlers)
            output = io.StringIO()
            with (
                patch.object(tui_bridge, "CourseTuiApp", return_value=app),
                patch.object(tui_bridge, "setup_logging"),
                patch("core.config.DATA_DIR", Path(tmp)),
                patch("core.config.ERROR_LOG_FILE", Path(tmp) / "logs/app-error.log"),
                patch("core.runtime.protect_application_children"),
                patch(
                    "core.startup.shutdown_workflows",
                    side_effect=ModuleNotFoundError("dbos"),
                ),
                redirect_stderr(output),
            ):
                self.assertEqual(tui_bridge.launch_tui(), 1)
            self.assertIn("worker exploded", output.getvalue())
            self.assertIn(str(Path(tmp) / "logs/app-error.log"), output.getvalue())
            self.assertEqual(logging.getLogger().handlers, handlers)

    def test_recoverable_identity_error_returns_to_menu(self):
        import launcher
        from unittest.mock import Mock

        ui = Mock()
        ui.show_menu.side_effect = [3, 7]
        with (
            patch("core.config.setup_logging"),
            patch("core.state.collect_project_state", return_value=SimpleNamespace()),
            patch(
                "core.app.launcher_controller.handle_manual_selection",
                side_effect=CredentialUnavailableError("请重新登录"),
            ),
        ):
            self.assertEqual(launcher.main(ui), 0)
        ui.show_warning.assert_called_once_with("请重新登录")
        ui.pause.assert_called_once()

    async def test_real_profile_extraction_produces_executable_distinct_identities(
        self,
    ):
        for user_id in ("identity-a", "identity-b"):
            page = SimpleNamespace(
                goto=AsyncMock(),
                wait_for_url=AsyncMock(),
                wait_for_function=AsyncMock(),
                evaluate=AsyncMock(return_value={"id": user_id, "name": "相同显示名"}),
                close=AsyncMock(),
            )
            with (
                TemporaryDirectory() as tmp,
                patch(
                    "core.auth.credential.prepare_page_after_navigation_async",
                    new=AsyncMock(),
                ),
            ):
                path = Path(tmp) / "cookies.json"
                profile = await extract_account_profile_from_context(
                    SimpleNamespace(new_page=AsyncMock(return_value=page))
                )
                from datetime import datetime

                save_credential_bundle(
                    datetime.now().astimezone(),
                    profile,
                    [{"name": "test", "value": "test"}],
                    cookies_path=path,
                )
                with (
                    patch("core.auth.credential.COOKIES_FILE", path),
                    patch("core.state.COOKIES_FILE", path),
                ):
                    identity = require_task_account()
                    self.assertEqual(identity, account_id())
                    if user_id == "identity-a":
                        first = identity
                    else:
                        self.assertNotEqual(first, identity)
            page.close.assert_awaited_once()

    async def test_no_identity_leaves_all_task_queues_untouched(self):
        from core.app.workflows import run_manual_course_selection

        with (
            patch("core.auth.access.load_missing", create=True),
            patch("core.auth.credential.load_credential_metadata", return_value=None),
            patch("core.state.has_valid_credential", return_value=(False, True)),
            patch("core.app.workflows.append_learning_urls") as learn,
            patch("core.app.workflows.append_exam_urls") as exam,
            patch("core.app.workflows.remember_discovery_entries") as discover,
        ):
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
        with patch(
            "core.workflow.host.get_host", side_effect=ModuleNotFoundError("dbos")
        ):
            with self.assertRaises(ModuleNotFoundError):
                run_operation(coro)
        self.assertIsNone(coro.cr_frame)

    def test_missing_dependency_reports_stderr_log_path_and_nonzero_exit(self):
        root = Path(__file__).resolve().parents[1]
        code = "import importlib.util,runpy; original=importlib.util.find_spec; importlib.util.find_spec=lambda name,*a,**k: None if name=='dbos' else original(name,*a,**k); runpy.run_path('launcher.py',run_name='__main__')"
        with TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, "-X", "utf8", "-c", code],
                cwd=root,
                env={**os.environ, "COURSE_AFK_DATA_DIR": tmp},
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("dbos", result.stderr)
            self.assertIn("uv sync", result.stderr)
            self.assertIn(str(Path(tmp) / "logs/app-error.log"), result.stderr)
            self.assertNotIn("never awaited", result.stderr)

    def test_runtime_fatal_error_reaches_terminal_after_tui_returns(self):
        code = "import sys;sys.argv=['launcher.py'];import core.ui.tui_bridge as b;b.launch_tui=lambda: (_ for _ in ()).throw(RuntimeError('operation exploded'));import launcher;raise SystemExit(launcher.entrypoint())"
        with TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, "-X", "utf8", "-c", code],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, "COURSE_AFK_DATA_DIR": tmp},
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("operation exploded", result.stderr)
            self.assertIn(str(Path(tmp) / "logs/app-error.log"), result.stderr)
