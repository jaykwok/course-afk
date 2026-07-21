import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from core import workflows
from core.workflows import parse_manual_selection_input


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
        self.closed = True


@asynccontextmanager
async def _fake_create_browser_context_for_entry(context):
    """模拟 create_browser_context：已有主控页 + 复用同一 context。"""
    if not context.pages:
        context.pages.append(
            FakeManualSelectionPage(url="https://www.mylearning.cn/")
        )
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
            callback_states = []
            with (
                mock.patch.object(workflows, "LEARNING_URLS_FILE", learning_file),
                mock.patch.object(workflows, "EXAM_URLS_FILE", exam_file),
                mock.patch.object(
                    workflows,
                    "create_browser_context",
                    side_effect=lambda **_k: _fake_create_browser_context_for_entry(
                        fake_context
                    ),
                ),
            ):
                result = await workflows.collect_learning_links_from_entry_urls(
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
                mock.patch.object(workflows, "LEARNING_URLS_FILE", learning_file),
                mock.patch.object(workflows, "EXAM_URLS_FILE", exam_file),
                mock.patch.object(
                    workflows,
                    "create_browser_context",
                    side_effect=lambda **_k: _fake_create_browser_context_for_entry(
                        fake_context
                    ),
                ),
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
            learning_file = temp_root / "课程链接.json"
            exam_file = temp_root / "考试链接.json"
            learning_file.write_text("[]", encoding="utf-8")
            exam_file.write_text("[]", encoding="utf-8")

            fake_context = FakeManualSelectionContext(popup_urls=[popup_url])
            with (
                mock.patch.object(workflows, "LEARNING_URLS_FILE", learning_file),
                mock.patch.object(workflows, "EXAM_URLS_FILE", exam_file),
                mock.patch.object(
                    workflows,
                    "create_browser_context",
                    side_effect=lambda **_k: _fake_create_browser_context_for_entry(
                        fake_context
                    ),
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
            learning_file = temp_root / "课程链接.json"
            exam_file = temp_root / "考试链接.json"
            learning_file.write_text("[]", encoding="utf-8")
            exam_file.write_text("[]", encoding="utf-8")

            fake_context = FakeManualSelectionContext(popup_urls=[popup_url])
            with (
                mock.patch.object(workflows, "LEARNING_URLS_FILE", learning_file),
                mock.patch.object(workflows, "EXAM_URLS_FILE", exam_file),
                mock.patch.object(
                    workflows,
                    "MANUAL_SELECTION_POPUP_URL_WAIT_MS",
                    0,
                ),
                mock.patch.object(
                    workflows,
                    "create_browser_context",
                    side_effect=lambda **_k: _fake_create_browser_context_for_entry(
                        fake_context
                    ),
                ),
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
        shared_context = object()
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
                    "create_browser_context",
                    side_effect=lambda *a, **k: _fake_shared_browser_context(
                        shared_context
                    ),
                ),
                mock.patch.object(
                    workflows,
                    "collect_learning_links_from_learning_zone_urls",
                    new=mock.AsyncMock(return_value=2),
                ) as parse_zone,
                mock.patch.object(
                    workflows,
                    "collect_learning_links_from_train_class_urls",
                    new=mock.AsyncMock(return_value=0),
                ) as parse_class,
                mock.patch.object(
                    workflows,
                    "collect_learning_links_from_entry_urls",
                    new=mock.AsyncMock(return_value=(1, 1, 0)),
                ) as collect_entry,
            ):
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
                context=shared_context,
            )
            parse_class.assert_not_awaited()
            collect_entry.assert_awaited_once_with(
                ["https://example.com/entry"],
                status_callback=None,
                context=shared_context,
            )
            self.assertEqual(result["direct_learning_count"], 1)
            self.assertEqual(result["direct_exam_count"], 0)
            self.assertEqual(result["learning_zone_parsed_count"], 2)
            self.assertEqual(result["train_class_parsed_count"], 0)
            self.assertEqual(result["entry_url_count"], 1)
            self.assertEqual(
                _read_learning_queue_urls(learning_file),
                [
                    "https://kc.zhixueyun.com/#/study/course/detail/12345678-1234-1234-1234-123456789abc"
                ],
            )

    async def test_run_manual_course_selection_manual_mode_opens_learning_zone_urls_manually(self):
        shared_context = object()
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
                    "create_browser_context",
                    side_effect=lambda *a, **k: _fake_shared_browser_context(
                        shared_context
                    ),
                ),
                mock.patch.object(
                    workflows,
                    "collect_learning_links_from_learning_zone_urls",
                    new=mock.AsyncMock(return_value=0),
                ) as parse_zone,
                mock.patch.object(
                    workflows,
                    "collect_learning_links_from_train_class_urls",
                    new=mock.AsyncMock(return_value=0),
                ) as parse_class,
                mock.patch.object(
                    workflows,
                    "collect_learning_links_from_entry_urls",
                    new=mock.AsyncMock(return_value=(2, 2, 0)),
                ) as collect_entry,
            ):
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
        parse_class.assert_not_awaited()
        collect_entry.assert_awaited_once_with(
            [
                "https://kc.zhixueyun.com/#/topic/专区001",
                "https://example.com/entry",
            ],
            status_callback=None,
            context=shared_context,
        )
        self.assertEqual(result["learning_zone_parsed_count"], 0)
        self.assertEqual(result["train_class_parsed_count"], 0)
        self.assertEqual(result["entry_url_count"], 2)

    async def test_run_manual_course_selection_auto_parses_train_class_urls(self):
        class_url = (
            "https://kc.zhixueyun.com/#/train-new/class-detail/"
            "e8d1e9b6-f9cf-4960-bf0f-57cd34dc0ca9"
        )
        shared_context = object()
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
                    "create_browser_context",
                    side_effect=lambda *a, **k: _fake_shared_browser_context(
                        shared_context
                    ),
                ),
                mock.patch.object(
                    workflows,
                    "collect_learning_links_from_learning_zone_urls",
                    new=mock.AsyncMock(return_value=0),
                ) as parse_zone,
                mock.patch.object(
                    workflows,
                    "collect_learning_links_from_train_class_urls",
                    new=mock.AsyncMock(return_value=3),
                ) as parse_class,
                mock.patch.object(
                    workflows,
                    "collect_learning_links_from_entry_urls",
                    new=mock.AsyncMock(return_value=(0, 0, 0)),
                ) as collect_entry,
            ):
                result = await workflows.run_manual_course_selection(
                    "\n".join(
                        [
                            class_url,
                            "https://kc.zhixueyun.com/app/wechat/#/qrScan?"
                            "businessType=6&businessId="
                            "e8d1e9b6-f9cf-4960-bf0f-57cd34dc0ca9",
                        ]
                    ),
                    learning_zone_mode="auto",
                )

        parse_zone.assert_not_awaited()
        parse_class.assert_awaited_once()
        self.assertEqual(parse_class.await_args.args[0], [class_url])
        self.assertIsNone(parse_class.await_args.kwargs.get("status_callback"))
        self.assertIs(
            parse_class.await_args.kwargs.get("context"),
            shared_context,
        )
        self.assertIn("before_close_callback", parse_class.await_args.kwargs)
        # 无入口时不再空调 entry collect
        collect_entry.assert_not_awaited()
        self.assertEqual(result["train_class_url_count"], 1)
        self.assertEqual(result["train_class_parsed_count"], 3)
        self.assertEqual(result["entry_url_count"], 0)

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

    async def test_run_manual_course_selection_expands_direct_subject_url(self):
        subject_url = (
            "https://kc.zhixueyun.com/#/study/subject/detail/"
            "1d40d4e0-a622-4535-8f02-ad108a930656"
        )
        course_url = (
            "https://kc.zhixueyun.com/#/study/course/detail/"
            "d5832449-44e7-41da-a593-c661f27842ed"
        )
        shared_context = object()
        with TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            learning_file = temp_root / "课程链接.json"
            exam_file = temp_root / "考试链接.json"
            learning_file.write_text("[]", encoding="utf-8")
            exam_file.write_text("[]", encoding="utf-8")

            async def fake_expand(subject_urls, status_callback=None, **kwargs):
                self.assertEqual(list(subject_urls), [subject_url])
                self.assertIs(kwargs.get("context"), shared_context)
                from core.learning_queue import append_learning_urls
                from core.exam_queue import append_exam_urls

                learning_added = append_learning_urls(
                    [course_url], file_path=learning_file
                )
                exam_added = append_exam_urls([], file_path=exam_file)
                return {
                    "course_count": 1,
                    "exam_count": 0,
                    "residual_count": 0,
                    "learning_added": len(learning_added),
                    "exam_added": len(exam_added),
                    "subject_count": 1,
                }

            with (
                mock.patch.object(workflows, "LEARNING_URLS_FILE", learning_file),
                mock.patch.object(workflows, "EXAM_URLS_FILE", exam_file),
                mock.patch.object(
                    workflows,
                    "create_browser_context",
                    side_effect=lambda *a, **k: _fake_shared_browser_context(
                        shared_context
                    ),
                ),
                mock.patch.object(
                    workflows,
                    "expand_and_append_subject_urls",
                    side_effect=fake_expand,
                ) as expand_mock,
                mock.patch.object(
                    workflows,
                    "collect_learning_links_from_entry_urls",
                    new=mock.AsyncMock(return_value=(0, 0, 0)),
                ),
            ):
                result = await workflows.run_manual_course_selection(subject_url)

            learning_urls = _read_learning_queue_urls(learning_file)

        expand_mock.assert_awaited_once()
        self.assertEqual(result["direct_subject_count"], 1)
        self.assertEqual(result["direct_learning_count"], 1)
        self.assertEqual(learning_urls, [course_url])

    async def test_run_manual_course_selection_shares_browser_across_jobs(self):
        """主题 + 专区 + 培训班只起一次浏览器，并传入同一 context。"""
        subject_url = (
            "https://kc.zhixueyun.com/#/study/subject/detail/"
            "1d40d4e0-a622-4535-8f02-ad108a930656"
        )
        zone_url = "https://kc.zhixueyun.com/#/topic/专区001"
        class_url = (
            "https://kc.zhixueyun.com/#/train-new/class-detail/"
            "e8d1e9b6-f9cf-4960-bf0f-57cd34dc0ca9"
        )
        shared_context = object()
        browser_starts = {"count": 0}

        @asynccontextmanager
        async def counting_browser(*_a, **_k):
            browser_starts["count"] += 1
            yield object(), shared_context

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
                    "create_browser_context",
                    side_effect=counting_browser,
                ),
                mock.patch.object(
                    workflows,
                    "expand_and_append_subject_urls",
                    new=mock.AsyncMock(
                        return_value={
                            "course_count": 0,
                            "exam_count": 0,
                            "residual_count": 0,
                            "learning_added": 0,
                            "exam_added": 0,
                            "subject_count": 1,
                        }
                    ),
                ) as expand_mock,
                mock.patch.object(
                    workflows,
                    "collect_learning_links_from_learning_zone_urls",
                    new=mock.AsyncMock(return_value=1),
                ) as parse_zone,
                mock.patch.object(
                    workflows,
                    "collect_learning_links_from_train_class_urls",
                    new=mock.AsyncMock(return_value=2),
                ) as parse_class,
                mock.patch.object(
                    workflows,
                    "collect_learning_links_from_entry_urls",
                    new=mock.AsyncMock(return_value=(0, 0, 0)),
                ),
            ):
                result = await workflows.run_manual_course_selection(
                    "\n".join([subject_url, zone_url, class_url]),
                    learning_zone_mode="auto",
                )

        self.assertEqual(browser_starts["count"], 1)
        self.assertIs(expand_mock.await_args.kwargs.get("context"), shared_context)
        self.assertIs(parse_zone.await_args.kwargs.get("context"), shared_context)
        self.assertIs(parse_class.await_args.kwargs.get("context"), shared_context)
        self.assertEqual(result["direct_subject_count"], 1)
        self.assertEqual(result["learning_zone_parsed_count"], 1)
        self.assertEqual(result["train_class_parsed_count"], 2)

    async def test_run_manual_course_selection_shares_browser_with_entry_phase(self):
        """自动批 + 入口：只起一次浏览器，Phase B 复用同一 context。"""
        zone_url = "https://kc.zhixueyun.com/#/topic/专区001"
        entry_url = "https://example.com/entry"
        shared_context = object()
        browser_starts = {"count": 0}

        @asynccontextmanager
        async def counting_browser(*_a, **kwargs):
            browser_starts["count"] += 1
            # 有入口时编排层强制有头
            self.assertIs(kwargs.get("headless"), False)
            yield object(), shared_context

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
                    "create_browser_context",
                    side_effect=counting_browser,
                ),
                mock.patch.object(
                    workflows,
                    "collect_learning_links_from_learning_zone_urls",
                    new=mock.AsyncMock(return_value=1),
                ) as parse_zone,
                mock.patch.object(
                    workflows,
                    "collect_learning_links_from_entry_urls",
                    new=mock.AsyncMock(return_value=(1, 1, 0)),
                ) as collect_entry,
            ):
                result = await workflows.run_manual_course_selection(
                    "\n".join([zone_url, entry_url]),
                    learning_zone_mode="auto",
                )

        self.assertEqual(browser_starts["count"], 1)
        self.assertIs(parse_zone.await_args.kwargs.get("context"), shared_context)
        collect_entry.assert_awaited_once_with(
            [entry_url],
            status_callback=None,
            context=shared_context,
        )
        self.assertEqual(result["learning_zone_parsed_count"], 1)
        self.assertEqual(result["manual_record_count"], 1)
        self.assertEqual(result["entry_url_count"], 1)

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
                mock.patch.object(workflows, "LEARNING_URLS_FILE", learning_file),
                mock.patch.object(workflows, "EXAM_URLS_FILE", exam_file),
                mock.patch.object(
                    workflows,
                    "create_browser_context",
                    side_effect=AssertionError("不应自建浏览器"),
                ),
                mock.patch.object(
                    workflows,
                    "is_controller_page",
                    return_value=False,
                ),
            ):
                result = await workflows.collect_learning_links_from_entry_urls(
                    ["https://example.com/entry"],
                    context=fake_context,
                )

        self.assertEqual(result, (0, 0, 0))
        self.assertFalse(fake_context.closed)


if __name__ == "__main__":
    unittest.main()
