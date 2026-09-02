import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch


class ExamPageProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_redirect_failure_is_not_parsed_or_saved(self):
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
                    new=AsyncMock(side_effect=RuntimeError("browser closed")),
                ) as mock_wait_for_auth,
            ):
                with self.assertRaisesRegex(RuntimeError, "browser closed"):
                    await probe_exam_page.main(
                        "https://kc.zhixueyun.com/#/exam/exam/answer-paper/test"
                    )

            mock_create_context.assert_called_once_with(
                cookies_path=probe_exam_page.COOKIES_FILE,
                headless=False,
            )
            mock_wait_for_auth.assert_awaited_once_with(
                browser_context.context.page,
                "https://kc.zhixueyun.com/#/exam/exam/answer-paper/test",
                timeout_ms=0,
            )
            browser_context.context.page.locator.assert_not_called()
            self.assertFalse(result_file.exists())


class CoursePageProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_runs_visible_formal_flow_and_saves_capture(self):
        from tools import probe_course_page

        class FakePage:
            url = "https://kc.zhixueyun.com/#/study/course/detail/test"

            def on(self, _event, _callback):
                return None

            async def title(self):
                return "测试课程"

            async def evaluate(self, _script, _selectors):
                return {
                    "selectorResults": [],
                    "interesting": [],
                    "subjectItems": [],
                    "bodyText": "课程正文",
                }

            async def content(self):
                return "<html><body>课程正文</body></html>"

        class FakeBrowserContextManager:
            async def __aenter__(self):
                return object(), object()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        async def fake_process(
            _context,
            _url,
            _handler,
            *,
            capture_callback,
            failure_file,
        ):
            self.assertIn("course_page", str(failure_file))
            await capture_callback(FakePage(), "page_created")
            await capture_callback(FakePage(), "after_navigation")
            return False

        with TemporaryDirectory() as tmp:
            capture_dir = Path(tmp) / "course_page"
            latest = capture_dir / "latest.json"
            with (
                patch.object(probe_course_page, "CAPTURE_DIR", capture_dir),
                patch.object(probe_course_page, "RESULT_FILE", latest),
                patch.object(probe_course_page, "ensure_data_layout"),
                patch.object(
                    probe_course_page,
                    "load_cookies",
                    return_value=[{"domain": ".example.test", "value": "secret"}],
                ),
                patch.object(
                    probe_course_page,
                    "create_browser_context",
                    return_value=FakeBrowserContextManager(),
                ) as create_context,
                patch.object(
                    probe_course_page,
                    "_process_url",
                    new=AsyncMock(side_effect=fake_process),
                ),
                patch.object(
                    probe_course_page, "sample_afk_slow_mo", return_value=2345
                ),
            ):
                result = await probe_course_page.main(
                    "https://kc.zhixueyun.com/#/study/course/detail/test",
                    run_flow=True,
                    flow_timeout=30,
                )

            # slow_mo 每次运行随机取样：取到的值同时用于启动和快照记录
            create_context.assert_called_once_with(
                cookies_path=probe_course_page.COOKIES_FILE,
                headless=False,
                slow_mo=2345,
            )
            self.assertEqual(result["browser_slow_mo"], 2345)
            self.assertEqual(result["flow"]["status"], "completed")
            self.assertEqual(result["headless"], False)
            self.assertEqual(len(result["captures"]), 1)
            self.assertTrue(latest.exists())
            self.assertNotIn("secret", latest.read_text(encoding="utf-8"))
            self.assertEqual(result["network_capture"]["event_count"], 0)
            self.assertEqual(result["console_capture"]["event_count"], 0)


if __name__ == "__main__":
    unittest.main()
