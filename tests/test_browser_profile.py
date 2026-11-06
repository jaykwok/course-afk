"""Browser-launch safety checks; all launch APIs are mocked, never start Edge."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from core.browser import session
from core.browser.profile import (
    PASSWORD_CHECK_CACHE_TIME, seed_automation_profile, validate_automation_profile,
)


class ProfileSeedTests(unittest.TestCase):
    def test_cache_includes_false_and_chromium_int64_timestamp(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp)
            seed_automation_profile(path)
            validate_automation_profile(path)
            state = json.loads((path / "Local State").read_text())
            self.assertIs(state["password_manager"]["os_password_blank"], False)
            self.assertEqual(state["password_manager"]["os_password_last_changed"], PASSWORD_CHECK_CACHE_TIME)
            self.assertGreater(int(PASSWORD_CHECK_CACHE_TIME), 13_000_000_000_000_000)
            preferences = json.loads((path / "Default/Preferences").read_text())
            self.assertIs(preferences["credentials_enable_service"], False)

    def test_boolean_only_or_invalid_cache_is_rejected(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp)
            with self.assertRaises(RuntimeError):
                validate_automation_profile(path)
            for content in ('invalid', '{}', '{"password_manager":{"os_password_blank":false}}',
                            '{"password_manager":{"os_password_blank":true,"os_password_last_changed":"9223372036854775807"}}'):
                (path / "Local State").write_text(content)
                with self.subTest(content=content), self.assertRaises(RuntimeError):
                    validate_automation_profile(path)

    def test_existing_profile_is_never_overwritten(self):
        with TemporaryDirectory() as tmp:
            state = Path(tmp) / "Local State"
            state.write_text('{"personal":"preserve"}')
            with self.assertRaises(FileExistsError):
                seed_automation_profile(Path(tmp))
            self.assertEqual(state.read_text(), '{"personal":"preserve"}')


class SafeLaunchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.object(session.sys, "platform", "win32"))
        self.enterContext(patch.object(session, "BROWSER_TYPE", "chromium"))
        self.browser = SimpleNamespace(close=AsyncMock(), new_context=AsyncMock())
        self.context = SimpleNamespace(browser=self.browser, pages=[], close=AsyncMock(),
                                       add_cookies=AsyncMock(), add_init_script=AsyncMock())
        self.browser.contexts = [self.context]
        self.launcher = SimpleNamespace(launch=AsyncMock(),
            launch_persistent_context=AsyncMock(return_value=self.context))
        self.playwright = SimpleNamespace(chromium=self.launcher)

    async def test_windows_launch_requires_seed_and_never_falls_back(self):
        with self.assertRaises(RuntimeError):
            await session.launch_async_browser(self.playwright, headless=False)
        with TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                await session.launch_async_browser(self.playwright, headless=False, user_data_dir=tmp)
            seed_automation_profile(Path(tmp))
            launched = await session.launch_async_browser(self.playwright, headless=False, user_data_dir=tmp)
            self.assertIs(launched, self.browser)
            self.assertEqual(self.launcher.launch_persistent_context.await_args.args, (tmp,))
            self.assertTrue(self.launcher.launch_persistent_context.await_args.kwargs["no_viewport"])
            self.launcher.launch_persistent_context.side_effect = RuntimeError("launch failed")
            with self.assertRaisesRegex(RuntimeError, "launch failed"):
                await session.launch_async_browser(self.playwright, headless=False, user_data_dir=tmp)
        self.launcher.launch.assert_not_awaited()

    async def test_profile_seed_precedes_driver_and_cleanup_follows_termination(self):
        paths = []

        async def launch(directory, **options):
            path = Path(directory)
            validate_automation_profile(path)
            paths.append(path)
            return self.context

        async def terminate():
            self.assertTrue(paths[-1].is_dir())

        self.launcher.launch_persistent_context.side_effect = launch
        manager = SimpleNamespace(__aenter__=AsyncMock(return_value=self.playwright), __aexit__=AsyncMock())
        job = SimpleNamespace(capture=lambda: None, terminate=AsyncMock(side_effect=terminate))
        with (patch.object(session, "async_playwright", return_value=manager),
              patch.object(session, "own_playwright_driver", return_value=job)):
            for _ in range(2):
                async with session.create_isolated_browser_context(cookies_path=None) as pair:
                    self.assertEqual(pair, (self.browser, self.context))
                    self.assertTrue(paths[-1].is_dir())
                self.assertFalse(paths[-1].exists())
        self.assertNotEqual(paths[0], paths[1])
        self.browser.new_context.assert_not_awaited()
        self.assertEqual(self.context.close.await_count, 2)
        self.assertEqual(job.terminate.await_count, 2)

    async def test_failed_or_cancelled_launch_cleans_profile(self):
        for error in (RuntimeError("failed"), asyncio.CancelledError()):
            paths = []

            async def launch(directory, **options):
                paths.append(Path(directory))
                raise error

            self.launcher.launch_persistent_context.side_effect = launch
            manager = SimpleNamespace(__aenter__=AsyncMock(return_value=self.playwright), __aexit__=AsyncMock())
            job = SimpleNamespace(capture=lambda: None, terminate=AsyncMock())
            with (self.subTest(error=type(error).__name__),
                  patch.object(session, "async_playwright", return_value=manager),
                  patch.object(session, "own_playwright_driver", return_value=job),
                  self.assertRaises(type(error))):
                async with session.create_isolated_browser_context(cookies_path=None):
                    self.fail("failed launch cannot yield")
            self.assertFalse(paths[0].exists())
            job.terminate.assert_awaited_once()
        self.launcher.launch.assert_not_awaited()

    async def test_seed_write_failure_never_starts_driver_or_browser(self):
        manager = SimpleNamespace(__aenter__=AsyncMock(), __aexit__=AsyncMock())
        with (patch.object(session, "async_playwright", return_value=manager),
              patch.object(session, "own_playwright_driver", return_value=None),
              patch("core.browser.profile.seed_automation_profile", side_effect=OSError("disk full")),
              self.assertRaises(OSError)):
            async with session.create_isolated_browser_context(cookies_path=None):
                self.fail("seed failure cannot yield")
        manager.__aenter__.assert_not_awaited()
        self.launcher.launch.assert_not_awaited()
        self.launcher.launch_persistent_context.assert_not_awaited()

    async def test_application_reuses_initial_tab_and_creates_one_only_when_absent(self):
        from contextlib import asynccontextmanager
        from unittest.mock import Mock
        from core.browser import application

        for initial in (False, True):
            page = object()
            self.context.pages = [page] if initial else []
            self.context.new_page = AsyncMock(return_value=page)
            self.context.on = Mock()
            self.context.remove_listener = Mock()

            @asynccontextmanager
            async def isolated(**kwargs):
                yield self.browser, self.context

            with self.subTest(initial=initial), patch.object(session, "create_isolated_browser_context", isolated):
                async with application.application_browser() as owner:
                    self.assertIs(owner.management, page)
                    self.assertIs(owner.context, self.context)
                self.assertEqual(self.context.new_page.await_count, int(not initial))
                self.assertIsNone(application.current_browser())
