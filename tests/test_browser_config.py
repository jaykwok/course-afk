import asyncio
import unittest
from unittest.mock import AsyncMock

from core.browser import session as browser


class BrowserLaunchConfigTests(unittest.TestCase):
    def test_default_chromium_args_disable_automation_controlled_blink_feature(self):
        with (
            unittest.mock.patch.object(browser, "BROWSER_TYPE", "chromium"),
            unittest.mock.patch.object(browser, "BROWSER_CHANNEL", "msedge"),
        ):
            options = browser.build_browser_launch_options(headless=False)

        self.assertIn("--disable-blink-features=AutomationControlled", options["args"])

    def test_default_chromium_args_disable_local_network_access_checks(self):
        with (
            unittest.mock.patch.object(browser, "BROWSER_TYPE", "chromium"),
            unittest.mock.patch.object(browser, "BROWSER_CHANNEL", "msedge"),
        ):
            options = browser.build_browser_launch_options(headless=False)

        disable_features = next(
            (
                arg
                for arg in options["args"]
                if arg.startswith("--disable-features=")
            ),
            "",
        )
        self.assertIn("LocalNetworkAccessChecks", disable_features)
        self.assertIn("BlockInsecurePrivateNetworkRequests", disable_features)

    def test_build_browser_launch_options_uses_channel_for_chromium(self):
        with (
            unittest.mock.patch.object(browser, "BROWSER_TYPE", "chromium"),
            unittest.mock.patch.object(browser, "BROWSER_CHANNEL", "msedge"),
            unittest.mock.patch.object(browser, "BROWSER_ARGS", ["--mute-audio"]),
        ):
            options = browser.build_browser_launch_options(headless=False, slow_mo=300)

        self.assertEqual(options["channel"], "msedge")
        self.assertEqual(options["args"], ["--mute-audio", "--start-maximized"])
        self.assertEqual(options["slow_mo"], 300)
        self.assertFalse(options["headless"])

    def test_build_browser_launch_options_skips_channel_and_args_for_webkit(self):
        with (
            unittest.mock.patch.object(browser, "BROWSER_TYPE", "webkit"),
            unittest.mock.patch.object(browser, "BROWSER_CHANNEL", "safari"),
            unittest.mock.patch.object(
                browser, "BROWSER_ARGS", ["--mute-audio", "--start-maximized"]
            ),
        ):
            options = browser.build_browser_launch_options(headless=False)

        self.assertEqual(options, {"headless": False})

    def test_build_browser_context_options_uses_no_viewport_for_visible_browser(self):
        self.assertEqual(
            browser.build_browser_context_options(headless=False),
            {"no_viewport": True},
        )

    def test_browser_configuration_rejects_headless_mode(self):
        with self.assertRaisesRegex(ValueError, "禁止使用 headless"):
            browser.build_browser_launch_options(headless=True)
        with self.assertRaisesRegex(ValueError, "禁止使用 headless"):
            browser.build_browser_context_options(headless=True)

    def test_launch_async_browser_uses_selected_browser_type(self):
        fake_browser = object()
        fake_launcher = type("FakeLauncher", (), {"launch": AsyncMock(return_value=fake_browser)})()
        fake_playwright = type(
            "FakePlaywright",
            (),
            {"webkit": fake_launcher},
        )()

        with (
            unittest.mock.patch.object(browser, "BROWSER_TYPE", "webkit"),
            unittest.mock.patch.object(browser, "BROWSER_CHANNEL", None),
            unittest.mock.patch.object(browser, "BROWSER_ARGS", []),
        ):
            launched = asyncio.run(browser.launch_async_browser(fake_playwright, headless=False))

        self.assertIs(launched, fake_browser)
        fake_launcher.launch.assert_awaited_once_with(headless=False)


