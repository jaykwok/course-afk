import unittest
from unittest.mock import patch


class _FakeLocator:
    def __init__(self, wait_error=None):
        self._wait_error = wait_error

    async def wait_for(self, state="visible", timeout=0):
        if self._wait_error:
            raise self._wait_error


class _FakePage:
    def __init__(self, wait_error=None):
        self._wait_error = wait_error

    def locator(self, selector):
        if selector != ".single-btns":
            raise KeyError(selector)
        return _FakeLocator(wait_error=self._wait_error)


class DetectExamModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_detect_exam_mode_logs_clean_multi_mode_message_without_playwright_details(self):
        from core.exam_parsing import detect_exam_mode

        page = _FakePage(
            RuntimeError(
                'Locator.wait_for: Timeout 3000ms exceeded.\n'
                "Call log:\n"
                '  - waiting for locator(".single-btns") to be visible\n'
            )
        )

        with patch("core.exam_parsing.logging.info") as mock_info:
            result = await detect_exam_mode(page)

        self.assertEqual(result, "multi")
        mock_info.assert_called_once_with("检测为多题目模式(无下一题按钮)")


if __name__ == "__main__":
    unittest.main()
