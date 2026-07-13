import unittest

from core.page_overlays import (
    DISMISS_TOPMOST_OVERLAY_SCRIPT,
    dismiss_topmost_overlays_async,
    dismiss_topmost_overlays_sync,
)


class FakeAsyncPage:
    def __init__(self):
        self.results = [
            {"tag": "svg", "className": "close-button", "zIndex": 999999},
            None,
        ]
        self.waits = []

    async def evaluate(self, script):
        self.script = script
        return self.results.pop(0)

    async def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)


class FakeSyncPage:
    def __init__(self):
        self.results = [
            {"tag": "svg", "className": "close-button", "zIndex": 999999},
            None,
        ]
        self.waits = []

    def evaluate(self, script):
        self.script = script
        return self.results.pop(0)

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)


class PageOverlayTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_handler_closes_shadow_dom_popup_and_stops(self):
        page = FakeAsyncPage()

        dismissed = await dismiss_topmost_overlays_async(page)

        self.assertEqual(dismissed, 1)
        self.assertEqual(page.waits, [200])
        self.assertIn("shadowRoot", page.script)
        self.assertIn("zIndex", page.script)
        self.assertIn("MouseEvent", page.script)


class SyncPageOverlayTests(unittest.TestCase):
    def test_sync_handler_closes_shadow_dom_popup_and_stops(self):
        page = FakeSyncPage()

        dismissed = dismiss_topmost_overlays_sync(page)

        self.assertEqual(dismissed, 1)
        self.assertEqual(page.waits, [200])
        self.assertEqual(page.script, DISMISS_TOPMOST_OVERLAY_SCRIPT)


if __name__ == "__main__":
    unittest.main()
