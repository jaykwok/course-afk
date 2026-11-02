import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from core.auth import login
from core.auth.credential import AccountProfile, extract_account_profile_from_context


class LoginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "cookies.json"
        self.path.write_text("old credential", encoding="utf-8")
        self.page = NS(
            goto=AsyncMock(),
            wait_for_url=AsyncMock(),
            locator=Mock(return_value=NS(content_frame=object())),
        )
        self.context = NS(
            new_page=AsyncMock(return_value=self.page),
            cookies=AsyncMock(
                return_value=[
                    {
                        "name": "authorization",
                        "value": "test",
                        "domain": "example.invalid",
                        "path": "/",
                    }
                ]
            ),
        )
        self.closed = False

        @asynccontextmanager
        async def browser(**kwargs):
            try:
                yield None, self.context
            finally:
                self.closed = True

        self.enterContext(patch.object(login, "create_browser_context", browser))
        self.enterContext(patch.object(login, "COOKIES_FILE", self.path))
        self.enterContext(
            patch.object(login, "prepare_page_after_navigation_async", new=AsyncMock())
        )
        self.enterContext(
            patch.object(
                login,
                "install_login_preferences_watcher",
                new=AsyncMock(return_value={}),
            )
        )
        self.profile = self.enterContext(
            patch.object(
                login,
                "extract_account_profile_from_context",
                new=AsyncMock(return_value=AccountProfile("Test", "account-a")),
            )
        )
        self.enterContext(patch.object(login, "_validate_pending_account"))

    async def test_success_commits_cookies_and_profile_together(self):
        profile = await login.login_and_save_credential()
        bundle = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(bundle["metadata"]["account_name"], profile.account_name)
        self.assertEqual(bundle["cookies"][0]["value"], "test")
        self.assertTrue(self.closed)

    async def test_profile_failure_preserves_old_credential(self):
        self.profile.side_effect = TimeoutError("profile unavailable")
        with self.assertRaises(login.LoginNotCompletedError):
            await login.login_and_save_credential()
        self.assertEqual(self.path.read_text(encoding="utf-8"), "old credential")
        self.assertTrue(self.closed)

    async def test_display_only_profile_cannot_replace_a_credential(self):
        self.profile.return_value = AccountProfile("显示名", "")
        with self.assertRaisesRegex(login.LoginNotCompletedError, "稳定账号标识"):
            await login.login_and_save_credential()
        self.assertEqual(self.path.read_text(encoding="utf-8"), "old credential")

    async def test_closed_login_returns_actionable_error(self):
        self.page.wait_for_url.side_effect = RuntimeError(
            "Target page, context or browser has been closed"
        )
        with self.assertRaisesRegex(login.LoginNotCompletedError, "未完成登录"):
            await login.login_and_save_credential()

    async def test_login_cancellation_preserves_credential_and_closes_context(self):
        self.page.wait_for_url.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await login.login_and_save_credential()
        self.assertTrue(self.closed)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "old credential")

    async def test_missing_cookies_cannot_commit_profile(self):
        self.context.cookies.return_value = []
        with self.assertRaises(login.LoginNotCompletedError):
            await login.login_and_save_credential()
        self.assertEqual(self.path.read_text(encoding="utf-8"), "old credential")

    async def test_profile_page_closes_on_evaluation_error(self):
        page = NS(
            goto=AsyncMock(),
            wait_for_url=AsyncMock(),
            wait_for_function=AsyncMock(),
            evaluate=AsyncMock(side_effect=RuntimeError("parse")),
            close=AsyncMock(),
        )
        context = NS(new_page=AsyncMock(return_value=page))
        with (
            patch(
                "core.auth.credential.prepare_page_after_navigation_async",
                new=AsyncMock(),
            ),
            self.assertRaises(RuntimeError),
        ):
            await extract_account_profile_from_context(context)
        page.close.assert_awaited_once()


class AccountBindingTests(unittest.TestCase):
    def test_pending_tasks_block_account_switch(self):
        state = NS(
            learning_count=1,
            learning_failure_count=0,
            exam_count=0,
            manual_exam_count=0,
        )
        old = NS(account_name="first", account_label="First")
        with (
            patch("core.state.collect_project_state", return_value=state),
            patch.object(login, "load_credential_metadata", return_value=old),
        ):
            with self.assertRaises(login.LoginNotCompletedError):
                login._validate_pending_account(AccountProfile("Second", "second"))

    def test_display_name_is_not_used_as_unique_identity(self):
        state = NS(
            learning_count=0,
            learning_failure_count=0,
            exam_count=1,
            manual_exam_count=0,
        )
        confirm = Mock(return_value=False)
        with (
            patch("core.state.collect_project_state", return_value=state),
            patch.object(
                login,
                "load_credential_metadata",
                return_value=NS(account_name="", account_label="Same name"),
            ),
        ):
            with self.assertRaises(login.LoginNotCompletedError):
                login._validate_pending_account(AccountProfile("Same name", ""))
        confirm.assert_not_called()
