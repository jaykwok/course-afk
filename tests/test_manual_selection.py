import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from core.app import workflows
from core.discovery import manual
from core.app.workflows import parse_manual_selection_input


@asynccontextmanager
async def _fake_shared_browser_context(shared_context=None):
    """供手动选课共享浏览器路径 mock create_browser_context。"""
    yield object(), shared_context if shared_context is not None else object()


class FakeManualSelectionPage:
    def __init__(
        self,
        url: str = "about:blank",
        opener_page=None,
        popup_url: str | None = None,
        page_handler_getter=None,
    ):
        self.url = url
        self._opener_page = opener_page
        self._popup_url = popup_url
        self._page_handler_getter = page_handler_getter
        self.closed = False

    async def opener(self):
        return self._opener_page

    async def wait_for_timeout(self, _milliseconds):
        await asyncio.sleep(0)

    async def evaluate(self, _script):
        return None

    async def goto(self, url, wait_until="load"):
        self.url = url
        if self._popup_url and self._page_handler_getter:
            handler = self._page_handler_getter()
            if handler:
                handler(FakeManualSelectionPage(url=self._popup_url))
            self._popup_url = None
        for _ in range(3):
            await asyncio.sleep(0)
            if self.closed:
                raise RuntimeError("Target page, context or browser has been closed")

    async def wait_for_url(self, _pattern, timeout=0):
        for _ in range(3):
            await asyncio.sleep(0)
            if self.closed:
                raise RuntimeError("Target page, context or browser has been closed")

    async def wait_for_event(self, _event, timeout=0):
        self.closed = True

    async def close(self):
        self.closed = True


class FakeManualSelectionContext:
    def __init__(self, popup_urls: list[str] | None = None):
        self.page_handler = None
        self.popup_urls = list(popup_urls or [])
        self.pages = []
        self.closed = False

    async def add_cookies(self, _cookies):
        return None

    def on(self, event, handler):
        if event == "page":
            self.page_handler = handler

    async def new_page(self):
        popup_url = (
            self.popup_urls.pop(0) if self.page_handler and self.popup_urls else None
        )
        page = FakeManualSelectionPage(
            popup_url=popup_url,
            page_handler_getter=lambda: self.page_handler,
        )
        self.pages.append(page)
        if self.page_handler:
            self.page_handler(page)
        return page

    async def close(self):
        self.closed = True


@asynccontextmanager
async def _fake_create_browser_context_for_entry(context):
    """模拟 create_browser_context：已有空白占位页 + 复用同一 context。"""
    if not context.pages:
        context.pages.append(FakeManualSelectionPage(url="about:blank"))
    try:
        yield object(), context
    finally:
        await context.close()


def _read_learning_queue_urls(file_path):
    return [entry["url"] for entry in json.loads(file_path.read_text(encoding="utf-8"))]


def _read_exam_queue_urls(file_path):
    return [entry["url"] for entry in json.loads(file_path.read_text(encoding="utf-8"))]


class ManualSelectionTests(unittest.TestCase):
    def test_parse_manual_selection_input_returns_empty_for_blank_text(self):
        self.assertEqual(parse_manual_selection_input(" \n\t "), [])

    def test_parse_manual_selection_input_extracts_multiple_urls(self):
        text = (
            "请处理这些入口 https://a.example.com/1, https://b.example.com/2"
            "\n以及 https://a.example.com/1"
        )
        self.assertEqual(
            parse_manual_selection_input(text),
            ["https://a.example.com/1", "https://b.example.com/2"],
        )


class ManualSelectionWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(
            mock.patch(
                "core.auth.access.require_task_account", return_value="test-account"
            )
        )

    async def test_collect_learning_links_from_entry_urls_keeps_context_created_pages_open(
        self,
    ):
        with TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            cookies_file = temp_root / "cookies.json"
            learning_file = temp_root / "课程链接.json"
            exam_file = temp_root / "考试链接.json"
            cookies_file.write_text(json.dumps([]), encoding="utf-8")
            learning_file.write_text("[]", encoding="utf-8")
            exam_file.write_text("[]", encoding="utf-8")

            fake_context = FakeManualSelectionContext()
            callback_states = []
            with (
                mock.patch.object(manual, "LEARNING_URLS_FILE", learning_file),
                mock.patch.object(manual, "EXAM_URLS_FILE", exam_file),
                mock.patch.object(
                    manual,
                    "create_browser_context",
                    side_effect=lambda **_k: _fake_create_browser_context_for_entry(
                        fake_context
                    ),
                ),
            ):
                result = await manual.collect_learning_links_from_entry_urls(
                    ["https://example.com/entry"],
                    before_close_callback=lambda counts: callback_states.append(
                        (
                            counts,
                            fake_context.closed,
                            fake_context.pages[0].closed,
                        )
                    ),
                )

        self.assertEqual(result, (0, 0, 0))
        self.assertEqual(callback_states, [((0, 0, 0), False, False)])
        self.assertTrue(fake_context.closed)

    async def test_collect_learning_links_from_entry_urls_records_noopener_popup(self):
        popup_url = "https://kc.zhixueyun.com/#/study/course/detail/11111111-1111-1111-1111-111111111111"
        with TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            cookies_file = temp_root / "cookies.json"
            learning_file = temp_root / "课程链接.json"
            exam_file = temp_root / "考试链接.json"
            cookies_file.write_text(json.dumps([]), encoding="utf-8")
            learning_file.write_text("[]", encoding="utf-8")
            exam_file.write_text("[]", encoding="utf-8")

            fake_context = FakeManualSelectionContext(popup_urls=[popup_url])
            with (
                mock.patch.object(manual, "LEARNING_URLS_FILE", learning_file),
                mock.patch.object(manual, "EXAM_URLS_FILE", exam_file),
                mock.patch.object(
                    manual,
                    "create_browser_context",
                    side_effect=lambda **_k: _fake_create_browser_context_for_entry(
                        fake_context
                    ),
                ),
            ):
                result = await manual.collect_learning_links_from_entry_urls(
                    ["https://example.com/entry"]
                )
            saved_urls = _read_learning_queue_urls(learning_file)

        self.assertEqual(result, (1, 1, 0))
        self.assertEqual(saved_urls, [popup_url])

    async def test_collect_learning_links_from_entry_urls_routes_exam_popup(self):
        popup_url = (
            "https://kc.zhixueyun.com/#/exam/exam/answer-paper/"
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        )
        with TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            learning_file = temp_root / "课程链接.json"
            exam_file = temp_root / "考试链接.json"
            learning_file.write_text("[]", encoding="utf-8")
            exam_file.write_text("[]", encoding="utf-8")

            fake_context = FakeManualSelectionContext(popup_urls=[popup_url])
            with (
                mock.patch.object(manual, "LEARNING_URLS_FILE", learning_file),
                mock.patch.object(manual, "EXAM_URLS_FILE", exam_file),
                mock.patch.object(
                    manual,
                    "create_browser_context",
                    side_effect=lambda **_k: _fake_create_browser_context_for_entry(
                        fake_context
                    ),
                ),
            ):
                result = await manual.collect_learning_links_from_entry_urls(
                    ["https://example.com/entry"]
                )

            learning_urls = _read_learning_queue_urls(learning_file)
            exam_urls = _read_exam_queue_urls(exam_file)

        self.assertEqual(result, (0, 0, 1))
        self.assertEqual(learning_urls, [])
        self.assertEqual(exam_urls, [popup_url])

    async def test_collect_learning_links_from_entry_urls_records_nonstandard_learning_popup(
        self,
    ):
        popup_url = "https://kc.zhixueyun.com/#/paas-container?paasurl=website%2Fdemo"
        with TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            learning_file = temp_root / "课程链接.json"
            exam_file = temp_root / "考试链接.json"
            learning_file.write_text("[]", encoding="utf-8")
            exam_file.write_text("[]", encoding="utf-8")

            fake_context = FakeManualSelectionContext(popup_urls=[popup_url])
            with (
                mock.patch.object(manual, "LEARNING_URLS_FILE", learning_file),
                mock.patch.object(manual, "EXAM_URLS_FILE", exam_file),
                mock.patch.object(
                    manual,
                    "MANUAL_SELECTION_POPUP_URL_WAIT_MS",
                    0,
                ),
                mock.patch.object(
                    manual,
                    "create_browser_context",
                    side_effect=lambda **_k: _fake_create_browser_context_for_entry(
                        fake_context
                    ),
                ),
            ):
                result = await manual.collect_learning_links_from_entry_urls(
                    ["https://example.com/entry"]
                )
            saved_urls = _read_learning_queue_urls(learning_file)

        self.assertEqual(result, (1, 1, 0))
        self.assertEqual(saved_urls, [popup_url])

    async def test_track_background_task_consumes_task_exceptions(self):
        pending_tasks = set()

        async def fail():
            raise RuntimeError("boom")

        task = asyncio.create_task(fail())
        manual._track_background_task(task, pending_tasks)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(pending_tasks, set())
        self.assertTrue(task.done())
        self.assertIsInstance(task.exception(), RuntimeError)

    async def test_run_manual_course_selection_auto_parses_learning_zone_urls(self):
        from tests.workflow_harness import new_selection

        await new_selection(
            self, "test_run_manual_course_selection_auto_parses_learning_zone_urls"
        )

    async def test_run_manual_course_selection_manual_mode_opens_learning_zone_urls_manually(
        self,
    ):
        from tests.workflow_harness import new_selection

        await new_selection(
            self,
            "test_run_manual_course_selection_manual_mode_opens_learning_zone_urls_manually",
        )

    async def test_run_manual_course_selection_auto_parses_train_class_urls(self):
        from tests.workflow_harness import new_selection

        await new_selection(
            self, "test_run_manual_course_selection_auto_parses_train_class_urls"
        )

    async def test_run_manual_course_selection_routes_direct_exam_url(self):
        exam_url = (
            "https://kc.zhixueyun.com/#/exam/exam/answer-paper/"
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        )
        with TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            learning_file = temp_root / "课程链接.json"
            exam_file = temp_root / "考试链接.json"
            learning_file.write_text("[]", encoding="utf-8")
            exam_file.write_text("[]", encoding="utf-8")

            with (
                mock.patch.object(workflows, "LEARNING_URLS_FILE", learning_file),
                mock.patch.object(workflows, "EXAM_URLS_FILE", exam_file),
                mock.patch.object(
                    manual,
                    "collect_learning_links_from_entry_urls",
                    new=mock.AsyncMock(return_value=(0, 0, 0)),
                ),
            ):
                result = await workflows.run_manual_course_selection(exam_url)

            learning_urls = _read_learning_queue_urls(learning_file)
            exam_urls = _read_exam_queue_urls(exam_file)

        self.assertEqual(result["direct_learning_count"], 0)
        self.assertEqual(result["direct_exam_count"], 1)
        self.assertEqual(result["exam_total"], 1)
        self.assertEqual(learning_urls, [])
        self.assertEqual(exam_urls, [exam_url])

    async def test_run_manual_course_selection_queues_exam_detail_without_browser(self):
        url = (
            "https://kc.zhixueyun.com/#/exam/exam/exam-detail/"
            "9ace2356-f9a1-4ad1-a0c2-5c4a1d4e6a92"
        )
        with TemporaryDirectory() as tmp:
            learning_file = Path(tmp) / "learning.json"
            exam_file = Path(tmp) / "exam.json"
            with (
                mock.patch.object(workflows, "LEARNING_URLS_FILE", learning_file),
                mock.patch.object(workflows, "EXAM_URLS_FILE", exam_file),
                mock.patch.object(manual, "create_browser_context") as create_browser,
            ):
                result = await workflows.run_manual_course_selection(
                    f"考试链接：{url}\n{url}?from=manual"
                )
            create_browser.assert_not_called()
            self.assertEqual(
                _read_exam_queue_urls(exam_file),
                [url.replace("exam-detail", "answer-paper")],
            )
            self.assertFalse(learning_file.exists())
        self.assertEqual(result["direct_exam_count"], 1)
        self.assertEqual(result["exam_total"], 1)
        self.assertEqual(result["entry_url_count"], 0)

    async def test_run_manual_course_selection_expands_direct_subject_url(self):
        from tests.workflow_harness import new_selection

        await new_selection(
            self, "test_run_manual_course_selection_expands_direct_subject_url"
        )

    async def test_run_manual_course_selection_shares_browser_across_jobs(self):
        from tests.workflow_harness import new_selection

        await new_selection(
            self, "test_run_manual_course_selection_shares_browser_across_jobs"
        )

    async def test_run_manual_course_selection_shares_browser_with_entry_phase(self):
        from tests.workflow_harness import new_selection

        await new_selection(
            self, "test_run_manual_course_selection_shares_browser_with_entry_phase"
        )

    async def test_collect_entry_with_injected_context_does_not_create_browser(self):
        """context= 注入时不再 create_browser_context。"""
        with TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            learning_file = temp_root / "课程链接.json"
            exam_file = temp_root / "考试链接.json"
            learning_file.write_text("[]", encoding="utf-8")
            exam_file.write_text("[]", encoding="utf-8")

            fake_context = FakeManualSelectionContext()
            with (
                mock.patch.object(manual, "LEARNING_URLS_FILE", learning_file),
                mock.patch.object(manual, "EXAM_URLS_FILE", exam_file),
                mock.patch.object(
                    manual,
                    "create_browser_context",
                    side_effect=AssertionError("不应自建浏览器"),
                ),
                mock.patch.object(
                    manual,
                    "is_session_internal_page",
                    return_value=False,
                ),
            ):
                result = await manual.collect_learning_links_from_entry_urls(
                    ["https://example.com/entry"],
                    context=fake_context,
                )

        self.assertEqual(result, (0, 0, 0))
        self.assertFalse(fake_context.closed)


if __name__ == "__main__":
    unittest.main()
