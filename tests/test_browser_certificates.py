import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.abort import BrowserCertificateError
from core.browser import session


class CertificateErrorTests(unittest.IsolatedAsyncioTestCase):
    def test_known_certificate_failure_is_classified_without_disabling_verification(self):
        with self.assertRaises(BrowserCertificateError) as caught:
            session.raise_for_certificate_error(RuntimeError("Page.goto: net::ERR_CERT_AUTHORITY_INVALID at https://www.mylearning.cn/p5/index.html"))
        self.assertEqual(caught.exception.code, "ERR_CERT_AUTHORITY_INVALID")
        self.assertIn("待办仍保留", str(caught.exception))
        self.assertFalse(session.build_browser_context_options(headless=False).get("ignore_https_errors", False))
        session.raise_for_certificate_error(RuntimeError("net::ERR_CONNECTION_RESET"))

    async def test_controller_certificate_failure_cleans_up_and_remains_recoverable(self):
        context = SimpleNamespace(add_init_script=AsyncMock(), add_cookies=AsyncMock(), close=AsyncMock())
        browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
        manager = SimpleNamespace(__aenter__=AsyncMock(return_value=object()), __aexit__=AsyncMock())
        with patch.object(session, "async_playwright", return_value=manager), patch.object(session, "own_playwright_driver", return_value=None), patch.object(session, "launch_async_browser", new=AsyncMock(return_value=browser)), patch.object(session, "_open_controller_page", new=AsyncMock(side_effect=RuntimeError("net::ERR_CERT_AUTHORITY_INVALID"))):
            with self.assertRaises(BrowserCertificateError):
                async with session.create_browser_context(cookies_path=None):
                    self.fail("untrusted page cannot start an operation")
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()
        manager.__aexit__.assert_awaited_once()

    def test_certificate_error_returns_to_main_menu_instead_of_exiting_application(self):
        import launcher
        state = SimpleNamespace(has_credential=True, credential_expired=False, learning_count=0, exam_count=1, manual_exam_count=0)
        with patch("core.config.setup_logging"), patch("core.state.collect_project_state", return_value=state), patch("core.ui.show_title"), patch("core.ui.render_dashboard"), patch("core.ui.prepare_menu_loading"), patch("core.ui.show_success"), patch("core.ui.show_menu", side_effect=[2, 7]), patch("core.ui.show_error") as show_error, patch("core.ui.pause"), patch("core.app.launcher_controller.handle_ai_exam", side_effect=BrowserCertificateError("ERR_CERT_AUTHORITY_INVALID")):
            self.assertEqual(launcher.main(), 0)
        show_error.assert_called_once()
        self.assertIn("ERR_CERT_AUTHORITY_INVALID", show_error.call_args.args[0])
