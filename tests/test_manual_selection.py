import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from core import workflows
from core.workflows import parse_manual_selection_input


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

    async def add_cookies(self, _cookies):
        return None

    def on(self, event, handler):
        if event == "page":
            self.page_handler = handler

    async def new_page(self):
        popup_url = (
            self.popup_urls.pop(0)
            if self.page_handler and self.popup_urls
            else None
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
        return None


class FakeManualSelectionBrowser:
    def __init__(self, context):
        self.context = context

    async def new_context(self, no_viewport=True):
        return self.context

    async def close(self):
        return None


class FakeManualSelectionChromium:
    def __init__(self, context):
        self.context = context

    async def launch(self, **_kwargs):
        return FakeManualSelectionBrowser(self.context)


class FakeAsyncPlaywrightManager:
    def __init__(self, context):
        self.context = context

    async def __aenter__(self):
        return type(
            "FakePlaywright",
            (),
            {"chromium": FakeManualSelectionChromium(self.context)},
        )()

    async def __aexit__(self, exc_type, exc, tb):
        return False


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
    async def test_collect_learning_links_from_entry_urls_keeps_context_created_pages_open(self):
        with TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            cookies_file = temp_root / "cookies.json"
            learning_file = temp_root / "课程链接.json"
            exam_file = temp_root / "考试链接.json"
            cookies_file.write_text(json.dumps([]), encoding="utf-8")
            learning_file.write_text("[]", encoding="utf-8")
            exam_file.write_text("[]", encoding="utf-8")

            fake_context = FakeManualSelectionContext()
            with mock.patch.object(workflows, "COOKIES_FILE", cookies_file):
                with mock.patch.object(workflows, "LEARNING_URLS_FILE", learning_file):
                    with mock.patch.object(workflows, "EXAM_URLS_FILE", exam_file):
                        with mock.patch.object(
                            workflows,
                            "async_playwright",
                            return_value=FakeAsyncPlaywrightManager(fake_context),
                        ):
                            result = await workflows.collect_learning_links_from_entry_urls(
                                ["https://example.com/entry"]
                            )

        self.assertEqual(result, (0, 0, 0))
        self.assertEqual(fake_context.pages[0].url, workflows.MYLEARNING_HOME)

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
            with mock.patch.object(workflows, "COOKIES_FILE", cookies_file):
                with mock.patch.object(workflows, "LEARNING_URLS_FILE", learning_file):
                    with mock.patch.object(workflows, "EXAM_URLS_FILE", exam_file):
                        with mock.patch.object(
                            workflows,
                            "async_playwright",
                            return_value=FakeAsyncPlaywrightManager(fake_context),
                        ):
                            result = await workflows.collect_learning_links_from_entry_urls(
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
            cookies_file = temp_root / "cookies.json"
            learning_file = temp_root / "课程链接.json"
            exam_file = temp_root / "考试链接.json"
            cookies_file.write_text(json.dumps([]), encoding="utf-8")
            learning_file.write_text("[]", encoding="utf-8")
            exam_file.write_text("[]", encoding="utf-8")

            fake_context = FakeManualSelectionContext(popup_urls=[popup_url])
            with (
                mock.patch.object(workflows, "COOKIES_FILE", cookies_file),
                mock.patch.object(workflows, "LEARNING_URLS_FILE", learning_file),
                mock.patch.object(workflows, "EXAM_URLS_FILE", exam_file),
                mock.patch.object(
                    workflows,
                    "async_playwright",
                    return_value=FakeAsyncPlaywrightManager(fake_context),
                ),
            ):
                result = await workflows.collect_learning_links_from_entry_urls(
                    ["https://example.com/entry"]
                )

            learning_urls = _read_learning_queue_urls(learning_file)
            exam_urls = _read_exam_queue_urls(exam_file)

        self.assertEqual(result, (0, 0, 1))
        self.assertEqual(learning_urls, [])
        self.assertEqual(exam_urls, [popup_url])

    async def test_collect_learning_links_from_entry_urls_records_nonstandard_learning_popup(self):
        popup_url = "https://kc.zhixueyun.com/#/paas-container?paasurl=website%2Fdemo"
        with TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            cookies_file = temp_root / "cookies.json"
            learning_file = temp_root / "课程链接.json"
            exam_file = temp_root / "考试链接.json"
            cookies_file.write_text(json.dumps([]), encoding="utf-8")
            learning_file.write_text("[]", encoding="utf-8")
            exam_file.write_text("[]", encoding="utf-8")

            fake_context = FakeManualSelectionContext(popup_urls=[popup_url])
            with mock.patch.object(workflows, "COOKIES_FILE", cookies_file):
                with mock.patch.object(workflows, "LEARNING_URLS_FILE", learning_file):
                    with mock.patch.object(workflows, "EXAM_URLS_FILE", exam_file):
                        with mock.patch.object(
                            workflows,
                            "MANUAL_SELECTION_POPUP_URL_WAIT_MS",
                            0,
                        ):
                            with mock.patch.object(
                                workflows,
                                "async_playwright",
                                return_value=FakeAsyncPlaywrightManager(fake_context),
                            ):
                                result = await workflows.collect_learning_links_from_entry_urls(
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
        workflows._track_background_task(task, pending_tasks)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(pending_tasks, set())
        self.assertTrue(task.done())
        self.assertIsInstance(task.exception(), RuntimeError)

    async def test_run_manual_course_selection_auto_parses_learning_zone_urls(self):
        with TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            learning_file = temp_root / "课程链接.json"
            exam_file = temp_root / "考试链接.json"
            learning_file.write_text("[]", encoding="utf-8")
            exam_file.write_text("[]", encoding="utf-8")

            with (
                mock.patch.object(workflows, "LEARNING_URLS_FILE", learning_file),
                mock.patch.object(workflows, "EXAM_URLS_FILE", exam_file),
            ):
                with mock.patch.object(
                    workflows,
                    "collect_learning_links_from_learning_zone_urls",
                    new=mock.AsyncMock(return_value=2),
                ) as parse_zone:
                    with mock.patch.object(
                        workflows,
                        "collect_learning_links_from_entry_urls",
                        new=mock.AsyncMock(return_value=(1, 1, 0)),
                    ) as collect_entry:
                        result = await workflows.run_manual_course_selection(
                            "\n".join(
                                [
                                    "https://kc.zhixueyun.com/#/study/course/detail/12345678-1234-1234-1234-123456789abc",
                                    "https://kc.zhixueyun.com/#/topic/专区001",
                                    "https://example.com/entry",
                                ]
                            ),
                            learning_zone_mode="auto",
                        )

            parse_zone.assert_awaited_once_with(
                ["https://kc.zhixueyun.com/#/topic/专区001"],
                status_callback=None,
            )
            collect_entry.assert_awaited_once_with(
                ["https://example.com/entry"],
                status_callback=None,
            )
            self.assertEqual(result["direct_learning_count"], 1)
            self.assertEqual(result["direct_exam_count"], 0)
            self.assertEqual(result["learning_zone_parsed_count"], 2)
            self.assertEqual(result["entry_url_count"], 1)
            self.assertEqual(
                _read_learning_queue_urls(learning_file),
                [
                    "https://kc.zhixueyun.com/#/study/course/detail/12345678-1234-1234-1234-123456789abc"
                ],
            )

    async def test_run_manual_course_selection_manual_mode_opens_learning_zone_urls_manually(self):
        with TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            learning_file = temp_root / "课程链接.json"
            exam_file = temp_root / "考试链接.json"
            learning_file.write_text("[]", encoding="utf-8")
            exam_file.write_text("[]", encoding="utf-8")

            with (
                mock.patch.object(workflows, "LEARNING_URLS_FILE", learning_file),
                mock.patch.object(workflows, "EXAM_URLS_FILE", exam_file),
            ):
                with mock.patch.object(
                    workflows,
                    "collect_learning_links_from_learning_zone_urls",
                    new=mock.AsyncMock(return_value=0),
                ) as parse_zone:
                    with mock.patch.object(
                        workflows,
                        "collect_learning_links_from_entry_urls",
                        new=mock.AsyncMock(return_value=(2, 2, 0)),
                    ) as collect_entry:
                        result = await workflows.run_manual_course_selection(
                            "\n".join(
                                [
                                    "https://kc.zhixueyun.com/#/topic/专区001",
                                    "https://example.com/entry",
                                ]
                            ),
                            learning_zone_mode="manual",
                        )

        parse_zone.assert_not_awaited()
        collect_entry.assert_awaited_once_with(
            [
                "https://kc.zhixueyun.com/#/topic/专区001",
                "https://example.com/entry",
            ],
            status_callback=None,
        )
        self.assertEqual(result["learning_zone_parsed_count"], 0)
        self.assertEqual(result["entry_url_count"], 2)

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
                    workflows,
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


if __name__ == "__main__":
    unittest.main()
