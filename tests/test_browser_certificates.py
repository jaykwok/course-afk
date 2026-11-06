import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.abort import BrowserCertificateError
from core.browser import session


class CertificateErrorTests(unittest.IsolatedAsyncioTestCase):
    def test_known_certificate_failure_is_classified_without_disabling_verification(
        self,
    ):
        with self.assertRaises(BrowserCertificateError) as caught:
            session.raise_for_certificate_error(
                RuntimeError(
                    "Page.goto: net::ERR_CERT_AUTHORITY_INVALID at https://www.mylearning.cn/p5/index.html"
                )
            )
        self.assertEqual(caught.exception.code, "ERR_CERT_AUTHORITY_INVALID")
        self.assertIn("待办仍保留", str(caught.exception))
        self.assertFalse(
            session.build_browser_context_options(headless=False).get(
                "ignore_https_errors", False
            )
        )
        session.raise_for_certificate_error(RuntimeError("net::ERR_CONNECTION_RESET"))

    async def test_bootstrap_certificate_failure_cleans_up_and_remains_recoverable(
        self,
    ):
        context = SimpleNamespace(
            add_init_script=AsyncMock(), add_cookies=AsyncMock(), close=AsyncMock()
        )
        browser = SimpleNamespace(
            new_context=AsyncMock(return_value=context), contexts=[context], close=AsyncMock()
        )
        manager = SimpleNamespace(
            __aenter__=AsyncMock(return_value=object()), __aexit__=AsyncMock()
        )
        with (
            patch.object(session, "async_playwright", return_value=manager),
            patch.object(session, "own_playwright_driver", return_value=None),
            patch.object(session, "load_cookies", return_value=[]),
            patch.object(
                session, "launch_async_browser", new=AsyncMock(return_value=browser)
            ),
            patch(
                "core.browser.bootstrap.bootstrap_platform_session",
                new=AsyncMock(
                    side_effect=RuntimeError("net::ERR_CERT_AUTHORITY_INVALID")
                ),
            ),
        ):
            with self.assertRaises(BrowserCertificateError):
                async with session.create_browser_context(cookies_path="test-placeholder"):
                    self.fail("untrusted page cannot start an operation")
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()
        manager.__aexit__.assert_awaited_once()
