import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch


class FakeLoginFrame:
    def __init__(self):
        self.evaluate_all_calls = []
        self.evaluate_calls = []

    @property
    def content_frame(self):
        return self

    def locator(self, _selector):
        return self

    def wait_for(self, **_kwargs):
        return None

    def click(self):
        return None

    def evaluate_all(self, script, data_time=None):
        self.evaluate_all_calls.append((script, data_time))
        if data_time is None:
            return 4
        return 3

    def evaluate(self, script, data_time=None):
        self.evaluate_calls.append((script, data_time))
        return {"selectedCount": 3, "checkedCount": 4}


class FakeLoginPage:
    def __init__(self, close_during_login=False):
        self.frame = FakeLoginFrame()
        self.close_during_login = close_during_login
        self.wait_for_url_calls = 0

    def goto(self, _url):
        return None

    def wait_for_url(self, _pattern, timeout=0):
        self.wait_for_url_calls += 1
        if self.close_during_login and self.wait_for_url_calls > 1:
            raise TargetClosedError("Target page, context or browser has been closed")
        return None

    def locator(self, _selector):
        return self.frame

    def close(self):
        return None


class FakeLoginContext:
    def __init__(self, close_during_login=False):
        self.page = FakeLoginPage(close_during_login=close_during_login)
        self.init_scripts = []

    def new_page(self):
        return self.page

    def add_init_script(self, script):
        self.init_scripts.append(script)

    def cookies(self):
        return []

    def close(self):
        return None


class FakeLoginBrowser:
    def __init__(self, close_during_login=False):
        self.new_context_calls = []
        self.context = FakeLoginContext(close_during_login=close_during_login)

    def new_context(self, **kwargs):
        self.new_context_calls.append(kwargs)
        return self.context

    def close(self):
        return None


class TargetClosedError(Exception):
    pass


class LoginTests(unittest.TestCase):
    def test_login_uses_no_viewport_context_for_visible_browser(self):
        from core.login import login_and_save_credential

        fake_browser = FakeLoginBrowser()
        fake_profile = SimpleNamespace(
            full_name="测试账号",
            account_name="tester",
            label="测试账号（tester）",
        )

        with TemporaryDirectory() as tmp:
            cookies_file = Path(tmp) / "cookies.json"
            with (
                patch("core.login.COOKIES_FILE", cookies_file),
                patch("core.login.launch_sync_browser", return_value=fake_browser),
                patch("core.login.extract_account_profile_from_sync_context", return_value=fake_profile),
                patch("core.login.save_credential_metadata"),
                patch("core.login.sync_playwright"),
            ):
                profile = login_and_save_credential()

        self.assertEqual(profile.label, fake_profile.label)
        self.assertEqual(fake_browser.new_context_calls, [{"no_viewport": True}])
        self.assertEqual(len(fake_browser.context.init_scripts), 1)
        self.assertIn("webdriver", fake_browser.context.init_scripts[0])
        self.assertEqual(fake_browser.context.page.frame.evaluate_calls[0][1], "3")
        self.assertIn(
            "setInterval",
            fake_browser.context.page.frame.evaluate_calls[0][0],
        )

    def test_login_saves_credentials_when_profile_lookup_times_out(self):
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from core.login import login_and_save_credential

        fake_browser = FakeLoginBrowser()

        with TemporaryDirectory() as tmp:
            cookies_file = Path(tmp) / "cookies.json"
            with (
                patch("core.login.COOKIES_FILE", cookies_file),
                patch("core.login.launch_sync_browser", return_value=fake_browser),
                patch(
                    "core.login.extract_account_profile_from_sync_context",
                    side_effect=PlaywrightTimeoutError("blank zhixueyun page"),
                ),
                patch("core.login.save_credential_metadata") as mock_save_metadata,
                patch("core.login.sync_playwright"),
            ):
                profile = login_and_save_credential()
                self.assertTrue(cookies_file.exists())

        self.assertEqual(profile.label, "未知账号")
        mock_save_metadata.assert_called_once()

    def test_login_closed_before_success_raises_clear_error(self):
        from core.login import LoginNotCompletedError, login_and_save_credential

        fake_browser = FakeLoginBrowser(close_during_login=True)

        with (
            patch("core.login.launch_sync_browser", return_value=fake_browser),
            patch("core.login.sync_playwright"),
        ):
            with self.assertRaises(LoginNotCompletedError) as ctx:
                login_and_save_credential()

        self.assertEqual(str(ctx.exception), "已手动关闭浏览器，未完成登录，登录凭证未更新")


if __name__ == "__main__":
    unittest.main()
