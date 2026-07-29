import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch


class ExamPageProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_redirect_failure_is_recorded_without_parsing_page(self):
        from tools import probe_exam_page

        class FakePage:
            def __init__(self):
                self.url = "https://kc.zhixueyun.com/oauth/#login/token"
                self.main_frame = object()
                self.locator = Mock(
                    side_effect=AssertionError("login page DOM must not be parsed")
                )

            def on(self, _event, _handler):
                return None

            async def goto(self, _url, wait_until=None):
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

        class FakeContext:
            def __init__(self):
                self.page = FakePage()

            async def new_page(self):
                return self.page

            async def cookies(self):
                return [{"name": "session", "domain": ".zhixueyun.com"}]

        class FakeBrowserContextManager:
            def __init__(self):
                self.context = FakeContext()

            async def __aenter__(self):
                return object(), self.context

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with TemporaryDirectory() as tmp:
            capture_dir = Path(tmp) / "exam_page"
            result_file = capture_dir / "latest.json"
            browser_context = FakeBrowserContextManager()
            with (
                patch.object(probe_exam_page, "CAPTURE_DIR", capture_dir),
                patch.object(probe_exam_page, "RESULT_FILE", result_file),
                patch.object(
                    probe_exam_page,
                    "load_cookies",
                    return_value=[{"name": "session", "domain": ".zhixueyun.com"}],
                ),
                patch.object(
                    probe_exam_page,
                    "create_browser_context",
                    return_value=browser_context,
                ) as mock_create_context,
                patch.object(
                    probe_exam_page,
                    "_wait_for_target_route_after_auth",
                    new=AsyncMock(return_value=False),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "已停止页面解析"):
                    await probe_exam_page.main(
                        "https://kc.zhixueyun.com/#/exam/exam/answer-paper/test"
                    )

            mock_create_context.assert_called_once_with(
                cookies_path=probe_exam_page.COOKIES_FILE,
                headless=False,
            )
            browser_context.context.page.locator.assert_not_called()
            result = json.loads(result_file.read_text(encoding="utf-8"))
            self.assertFalse(result["login_redirect_completed"])
            self.assertIn("未解析或保存当前登录页", result["error"])


if __name__ == "__main__":
    unittest.main()
