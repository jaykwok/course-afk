import unittest

from core.page_overlays import (
    DISMISS_TOPMOST_OVERLAY_SCRIPT,
    dismiss_topmost_overlays_async,
    dismiss_topmost_overlays_sync,
    prepare_page_after_navigation_async,
    prepare_page_after_navigation_sync,
)


class FakeAsyncPage:
    """按调用顺序返回 evaluate 结果；未预设时返回 None。"""

    def __init__(self, results=None):
        self.results = list(
            results
            if results is not None
            else [
                {"tag": "svg", "className": "close-button", "zIndex": 999999},
            ]
        )
        self.waits = []
        self.scripts = []

    async def evaluate(self, script):
        self.scripts.append(script)
        if self.results:
            return self.results.pop(0)
        return None

    async def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)


class FakeSyncPage:
    def __init__(self, results=None):
        self.results = list(
            results
            if results is not None
            else [
                {"tag": "svg", "className": "close-button", "zIndex": 999999},
            ]
        )
        self.waits = []
        self.scripts = []

    def evaluate(self, script):
        self.scripts.append(script)
        if self.results:
            return self.results.pop(0)
        return None

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)


class PageOverlayTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_handler_closes_shadow_dom_popup_and_stops(self):
        page = FakeAsyncPage()

        dismissed = await dismiss_topmost_overlays_async(page)

        self.assertEqual(dismissed, 1)
        self.assertEqual(page.waits, [200])
        self.assertEqual(page.scripts[0], DISMISS_TOPMOST_OVERLAY_SCRIPT)
        self.assertIn("shadowRoot", page.scripts[0])
        self.assertIn("zIndex", page.scripts[0])
        self.assertIn("MouseEvent", page.scripts[0])
        # 单脚本，不跑第二套文案匹配
        self.assertEqual(len(page.scripts), 2)  # close once + empty stop
        self.assertNotIn("AI学升级", DISMISS_TOPMOST_OVERLAY_SCRIPT)


class SyncPageOverlayTests(unittest.TestCase):
    def test_sync_handler_closes_shadow_dom_popup_and_stops(self):
        page = FakeSyncPage()

        dismissed = dismiss_topmost_overlays_sync(page)

        self.assertEqual(dismissed, 1)
        self.assertEqual(page.waits, [200])
        self.assertEqual(page.scripts[0], DISMISS_TOPMOST_OVERLAY_SCRIPT)

    def test_prepare_is_multi_round_wrapper_of_same_script(self):
        page = FakeSyncPage(results=[None, None])

        total = prepare_page_after_navigation_sync(
            page,
            rounds=5,
            between_round_milliseconds=50,
        )

        self.assertEqual(total, 0)
        # 两轮空：每轮只跑一套 DISMISS 脚本一次
        self.assertEqual(page.scripts, [DISMISS_TOPMOST_OVERLAY_SCRIPT] * 2)
        self.assertEqual(page.waits, [50])


class PrepareNavigationAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_closes_then_stops_on_empty(self):
        page = FakeAsyncPage(
            results=[
                {"tag": "i", "className": "close", "zIndex": 999},
                None,
                None,
            ]
        )

        total = await prepare_page_after_navigation_async(
            page,
            rounds=4,
            settle_milliseconds=10,
            between_round_milliseconds=20,
        )

        self.assertEqual(total, 1)
        self.assertTrue(all(s == DISMISS_TOPMOST_OVERLAY_SCRIPT for s in page.scripts))
        self.assertIn(10, page.waits)
        self.assertIn(20, page.waits)


if __name__ == "__main__":
    unittest.main()