class BrowserFeatureSwitchTests(unittest.TestCase):
    def test_disable_features_switch_appears_only_once(self):
        """Chromium 的 --disable-features 重名后到者覆盖前者：我们只能给一个，
        且必须把 Playwright 的默认禁用项一起带上，否则它那份会被顶掉。"""
        from core.config import (
            _PLAYWRIGHT_DISABLED_FEATURES,
            _PROJECT_DISABLED_FEATURES,
        )

        with (
            unittest.mock.patch.object(browser, "BROWSER_TYPE", "chromium"),
            unittest.mock.patch.object(browser, "BROWSER_CHANNEL", "msedge"),
        ):
            options = browser.build_browser_launch_options(headless=False)

        disable_switches = [
            arg for arg in options["args"] if arg.startswith("--disable-features=")
        ]
        self.assertEqual(len(disable_switches), 1)

        features = set(disable_switches[0].split("=", 1)[1].split(","))
        self.assertTrue(set(_PROJECT_DISABLED_FEATURES) <= features)
        self.assertTrue(set(_PLAYWRIGHT_DISABLED_FEATURES) <= features)


class SlowMoSamplingTests(unittest.TestCase):
    def test_sample_afk_slow_mo_varies_inside_configured_range(self):
        from core.config import AFK_SLOW_MO_MAX, AFK_SLOW_MO_MIN, sample_afk_slow_mo

        samples = [sample_afk_slow_mo() for _ in range(200)]

        self.assertTrue(
            all(AFK_SLOW_MO_MIN <= value <= AFK_SLOW_MO_MAX for value in samples)
        )
        # 固定 slow_mo 正是要改掉的东西：200 次取样不该只有一个值
        self.assertGreater(len(set(samples)), 20)


class BrowserStealthTests(unittest.IsolatedAsyncioTestCase):
    def test_apply_sync_browser_stealth_adds_init_script(self):
        class FakeContext:
            def __init__(self):
                self.scripts = []

            def add_init_script(self, script):
                self.scripts.append(script)

        context = FakeContext()

        browser.apply_sync_browser_stealth(context)

        self.assertEqual(len(context.scripts), 1)
        self.assertIn("webdriver", context.scripts[0])

    def test_stealth_script_covers_the_three_known_leaks(self):
        """webdriver 本身、补丁函数的 toString、同源子 frame 三处都要盖住。

        只断言意图，不锁具体写法：脚本行为由 stealth 的实机验证兜底。
        """
        script = browser.BROWSER_STEALTH_INIT_SCRIPT

        self.assertIn("webdriver", script)
        self.assertIn("[native code]", script)
        self.assertIn("contentWindow", script)

    def test_stealth_script_does_not_define_own_property_on_navigator(self):
        """实例上的 own property 是旧写法留下的破绽：
        真实 Chrome 里 webdriver 只挂在 Navigator.prototype 上。"""
        script = browser.BROWSER_STEALTH_INIT_SCRIPT

        self.assertNotIn('Object.defineProperty(navigator, "webdriver"', script)
        self.assertIn("delete nav.webdriver", script)

    async def test_apply_async_browser_stealth_adds_init_script(self):
        class FakeContext:
            def __init__(self):
                self.scripts = []

            async def add_init_script(self, script):
                self.scripts.append(script)

        context = FakeContext()

        await browser.apply_async_browser_stealth(context)

        self.assertEqual(len(context.scripts), 1)
        self.assertIn("webdriver", context.scripts[0])


class BrowserControllerPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_ensure_controller_page_reuses_existing_open_page(self):
        class FakeBrowser:
            def is_connected(self):
                return True

        class FakePage:
            def __init__(self):
                self.goto_calls = []
                self.closed = False
                self.handlers = {}

            async def goto(self, url, wait_until="load"):
                self.goto_calls.append((url, wait_until))

            async def evaluate(self, _script):
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

            def is_closed(self):
                return self.closed

            def on(self, event, handler):
                self.handlers[event] = handler

        class FakeContext:
            def __init__(self):
                self.browser = FakeBrowser()
                self.pages = []

            async def new_page(self):
                page = FakePage()
                self.pages.append(page)
                return page

        context = FakeContext()
        try:
            first_page = await browser.ensure_controller_page(context)
            second_page = await browser.ensure_controller_page(context)
        finally:
            browser.release_controller_page(context)

        self.assertIs(first_page, second_page)
        self.assertEqual(len(context.pages), 1)
        self.assertEqual(
            first_page.goto_calls,
            [(browser.MYLEARNING_HOME, "load")],
        )

    async def test_controller_page_close_marks_window_closed(self):
        """心跳页语义：mylearning 主控页被关 = 整窗被关。置位闩锁后
        is_browser_connected 短路为 False（覆盖 Edge 后台进程仍存活的情形），
        ensure_controller_page 返回 None 而不是重开。"""

        class FakeBrowser:
            def is_connected(self):
                return True  # 模拟 Edge 后台模式：窗口关了进程还在

        class FakePage:
            def __init__(self):
                self.goto_calls = []
                self.closed = False
                self.handlers = {}

            async def goto(self, url, wait_until="load"):
                self.goto_calls.append((url, wait_until))

            async def evaluate(self, _script):
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

            def is_closed(self):
                return self.closed

            def on(self, event, handler):
                self.handlers[event] = handler

            async def close(self):
                self.closed = True
                close_handler = self.handlers.get("close")
                if close_handler is not None:
                    close_handler()

        class FakeContext:
            def __init__(self):
                self.browser = FakeBrowser()
                self.pages = []

            async def new_page(self):
                page = FakePage()
                self.pages.append(page)
                return page

        context = FakeContext()
        try:
            first_page = await browser.ensure_controller_page(context)
            self.assertFalse(browser.is_controller_window_closed(context))
            self.assertTrue(browser.is_browser_connected(context))

            await first_page.close()  # 用户关闭整窗 → 心跳页 close 事件

            self.assertTrue(browser.is_controller_window_closed(context))
            self.assertFalse(
                browser.is_browser_connected(context),
                "心跳页关闭后连接判断必须短路为 False（Edge 后台模式）",
            )
            second_page = await browser.ensure_controller_page(context)
            self.assertIsNone(
                second_page, "心跳页被关后不得自动重开"
            )
            self.assertEqual(len(context.pages), 1)
        finally:
            browser.release_controller_page(context)

        self.assertFalse(browser.is_controller_window_closed(context))

    async def test_context_manager_cleanup_removes_close_callback_latch(self):
        """正常退出关闭 context 时，心跳页回调虽会置位闩锁，但上下文管理器
        必须在回调之后清理全部以 context id 为键的注册数据。"""

        events = []

        class FakePage:
            def __init__(self):
                self.handlers = {}
                self.closed = False

            async def goto(self, _url, wait_until="load"):
                return None

            def on(self, event, handler):
                self.handlers[event] = handler

            def is_closed(self):
                return self.closed

            async def close(self):
                self.closed = True
                handler = self.handlers.get("close")
                if handler is not None:
                    handler()

        class FakeContext:
            def __init__(self):
                self.pages = []

            async def add_init_script(self, _script):
                return None

            async def add_cookies(self, _cookies):
                return None

            async def new_page(self):
                page = FakePage()
                self.pages.append(page)
                return page

            async def close(self):
                events.append("context.close")
                for page in list(self.pages):
                    await page.close()

        context = FakeContext()

        class FakeBrowser:
            async def new_context(self, **_kwargs):
                return context

            async def close(self):
                events.append("browser.close")

        fake_browser = FakeBrowser()

        class FakePlaywrightManager:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, _exc_type, _exc, _tb):
                return None

        with (
            unittest.mock.patch.object(
                browser, "async_playwright", return_value=FakePlaywrightManager()
            ),
            unittest.mock.patch.object(
                browser,
                "launch_async_browser",
                new=AsyncMock(return_value=fake_browser),
            ),
            unittest.mock.patch.object(browser, "load_cookies", return_value=[]),
            unittest.mock.patch.object(
                browser,
                "prepare_page_after_navigation_async",
                new=AsyncMock(),
            ),
        ):
            async with browser.create_browser_context() as (_browser, active_context):
                context_key = id(active_context)
                self.assertIn(context_key, browser._CONTROLLER_PAGES)
                self.assertIn(context_key, browser._CONTEXT_HEADLESS)

        self.assertEqual(events, ["context.close", "browser.close"])
        self.assertNotIn(context_key, browser._CONTROLLER_PAGES)
        self.assertNotIn(context_key, browser._CONTEXT_HEADLESS)
        self.assertNotIn(context_key, browser._CONTEXT_WINDOW_CLOSED)


if __name__ == "__main__":
    unittest.main()
