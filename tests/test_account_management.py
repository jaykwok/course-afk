import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from core import config
from core.auth import credential, login
from core.auth.credential import AccountProfile, clear_local_credentials, load_credential_metadata, load_pending_owner_metadata
from core.app.launcher_controller import handle_account_management, handle_clear_credentials, handle_show_login_state
from core.file_ops import load_cookies
from core.state import ProjectState


class AccountFixture(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cookies = self.root/'credentials/cookies.json'
        self.legacy = self.root/'credentials/credential_meta.json'
        self.cookies.parent.mkdir()
        self.metadata = {'saved_at':'2026-10-20T10:00:00+08:00','expires_at':'2099-12-31T10:00:00+08:00','account_name':'account-a','account_display_name':'测试甲','account_label':'测试甲（account-a）'}
        self.bundle = {'version':1,'cookies':[{'name':'authorization','value':'test-only-cookie-value'}], 'metadata':self.metadata}
        self.cookies.write_text(json.dumps(self.bundle), encoding='utf-8')
        self.legacy.write_text(json.dumps(self.metadata), encoding='utf-8')
        self.original = self.cookies.read_bytes()
        self.enterContext(patch.multiple(config, COOKIES_FILE=self.cookies, CREDENTIAL_META_FILE=self.legacy))
        self.enterContext(patch.multiple(credential, COOKIES_FILE=self.cookies, CREDENTIAL_META_FILE=self.legacy))
        self.pending = ProjectState(True, False, 1, 1, 1, 1)


class ClearCredentialTests(AccountFixture):
    def test_clear_removes_all_credentials_and_legacy_metadata_but_preserves_pending_owner(self):
        self.assertTrue(clear_local_credentials(preserve_pending_owner=True))
        self.assertEqual(load_cookies(self.cookies), [])
        self.assertIsNone(load_credential_metadata())
        self.assertFalse(self.legacy.exists())
        self.assertNotIn('test-only-cookie-value', self.cookies.read_text())
        self.assertEqual(load_pending_owner_metadata().account_name, 'account-a')
        self.assertEqual(list(self.cookies.parent.iterdir()), [self.cookies])

    def test_no_pending_tasks_retains_no_owner_and_repeated_clear_is_safe(self):
        clear_local_credentials()
        clear_local_credentials()
        self.assertIsNone(load_pending_owner_metadata())
        self.assertIsNone(load_credential_metadata())

    def test_legacy_cookie_array_uses_legacy_metadata_only_for_pending_owner(self):
        self.cookies.write_text(json.dumps(self.bundle['cookies']), encoding='utf-8')
        clear_local_credentials(preserve_pending_owner=True)
        self.assertEqual(load_pending_owner_metadata().account_name, 'account-a')
        self.assertEqual(load_cookies(self.cookies), [])

    def test_write_failure_keeps_original_credentials(self):
        with patch('core.auth.credential.write_json_atomic', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                clear_local_credentials(preserve_pending_owner=True)
        self.assertEqual(self.cookies.read_bytes(), self.original)
        self.assertTrue(self.legacy.exists())

    def test_clearing_does_not_bypass_pending_account_guard(self):
        clear_local_credentials(preserve_pending_owner=True)
        confirm = Mock(return_value=True)
        with patch('core.state.collect_project_state', return_value=self.pending):
            with self.assertRaises(login.LoginNotCompletedError):
                login._validate_pending_account(AccountProfile('测试乙', 'account-b'), confirm)
            login._validate_pending_account(AccountProfile('测试甲', 'account-a'), None)
        confirm.assert_not_called()


class FakeUi:
    def __init__(self, answer=False):
        self.answer = answer
        self.confirmations = []
        self.messages = []
        self.summaries = []
    def prompt_summary_confirmation(self, title, rows, message, default):
        self.confirmations.append((title, rows, default))
        return self.answer
    def show_info(self, text): self.messages.append(text)
    def show_error(self, text): self.messages.append(text)
    def show_success(self, text): self.messages.append(text)
    def show_summary(self, title, rows): self.summaries.append((title, rows))
    def pause(self): pass


class AccountControllerTests(AccountFixture):
    def test_cancel_is_default_and_keeps_credentials(self):
        ui = FakeUi()
        handle_clear_credentials(self.pending, ui)
        self.assertEqual(ui.confirmations[0][2], 'N')
        self.assertEqual(self.cookies.read_bytes(), self.original)

    def test_confirmed_clear_shows_logged_out_state_without_cookie_values(self):
        ui = FakeUi(True)
        handle_clear_credentials(self.pending, ui)
        handle_show_login_state(ProjectState(False, True, 1, 1, 1, 1), ui)
        rows = dict(ui.summaries[0][1])
        self.assertEqual(rows['登录状态'], '未登录')
        self.assertEqual(rows['待办归属账号'], self.metadata['account_label'])
        self.assertNotIn('test-only-cookie-value', str(ui.summaries))

    def test_account_menu_dispatch_and_zero_return(self):
        for choice in range(1, 6):
            with patch('core.app.launcher_controller.handle_refresh_credential') as refresh, patch('core.app.launcher_controller.handle_show_login_state') as show, patch('core.app.launcher_controller.handle_clear_credentials') as clear:
                ui = NS(prompt_choice=lambda *args:choice)
                handle_account_management(self.pending, ui)
                if choice == 1: refresh.assert_called_once_with(self.pending, ui, switch_account=True)
                elif choice == 2: refresh.assert_called_once_with(self.pending, ui)
                elif choice == 3: show.assert_called_once_with(self.pending, ui)
                elif choice == 4: clear.assert_called_once_with(self.pending, ui)
                else:
                    refresh.assert_not_called()
                    show.assert_not_called()
                    clear.assert_not_called()


class AccountTuiTests(AccountFixture):
    def test_clear_confirmation_and_return_refresh_account_status(self):
        from textual.widgets import Static
        from core.ui.tui_app import OptionScreen, PauseScreen, YesNoScreen
        from core.ui.tui_bridge import TuiFrontend
        from tests.test_tui_smoke import _CapturingApp, _wait_for

        async def scenario():
            def state():
                return ProjectState(bool(load_cookies(self.cookies)), False, 1, 1, 0, 0)
            with patch('core.config.setup_logging'), patch('core.state.collect_project_state', side_effect=state):
                app = _CapturingApp()
                frontend = TuiFrontend(app)
                frontend.install()
                try:
                    async with app.run_test(size=(90, 28)) as pilot:
                        def menu(title): return isinstance(app.screen, OptionScreen) and app.screen._title == title
                        self.assertTrue(await _wait_for(lambda:menu('主菜单'), pilot))
                        await pilot.press('6')
                        self.assertTrue(await _wait_for(lambda:menu('账号与登录'), pilot))
                        await pilot.press('0')
                        self.assertTrue(await _wait_for(lambda:menu('主菜单'), pilot))
                        await pilot.press('6')
                        self.assertTrue(await _wait_for(lambda:menu('账号与登录'), pilot))
                        await pilot.press('4')
                        self.assertTrue(await _wait_for(lambda:isinstance(app.screen, YesNoScreen), pilot))
                        self.assertEqual(app.focused.id, 'no')
                        await pilot.press('enter')
                        self.assertTrue(await _wait_for(lambda:menu('主菜单'), pilot))
                        self.assertEqual(self.cookies.read_bytes(), self.original)
                        await pilot.press('6')
                        self.assertTrue(await _wait_for(lambda:menu('账号与登录'), pilot))
                        await pilot.press('4')
                        self.assertTrue(await _wait_for(lambda:isinstance(app.screen, YesNoScreen), pilot))
                        await pilot.press('y')
                        self.assertTrue(await _wait_for(lambda:isinstance(app.screen, PauseScreen), pilot))
                        await pilot.press('enter')
                        self.assertTrue(await _wait_for(lambda:menu('主菜单'), pilot))
                        self.assertIn('未登录', str(app.screen.query_one('#opt-status', Static).render()))
                        self.assertEqual(load_cookies(self.cookies), [])
                        self.assertEqual(load_pending_owner_metadata().account_name, 'account-a')
                        await pilot.press('0')
                        self.assertTrue(await _wait_for(lambda:not app._test_worker.is_alive(), pilot))
                finally: frontend.restore()
                self.assertEqual(app._worker_result, 'ok')
        asyncio.run(asyncio.wait_for(scenario(), timeout=30))
