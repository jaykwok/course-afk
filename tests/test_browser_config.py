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
        self.assertIn("NetworkTimeServiceQuerying", features)
        self.assertIn("AimEnabled", features)
        self.assertIn("AvoidCorsURLLoaderRestartOnRedirect", features)
        self.assertNotIn("BoundaryEventDispatchTracksNodeRemoval", features)


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




if __name__ == "__main__":
    unittest.main()
