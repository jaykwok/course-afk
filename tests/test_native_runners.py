import asyncio
import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch




def _model_config(
    model="test-model",
    *,
    request_type="responses",
    web_search=False,
    thinking=False,
    reasoning_effort=None,
):
    return {
        "model": model,
        "request_type": request_type,
        "web_search": web_search,
        "thinking": thinking,
        "reasoning_effort": reasoning_effort,
    }


def _exam_entries(urls, failed_model_configs_by_url=None):
    failed_model_configs_by_url = failed_model_configs_by_url or {}
    return [
        {
            "url": url,
            "ai_failed_model_configs": failed_model_configs_by_url.get(url, []),
        }
        for url in urls
    ]


def _write_exam_queue_fixture(file_path, urls, failed_model_configs_by_url=None):
    file_path.write_text(
        json.dumps(_exam_entries(urls, failed_model_configs_by_url), ensure_ascii=False),
        encoding="utf-8",
    )


def _read_exam_queue_urls(file_path):
    return [entry["url"] for entry in json.loads(file_path.read_text(encoding="utf-8"))]


def _manual_entries(urls, reason="manual_pending", failed_model_configs_by_url=None):
    failed_model_configs_by_url = failed_model_configs_by_url or {}
    return [
        {
            "url": url,
            "reason": reason,
            "reason_text": "测试人工考试待处理",
            "remaining_attempts": None,
            "threshold": None,
            "ai_failed_model_configs": failed_model_configs_by_url.get(url, []),
        }
        for url in urls
    ]


def _write_manual_exam_queue_fixture(file_path, urls, failed_model_configs_by_url=None):
    file_path.write_text(
        json.dumps(
            _manual_entries(urls, failed_model_configs_by_url=failed_model_configs_by_url),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _read_manual_exam_queue(file_path):
    return json.loads(file_path.read_text(encoding="utf-8"))


def _learning_entries(urls):
    return [{"url": url} for url in urls]


def _write_learning_queue_fixture(file_path, urls):
    file_path.write_text(
        json.dumps(_learning_entries(urls), ensure_ascii=False),
        encoding="utf-8",
    )


def _read_learning_queue_urls(file_path):
    return [entry["url"] for entry in json.loads(file_path.read_text(encoding="utf-8"))]


def _read_learning_failures(file_path):
    return json.loads(file_path.read_text(encoding="utf-8"))


class TargetClosedError(Exception):
    """模块级复用：模拟 Playwright 的 TargetClosedError（类名匹配 is_target_closed_exception）。"""


class _AfkCoursePage:
    """挂课一门一页：is_closed / close / goto 供 _process_url 使用。"""

    def __init__(self):
        self.closed = False
        self.gotos = []

    def is_closed(self):
        return self.closed

    async def evaluate(self, _script):
        return None

    async def wait_for_timeout(self, _milliseconds):
        return None

    async def goto(self, url, **kwargs):
        self.gotos.append(url)

    async def close(self):
        self.closed = True


async def _recheck_noop(_context):
    """复查 mock：无操作。"""
    return None


class AfkBatchPreparationTests(unittest.TestCase):
    def test_prepare_afk_batch_reads_pending_learning_json_queue(self):
        from core.learning.afk_runner import prepare_afk_batch

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            learning_file = root / "learning.json"
            _write_learning_queue_fixture(
                learning_file,
                ["https://a.example.com/1", "https://b.example.com/2"],
            )

            batch = prepare_afk_batch(
                learning_file=learning_file,
            )
            self.assertEqual(
                batch.urls,
                ["https://a.example.com/1", "https://b.example.com/2"],
            )

    def test_prepare_afk_batch_deduplicates_normalized_urls(self):
        from core.learning.afk_runner import prepare_afk_batch

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            learning_file = root / "learning.json"
            course = (
                "https://kc.zhixueyun.com/#/study/course/detail/"
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
            )
            _write_learning_queue_fixture(
                learning_file,
                [course, course + "?x=1", "  " + course + "  ", course],
            )

            batch = prepare_afk_batch(learning_file=learning_file)

            self.assertEqual(batch.urls, [course])
            self.assertEqual(_read_learning_queue_urls(learning_file), [course])

    def test_prepare_afk_batch_rejects_legacy_text_learning_file(self):
        from core.learning.afk_runner import prepare_afk_batch

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            learning_file = root / "learning.json"

            learning_file.write_text("https://c.example.com/3\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                prepare_afk_batch(learning_file=learning_file)


class AfkGracefulExitTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_afk_once_keeps_empty_learning_queue_file_after_processing(self):
        from core.learning.afk_runner import AfkBatch, run_afk_once

        class FakeContext:
            pass

        class FakeBrowserContextManager:
            async def __aenter__(self):
                return None, FakeContext()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            learning_file = root / "learning.json"
            _write_learning_queue_fixture(
                learning_file,
                ["https://kc.zhixueyun.com/#/study/course/detail/a"],
            )
            batch = AfkBatch(
                urls=["https://kc.zhixueyun.com/#/study/course/detail/a"],
            )

            with (
                patch("core.learning.afk_runner.LEARNING_URLS_FILE", learning_file),
                patch("core.learning.afk_runner.prepare_afk_batch", return_value=batch),
                patch(
                    "core.learning.afk_runner.create_browser_context",
                    return_value=FakeBrowserContextManager(),
                ),
                patch("core.learning.afk_runner.normalize_urls", side_effect=lambda urls: list(urls or [])),
                patch("core.learning.afk_runner.is_compliant_url_regex", return_value=True),
                patch("core.learning.afk_runner._process_url", new=AsyncMock(return_value=False)),
                patch(
                    "core.learning.afk_runner._recheck_url_type_links",
                    new=AsyncMock(side_effect=_recheck_noop),
                ),
            ):
                await run_afk_once()
            self.assertTrue(learning_file.exists())
            self.assertEqual(json.loads(learning_file.read_text(encoding="utf-8")), [])

    async def test_run_afk_once_keeps_failed_url_in_learning_queue(self):
        from core.learning.afk_runner import AfkBatch, run_afk_once

        class FakeContext:
            pass

        class FakeBrowserContextManager:
            async def __aenter__(self):
                return None, FakeContext()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            learning_file = root / "learning.json"
            failures_file = root / "failures.json"
            _write_learning_queue_fixture(
                learning_file,
                [
                    "https://kc.zhixueyun.com/#/study/course/detail/a",
                    "https://kc.zhixueyun.com/#/study/course/detail/b",
                ],
            )
            batch = AfkBatch(
                urls=[
                    "https://kc.zhixueyun.com/#/study/course/detail/a",
                    "https://kc.zhixueyun.com/#/study/course/detail/b",
                ],
            )

            with (
                patch("core.learning.afk_runner.LEARNING_URLS_FILE", learning_file),
                patch("core.learning.afk_runner.LEARNING_FAILURES_FILE", failures_file),
                patch("core.learning.afk_runner.prepare_afk_batch", return_value=batch),
                patch(
                    "core.learning.afk_runner.create_browser_context",
                    return_value=FakeBrowserContextManager(),
                ),
                patch("core.learning.afk_runner.normalize_urls", side_effect=lambda urls: list(urls or [])),
                patch("core.learning.afk_runner.is_compliant_url_regex", return_value=True),
                patch(
                    "core.learning.afk_runner._process_url",
                    new=AsyncMock(side_effect=[True, False]),
                ),
                patch(
                    "core.learning.afk_runner._recheck_url_type_links",
                    new=AsyncMock(side_effect=_recheck_noop),
                ),
            ):
                await run_afk_once()
            self.assertEqual(
                _read_learning_queue_urls(learning_file),
                ["https://kc.zhixueyun.com/#/study/course/detail/a"],
            )

    async def test_process_url_records_retryable_failure_to_learning_failures(self):
        from core.learning.afk_runner import _process_url

        class FakePage:
            def __init__(self):
                self.closed = False

            def is_closed(self):
                return self.closed

            async def evaluate(self, _script):
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

            async def goto(self, _url, **kwargs):
                return None

            async def close(self):
                self.closed = True

        page = FakePage()

        class FakeContext:
            async def new_page(self):
                return page

        async def failing_handler(_page):
            raise RuntimeError("boom")

        with TemporaryDirectory() as tmp:
            failures_file = Path(tmp) / "failures.json"

            with (
                patch("core.learning.afk_runner.LEARNING_FAILURES_FILE", failures_file),
                patch("core.learning.afk_runner.ensure_controller_page", new=AsyncMock()),
            ):
                keep_pending = await _process_url(
                    FakeContext(),
                    "https://kc.zhixueyun.com/#/study/course/detail/a",
                    failing_handler,
                )

            self.assertTrue(keep_pending)
            self.assertTrue(page.closed)
            self.assertEqual(
                _read_learning_failures(failures_file),
                [
                    {
                        "url": "https://kc.zhixueyun.com/#/study/course/detail/a",
                        "reason": "retryable_error",
                        "reason_text": "挂课处理失败，后续可重新加入课程链接: boom",
                        "detail": {},
                    }
                ],
            )

    async def test_process_url_capture_hook_uses_formal_lifecycle_and_probe_failure_file(self):
        from core.learning.afk_runner import _process_url

        page = _AfkCoursePage()

        class FakeContext:
            async def new_page(self):
                return page

        async def failing_handler(_page):
            raise RuntimeError("probe boom")

        capture = AsyncMock()
        with TemporaryDirectory() as tmp:
            normal_failures = Path(tmp) / "normal-failures.json"
            probe_failures = Path(tmp) / "probe-failures.json"
            with (
                patch(
                    "core.learning.afk_runner.LEARNING_FAILURES_FILE",
                    normal_failures,
                ),
                patch(
                    "core.learning.afk_runner.ensure_controller_page",
                    new=AsyncMock(),
                ),
            ):
                keep_pending = await _process_url(
                    FakeContext(),
                    "https://kc.zhixueyun.com/#/study/course/detail/probe",
                    failing_handler,
                    capture_callback=capture,
                    failure_file=probe_failures,
                )

            self.assertTrue(keep_pending)
            self.assertTrue(page.closed)
            self.assertFalse(normal_failures.exists())
            self.assertEqual(
                [item.args[1] for item in capture.await_args_list],
                ["page_created", "after_navigation", "error"],
            )
            self.assertEqual(
                _read_learning_failures(probe_failures)[0]["reason"],
                "retryable_error",
            )

    async def test_process_url_records_and_reraises_waf_block(self):
        from core.abort import WafBlockError
        from core.learning.afk_runner import _process_url

        page = _AfkCoursePage()

        class FakeContext:
            async def new_page(self):
                return page

        async def blocked_handler(_page):
            raise WafBlockError()

        with TemporaryDirectory() as tmp:
            probe_failures = Path(tmp) / "probe-failures.json"
            with patch(
                "core.learning.afk_runner.ensure_controller_page",
                new=AsyncMock(),
            ):
                with self.assertRaises(WafBlockError):
                    await _process_url(
                        FakeContext(),
                        "https://kc.zhixueyun.com/#/study/course/detail/waf",
                        blocked_handler,
                        failure_file=probe_failures,
                    )

            self.assertTrue(page.closed)
            self.assertEqual(
                _read_learning_failures(probe_failures)[0]["reason"],
                "waf_blocked",
            )

    async def test_process_url_classifies_invalid_course_resource_id_from_api(self):
        from core.learning.afk_runner import _process_url

        class FakeResponse:
            url = (
                "https://kc.zhixueyun.com/api/v1/course-study/"
                "course-front/info/d5832449-44e7-41da-a593-c661f27842ed"
            )
            status = 422

            async def text(self):
                return json.dumps(
                    {"errorCode": 40121, "message": "Invalid input."}
                )

        class FakeLocator:
            async def count(self):
                return 0

        class FakePage(_AfkCoursePage):
            def __init__(self):
                super().__init__()
                self.handlers = {}

            def on(self, event, handler):
                self.handlers[event] = handler

            def locator(self, _selector):
                return FakeLocator()

            async def content(self):
                return "<html><body></body></html>"

            async def goto(self, url, **kwargs):
                await super().goto(url, **kwargs)
                self.handlers["response"](FakeResponse())

        page = FakePage()

        class FakeContext:
            async def new_page(self):
                return page

        handler = AsyncMock()
        with TemporaryDirectory() as tmp:
            failures_file = Path(tmp) / "failures.json"
            with patch(
                "core.learning.afk_runner.ensure_controller_page",
                new=AsyncMock(),
            ):
                keep_pending = await _process_url(
                    FakeContext(),
                    (
                        "https://kc.zhixueyun.com/#/study/course/detail/"
                        "d5832449-44e7-41da-a593-c661f27842ed"
                    ),
                    handler,
                    failure_file=failures_file,
                )

            self.assertFalse(keep_pending)
            self.assertTrue(page.closed)
            handler.assert_not_awaited()
            failure = _read_learning_failures(failures_file)[0]
            self.assertEqual(failure["reason"], "invalid_course_link")
            self.assertIn("422/40121", failure["reason_text"])

    async def test_run_afk_once_stops_batch_after_first_waf_block(self):
        from core.abort import WafBlockError
        from core.learning.afk_runner import AfkBatch, run_afk_once

        class FakeBrowserContextManager:
            async def __aenter__(self):
                return None, object()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        urls = [
            "https://kc.zhixueyun.com/#/study/course/detail/a",
            "https://kc.zhixueyun.com/#/study/course/detail/b",
        ]
        status_messages = []
        process = AsyncMock(side_effect=WafBlockError())
        recheck = AsyncMock()
        with TemporaryDirectory() as tmp:
            learning_file = Path(tmp) / "learning.json"
            with (
                patch("core.learning.afk_runner.LEARNING_URLS_FILE", learning_file),
                patch(
                    "core.learning.afk_runner.prepare_afk_batch",
                    return_value=AfkBatch(urls=urls),
                ),
                patch(
                    "core.learning.afk_runner.create_browser_context",
                    return_value=FakeBrowserContextManager(),
                ),
                patch(
                    "core.learning.afk_runner.normalize_urls",
                    side_effect=lambda values: list(values or []),
                ),
                patch(
                    "core.learning.afk_runner.is_compliant_url_regex",
                    return_value=True,
                ),
                patch("core.learning.afk_runner._process_url", new=process),
                patch(
                    "core.learning.afk_runner._recheck_url_type_links",
                    new=recheck,
                ),
            ):
                await run_afk_once(status_callback=status_messages.append)

            self.assertEqual(process.await_count, 1)
            recheck.assert_not_awaited()
            self.assertEqual(_read_learning_queue_urls(learning_file), urls)
            self.assertTrue(any("本轮挂课已停止" in item for item in status_messages))

    async def test_process_url_clears_no_permission_and_records_reason(self):
        """无权限/资源不存在：移出课程链接，失败文档写明原因。"""
        from core.abort import NoPermissionError
        from core.learning.afk_runner import _process_url

        class FakePage:
            def __init__(self):
                self.closed = False

            def is_closed(self):
                return self.closed

            async def close(self):
                self.closed = True

        page = FakePage()

        class FakeContext:
            async def new_page(self):
                return page

        async def denied_handler(_page):
            raise NoPermissionError(
                "该资源已不存在，已从课程链接清理",
                reason="resource_gone",
                reason_text="该资源已不存在，已从课程链接清理",
            )

        with TemporaryDirectory() as tmp:
            failures_file = Path(tmp) / "failures.json"

            with (
                patch("core.learning.afk_runner.LEARNING_FAILURES_FILE", failures_file),
                patch("core.learning.afk_runner.ensure_controller_page", new=AsyncMock()),
                patch(
                    "core.learning.afk_runner.goto_and_prepare_async",
                    new=AsyncMock(),
                ),
            ):
                keep_pending = await _process_url(
                    FakeContext(),
                    "https://kc.zhixueyun.com/#/study/course/detail/gone",
                    denied_handler,
                )

            self.assertFalse(keep_pending)
            self.assertTrue(page.closed)
            self.assertEqual(
                _read_learning_failures(failures_file),
                [
                    {
                        "url": "https://kc.zhixueyun.com/#/study/course/detail/gone",
                        "reason": "resource_gone",
                        "reason_text": "该资源已不存在，已从课程链接清理",
                        "detail": {},
                    }
                ],
            )

    async def test_run_afk_once_opens_new_page_per_url_and_closes(self):
        """一门一页：每门 new_page，处理完 close，避免同页 goto 触发 errors 限流。"""
        from core.learning.afk_runner import AfkBatch, run_afk_once

        class FakeContext:
            def __init__(self):
                self.new_page_count = 0
                self.pages = []

            async def new_page(self):
                self.new_page_count += 1
                page = _AfkCoursePage()
                self.pages.append(page)
                return page

        class FakeBrowserContextManager:
            def __init__(self, context):
                self.context = context

            async def __aenter__(self):
                return None, self.context

            async def __aexit__(self, exc_type, exc, tb):
                return False

        context = FakeContext()
        with TemporaryDirectory() as tmp:
            learning_file = Path(tmp) / "learning.json"
            urls = [
                "https://kc.zhixueyun.com/#/study/course/detail/a",
                "https://kc.zhixueyun.com/#/study/course/detail/b",
                "https://kc.zhixueyun.com/#/study/course/detail/c",
            ]
            batch = AfkBatch(urls=urls)

            with (
                patch("core.learning.afk_runner.LEARNING_URLS_FILE", learning_file),
                patch("core.learning.afk_runner.prepare_afk_batch", return_value=batch),
                patch(
                    "core.learning.afk_runner.create_browser_context",
                    return_value=FakeBrowserContextManager(context),
                ),
                patch("core.learning.afk_runner.normalize_urls", side_effect=lambda urls: list(urls or [])),
                patch("core.learning.afk_runner.is_compliant_url_regex", return_value=True),
                patch("core.learning.afk_runner.ensure_controller_page", new=AsyncMock()),
                patch("core.learning.afk_runner.course_learning", new=AsyncMock()),
                patch(
                    "core.learning.afk_runner._recheck_url_type_links",
                    new=AsyncMock(side_effect=_recheck_noop),
                ),
            ):
                await run_afk_once()

        self.assertEqual(context.new_page_count, 3)
        self.assertEqual(len(context.pages), 3)
        self.assertTrue(all(page.closed for page in context.pages))
        self.assertEqual(
            [page.gotos for page in context.pages],
            [
                ["https://kc.zhixueyun.com/#/study/course/detail/a"],
                ["https://kc.zhixueyun.com/#/study/course/detail/b"],
                ["https://kc.zhixueyun.com/#/study/course/detail/c"],
            ],
        )

    async def test_run_afk_once_saves_current_and_remaining_urls_on_keyboard_interrupt(self):
        from core.abort import UserAbortRequested
        from core.learning.afk_runner import AfkBatch, run_afk_once

        class FakeContext:
            pass

        class FakeBrowserContextManager:
            async def __aenter__(self):
                return None, FakeContext()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with TemporaryDirectory() as tmp:
            learning_file = Path(tmp) / "learning.json"
            batch = AfkBatch(
                urls=[
                    "https://kc.zhixueyun.com/#/study/course/detail/a",
                    "https://kc.zhixueyun.com/#/study/course/detail/b",
                    "https://kc.zhixueyun.com/#/study/course/detail/c",
                ],
            )

            with (
                patch("core.learning.afk_runner.LEARNING_URLS_FILE", learning_file),
                patch("core.learning.afk_runner.prepare_afk_batch", return_value=batch),
                patch(
                    "core.learning.afk_runner.create_browser_context",
                    return_value=FakeBrowserContextManager(),
                ),
                patch("core.learning.afk_runner.normalize_urls", side_effect=lambda urls: list(urls or [])),
                patch("core.learning.afk_runner.is_compliant_url_regex", return_value=True),
                patch(
                    "core.learning.afk_runner._process_url",
                    new=AsyncMock(side_effect=[True, KeyboardInterrupt()]),
                ),
            ):
                with self.assertRaises(UserAbortRequested) as ctx:
                    await run_afk_once()

            self.assertEqual(
                str(ctx.exception),
                "已收到 Ctrl+C，已保存当前和剩余学习链接，程序退出",
            )
            self.assertEqual(
                _read_learning_queue_urls(learning_file),
                [
                    "https://kc.zhixueyun.com/#/study/course/detail/a",
                    "https://kc.zhixueyun.com/#/study/course/detail/b",
                    "https://kc.zhixueyun.com/#/study/course/detail/c",
                ],
            )

    async def test_run_afk_once_updates_learning_queue_to_remaining_urls_on_abort_with_save(self):
        from core.abort import UserAbortRequested
        from core.learning.afk_runner import AfkBatch, run_afk_once

        class FakeContext:
            pass

        class FakeBrowserContextManager:
            async def __aenter__(self):
                return None, FakeContext()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            learning_file = root / "learning.json"
            _write_learning_queue_fixture(
                learning_file,
                [
                    "https://kc.zhixueyun.com/#/study/course/detail/a",
                    "https://kc.zhixueyun.com/#/study/course/detail/b",
                    "https://kc.zhixueyun.com/#/study/course/detail/c",
                ],
            )
            batch = AfkBatch(
                urls=[
                    "https://kc.zhixueyun.com/#/study/course/detail/a",
                    "https://kc.zhixueyun.com/#/study/course/detail/b",
                    "https://kc.zhixueyun.com/#/study/course/detail/c",
                ],
            )

            with (
                patch("core.learning.afk_runner.LEARNING_URLS_FILE", learning_file),
                patch("core.learning.afk_runner.prepare_afk_batch", return_value=batch),
                patch(
                    "core.learning.afk_runner.create_browser_context",
                    return_value=FakeBrowserContextManager(),
                ),
                patch("core.learning.afk_runner.normalize_urls", side_effect=lambda urls: list(urls or [])),
                patch("core.learning.afk_runner.is_compliant_url_regex", return_value=True),
                patch(
                    "core.learning.afk_runner._process_url",
                    new=AsyncMock(
                        side_effect=[
                            False,
                            UserAbortRequested("已保存当前和剩余学习链接，程序退出"),
                        ]
                    ),
                ),
            ):
                with self.assertRaises(UserAbortRequested):
                    await run_afk_once()

            self.assertEqual(
                _read_learning_queue_urls(learning_file),
                [
                    "https://kc.zhixueyun.com/#/study/course/detail/b",
                    "https://kc.zhixueyun.com/#/study/course/detail/c",
                ],
            )

    async def test_run_afk_once_keeps_current_url_when_only_course_tab_is_closed(self):
        from core.learning.afk_runner import AfkBatch, run_afk_once

        class FakeBrowser:
            def is_connected(self):
                return True

        class FakePage:
            async def evaluate(self, _script):
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

            async def goto(self, url, **kwargs):
                return None

            async def close(self):
                return None

            def on(self, _event, _handler):
                return None

        class FakeContext:
            def __init__(self):
                self.browser = FakeBrowser()

            async def new_page(self):
                return FakePage()

        class FakeBrowserContextManager:
            async def __aenter__(self):
                return None, FakeContext()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with TemporaryDirectory() as tmp:
            learning_file = Path(tmp) / "learning.json"
            batch = AfkBatch(
                urls=[
                    "https://kc.zhixueyun.com/#/study/course/detail/a",
                    "https://kc.zhixueyun.com/#/study/course/detail/b",
                ],
            )

            with (
                patch("core.learning.afk_runner.LEARNING_URLS_FILE", learning_file),
                patch("core.learning.afk_runner.prepare_afk_batch", return_value=batch),
                patch(
                    "core.learning.afk_runner.create_browser_context",
                    return_value=FakeBrowserContextManager(),
                ),
                patch("core.learning.afk_runner.normalize_urls", side_effect=lambda urls: list(urls or [])),
                patch("core.learning.afk_runner.is_compliant_url_regex", return_value=True),
                patch("core.learning.afk_runner.ensure_controller_page", new=AsyncMock()),
                patch(
                    "core.learning.afk_runner.course_learning",
                    new=AsyncMock(
                        side_effect=[
                            TargetClosedError(
                                "Target page, context or browser has been closed"
                            ),
                            None,
                        ]
                    ),
                ),
                patch(
                    "core.learning.afk_runner._recheck_url_type_links",
                    new=AsyncMock(side_effect=_recheck_noop),
                ),
                patch("core.learning.afk_runner.logging.warning") as mock_warning,
            ):
                await run_afk_once()
            self.assertEqual(
                _read_learning_queue_urls(learning_file),
                ["https://kc.zhixueyun.com/#/study/course/detail/a"],
            )
            mock_warning.assert_not_called()

    async def test_run_afk_once_returns_to_menu_and_preserves_urls_when_browser_window_is_closed(self):
        from core.abort import UserCancelRequested
        from core.learning.afk_runner import AfkBatch, run_afk_once

        class FakeBrowser:
            def is_connected(self):
                return False

        class FakePage:
            async def evaluate(self, _script):
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

            async def goto(self, url, **kwargs):
                return None

            async def close(self):
                return None

            def on(self, _event, _handler):
                return None

        class FakeContext:
            def __init__(self):
                self.browser = FakeBrowser()

            async def new_page(self):
                return FakePage()

        class FakeBrowserContextManager:
            async def __aenter__(self):
                return None, FakeContext()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with TemporaryDirectory() as tmp:
            learning_file = Path(tmp) / "learning.json"
            batch = AfkBatch(
                urls=[
                    "https://kc.zhixueyun.com/#/study/course/detail/a",
                    "https://kc.zhixueyun.com/#/study/course/detail/b",
                ],
            )

            with (
                patch("core.learning.afk_runner.LEARNING_URLS_FILE", learning_file),
                patch("core.learning.afk_runner.prepare_afk_batch", return_value=batch),
                patch(
                    "core.learning.afk_runner.create_browser_context",
                    return_value=FakeBrowserContextManager(),
                ),
                patch("core.learning.afk_runner.normalize_urls", side_effect=lambda urls: list(urls or [])),
                patch("core.learning.afk_runner.is_compliant_url_regex", return_value=True),
                patch("core.learning.afk_runner.ensure_controller_page", new=AsyncMock()),
                patch(
                    "core.learning.afk_runner.course_learning",
                    new=AsyncMock(
                        side_effect=TargetClosedError(
                            "Target page, context or browser has been closed"
                        )
                    ),
                ),
                patch("core.learning.afk_runner.logging.warning") as mock_warning,
            ):
                with self.assertRaises(UserCancelRequested) as ctx:
                    await run_afk_once()

            self.assertIn("返回主菜单", str(ctx.exception))
            # 浏览器关闭时剩余链接（含正在处理的）已写回，不丢失
            self.assertEqual(_read_learning_queue_urls(learning_file), batch.urls)
            mock_warning.assert_not_called()

    async def test_open_course_page_returns_to_menu_when_browser_closed_before_new_page(self):
        """浏览器在 new_page 阶段被关闭时，应返回主菜单而非裸异常退出。"""
        from core.abort import UserCancelRequested
        from core.learning.afk_runner import _open_course_page

        class FakeBrowser:
            def is_connected(self):
                return False

        class FakeContext:
            def __init__(self):
                self.browser = FakeBrowser()

            async def new_page(self):
                raise TargetClosedError(
                    "Target page, context or browser has been closed"
                )

        with patch("core.learning.afk_runner.ensure_controller_page", new=AsyncMock()):
            with self.assertRaises(UserCancelRequested) as ctx:
                await _open_course_page(FakeContext())

        self.assertIn("返回主菜单", str(ctx.exception))

    async def test_run_afk_once_returns_to_menu_when_browser_closed_during_setup(self):
        """浏览器启动/认证阶段被关闭时，也返回主菜单。"""
        from core.abort import UserCancelRequested
        from core.learning.afk_runner import AfkBatch, run_afk_once

        class FakeBrowserContextManager:
            async def __aenter__(self):
                raise TargetClosedError(
                    "Target page, context or browser has been closed"
                )

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with TemporaryDirectory() as tmp:
            learning_file = Path(tmp) / "learning.json"
            batch = AfkBatch(
                urls=["https://kc.zhixueyun.com/#/study/course/detail/a"],
            )

            with (
                patch("core.learning.afk_runner.LEARNING_URLS_FILE", learning_file),
                patch("core.learning.afk_runner.prepare_afk_batch", return_value=batch),
                patch(
                    "core.learning.afk_runner.create_browser_context",
                    return_value=FakeBrowserContextManager(),
                ),
                patch("core.learning.afk_runner.normalize_urls", side_effect=lambda urls: list(urls or [])),
            ):
                with self.assertRaises(UserCancelRequested) as ctx:
                    await run_afk_once()

            self.assertIn("返回主菜单", str(ctx.exception))
            self.assertEqual(
                _read_learning_queue_urls(learning_file),
                ["https://kc.zhixueyun.com/#/study/course/detail/a"],
            )

    async def test_run_afk_once_returns_to_menu_on_cancelled_error(self):
        """TUI Ctrl+C 经跨线程取消产生 CancelledError：保存剩余链接后返回主菜单。"""
        from core.abort import UserCancelRequested
        from core.learning.afk_runner import AfkBatch, run_afk_once

        class FakeContext:
            pass

        class FakeBrowserContextManager:
            async def __aenter__(self):
                return None, FakeContext()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with TemporaryDirectory() as tmp:
            learning_file = Path(tmp) / "learning.json"
            batch = AfkBatch(
                urls=[
                    "https://kc.zhixueyun.com/#/study/course/detail/a",
                    "https://kc.zhixueyun.com/#/study/course/detail/b",
                ],
            )

            with (
                patch("core.learning.afk_runner.LEARNING_URLS_FILE", learning_file),
                patch("core.learning.afk_runner.prepare_afk_batch", return_value=batch),
                patch(
                    "core.learning.afk_runner.create_browser_context",
                    return_value=FakeBrowserContextManager(),
                ),
                patch("core.learning.afk_runner.normalize_urls", side_effect=lambda urls: list(urls or [])),
                patch("core.learning.afk_runner.is_compliant_url_regex", return_value=True),
                patch(
                    "core.learning.afk_runner._process_url",
                    new=AsyncMock(side_effect=[True, asyncio.CancelledError()]),
                ),
            ):
                with self.assertRaises(UserCancelRequested) as ctx:
                    await run_afk_once()

            self.assertIn("返回主菜单", str(ctx.exception))
            self.assertEqual(
                _read_learning_queue_urls(learning_file),
                [
                    "https://kc.zhixueyun.com/#/study/course/detail/a",
                    "https://kc.zhixueyun.com/#/study/course/detail/b",
                ],
            )


class ExamAttemptRoutingTests(unittest.TestCase):
    def test_classify_exam_entry_url_uses_explicit_hash_routes(self):
        from core.exam.runner import classify_exam_entry_url

        self.assertEqual(
            classify_exam_entry_url(
                "https://kc.zhixueyun.com/#/study/subject/detail/test-subject"
            ),
            "subject",
        )
        self.assertEqual(
            classify_exam_entry_url(
                "https://kc.zhixueyun.com/#/study/course/detail/test-course"
            ),
            "course",
        )
        self.assertEqual(
            classify_exam_entry_url(
                "https://kc.zhixueyun.com/#/exam/exam/answer-paper/test-paper"
            ),
            "exam",
        )
        self.assertEqual(
            classify_exam_entry_url("https://example.com/?next=exam/course"),
            "unknown",
        )

    def test_parse_remaining_attempts_extracts_integer(self):
        from core.exam.runner import parse_remaining_attempts

        self.assertEqual(parse_remaining_attempts("开始考试 剩余12次"), 12)

    def test_parse_remaining_attempts_returns_none_when_unlimited(self):
        from core.exam.runner import parse_remaining_attempts

        self.assertIsNone(parse_remaining_attempts("开始考试"))

class AiExamRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_answer_question_accepts_single_mode_without_item_wrapper(self):
        from core.exam import runner as exam_runner

        class FakeLocator:
            def __init__(self, count=0, text=""):
                self._count = count
                self._text = text

            @property
            def first(self):
                return self

            @property
            def last(self):
                return self

            async def count(self):
                return self._count

            async def inner_text(self):
                return self._text

        class FakePage:
            def locator(self, selector):
                return {
                    ".question-type-item": FakeLocator(),
                    ".o-score": FakeLocator(1, "单选题（2分）"),
                    ".single-title .rich-text-style": FakeLocator(1, "测试题干"),
                }[selector]

        self.assertTrue(await exam_runner._has_ready_answer_question(FakePage()))

    async def test_locate_exam_button_ignores_hidden_matches(self):
        from core.exam import runner as exam_runner

        class HiddenLocator:
            async def count(self):
                return 1

            def nth(self, _index):
                return self

            async def is_visible(self):
                return False

        class FakePage:
            def locator(self, _selector):
                return HiddenLocator()

        self.assertIsNone(await exam_runner._locate_exam_button(FakePage()))

    async def test_wait_for_target_route_follows_auth_round_trip(self):
        from core.exam import runner as exam_runner

        class FakePage:
            def __init__(self):
                self.url = "https://kc.zhixueyun.com/oauth/#login/token"
                self.wait_count = 0

            async def wait_for_timeout(self, _milliseconds):
                self.wait_count += 1
                self.url = (
                    "https://open.mylearning.cn/open/authorize"
                    if self.wait_count == 1
                    else "https://kc.zhixueyun.com/#/exam/exam/answer-paper/a"
                )

            async def wait_for_load_state(self, _state):
                return None

        page = FakePage()
        with patch(
            "core.exam.runner._is_paper_entry_ready",
            new=AsyncMock(return_value=True),
        ):
            self.assertTrue(
                await exam_runner._wait_for_target_route_after_auth(
                    page,
                    "https://kc.zhixueyun.com/#/exam/exam/answer-paper/a",
                    timeout_ms=1000,
                    interval_ms=100,
                )
            )
        self.assertEqual(page.wait_count, 2)

    async def test_wait_for_target_route_accepts_completed_redirect(self):
        from core.exam import runner as exam_runner

        class FakePage:
            url = "https://kc.zhixueyun.com/#/exam/exam/answer-paper/a"

            def __init__(self):
                self.load_count = 0
                self.wait_count = 0

            async def wait_for_load_state(self, _state):
                self.load_count += 1

            async def wait_for_timeout(self, _milliseconds):
                self.wait_count += 1

        page = FakePage()
        with (
            patch(
                "core.exam.runner._has_authorization_cookie",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "core.exam.runner._is_paper_entry_ready",
                new=AsyncMock(return_value=True),
            ),
        ):
            self.assertTrue(
                await exam_runner._wait_for_target_route_after_auth(
                    page,
                    page.url,
                    timeout_ms=1000,
                    interval_ms=100,
                )
            )
        self.assertEqual(page.load_count, 1)
        self.assertEqual(page.wait_count, 0)

    async def test_wait_for_target_route_does_not_accept_target_url_without_auth_or_dom(self):
        from core.exam import runner as exam_runner

        class FakePage:
            url = "https://kc.zhixueyun.com/#/exam/exam/answer-paper/a"

            def __init__(self):
                self.wait_count = 0

            async def wait_for_timeout(self, _milliseconds):
                self.wait_count += 1

        page = FakePage()
        with (
            patch(
                "core.exam.runner._has_authorization_cookie",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "core.exam.runner._is_paper_entry_ready",
                new=AsyncMock(return_value=False),
            ),
        ):
            completed = await exam_runner._wait_for_target_route_after_auth(
                page,
                page.url,
                timeout_ms=300,
                interval_ms=100,
            )

        self.assertFalse(completed)
        self.assertEqual(page.wait_count, 3)

    async def test_wait_for_target_route_zero_timeout_waits_until_dom_is_ready(self):
        from core.exam import runner as exam_runner

        class FakePage:
            url = "https://kc.zhixueyun.com/#/exam/exam/answer-paper/a"

            def __init__(self):
                self.wait_count = 0
                self.load_states = []

            async def wait_for_timeout(self, _milliseconds):
                self.wait_count += 1

            async def wait_for_load_state(self, state, **_kwargs):
                self.load_states.append(state)

        page = FakePage()
        ready_states = iter((False, False, True))
        with (
            patch(
                "core.exam.runner._has_authorization_cookie",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "core.exam.runner._is_paper_entry_ready",
                new=AsyncMock(side_effect=lambda _page: next(ready_states)),
            ),
        ):
            completed = await exam_runner._wait_for_target_route_after_auth(
                page,
                page.url,
                timeout_ms=0,
                interval_ms=100,
            )

        self.assertTrue(completed)
        self.assertEqual(page.wait_count, 2)
        self.assertEqual(page.load_states, ["load", "networkidle"])

    async def test_open_paper_answer_page_accepts_current_page_navigation(self):
        from core.exam import runner as exam_runner

        class FakePopupContext:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                raise exam_runner.PlaywrightTimeoutError("no popup")

        class FakePage:
            def expect_popup(self, timeout):
                self.popup_timeout = timeout
                return FakePopupContext()

        page = FakePage()
        button = AsyncMock()
        with patch(
            "core.exam.runner._is_direct_answer_paper_page",
            new=AsyncMock(return_value=True),
        ):
            answer_page = await exam_runner._open_paper_answer_page(
                page, button, popup_timeout_ms=100
            )

        self.assertIs(answer_page, page)
        self.assertEqual(page.popup_timeout, 100)
        button.click.assert_awaited_once_with()

    def test_build_exam_client_uses_openai_completion_config(self):
        from core.exam import runner as exam_runner

        with (
            patch("core.exam.runner.OPENAI_COMPLETION_BASE_URL", "https://openai-compatible.example/v1"),
            patch("core.exam.runner.OPENAI_COMPLETION_API_KEY", "test-key"),
            patch("core.exam.runner.MODEL_NAME", "test-model"),
            patch("core.exam.runner.OpenAI") as mock_openai,
        ):
            client, model = exam_runner._build_exam_client()

        mock_openai.assert_called_once_with(
            api_key="test-key",
            base_url="https://openai-compatible.example/v1",
            timeout=exam_runner.AI_REQUEST_TIMEOUT,
        )
        self.assertEqual(client, mock_openai.return_value)
        self.assertEqual(model, "test-model")

    async def test_run_course_ai_exam_continues_ai_when_course_exam_is_in_progress(self):
        from core.exam.runner import _run_course_ai_exam

        class FakeLocator:
            def __init__(self, *, count=0, text=""):
                self._count = count
                self._text = text

            async def count(self):
                return self._count

            async def inner_text(self):
                return self._text

        class FakePage:
            async def evaluate(self, _script):
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

            def __init__(self):
                self.url = "https://kc.zhixueyun.com/#/study/course/detail/test-course"
                self.status_text = "考试中"
                self._exam_button = FakeLocator(count=1, text="继续考试")

            def locator(self, selector):
                if selector == ".btn.new-radius":
                    return self._exam_button
                if selector == ".neer-status":
                    return FakeLocator(count=1, text=self.status_text)
                raise KeyError(selector)

        page = FakePage()

        async def finish_exam(*args, **kwargs):
            page.status_text = "已通过"

        with (
            patch("core.exam.runner._open_course_exam_tab", new=AsyncMock()),
            patch("core.exam.runner.check_exam_passed", new=AsyncMock(return_value=True)) as mock_check_passed,
            patch("core.exam.runner.wait_for_finish_test", new=AsyncMock(side_effect=finish_exam)) as mock_wait,
            patch("core.exam.runner._handle_exam_result", new=AsyncMock()),
        ):
            await _run_course_ai_exam(page, page.url, object(), "test-model")

        mock_wait.assert_awaited_once()
        mock_check_passed.assert_awaited_once()

    async def test_run_course_ai_exam_retries_failed_result_once_then_records_model_and_manual(self):
        from core.exam.runner import _run_course_ai_exam

        class FakeLocator:
            def __init__(self, *, count=0, text=""):
                self._count = count
                self._text = text

            async def count(self):
                return self._count

            async def inner_text(self):
                return self._text

        class FakePage:
            async def evaluate(self, _script):
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

            def __init__(self):
                self.url = "https://kc.zhixueyun.com/#/study/course/detail/test-course"
                self._exam_button = FakeLocator(count=1, text="继续考试 剩余2次")
                self._status = FakeLocator(count=1, text="未通过")

            def locator(self, selector):
                if selector == ".btn.new-radius":
                    return self._exam_button
                if selector == ".neer-status":
                    return self._status
                raise KeyError(selector)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            manual_file = root / "manual.json"
            exam_file = root / "exam.json"
            page = FakePage()
            _write_exam_queue_fixture(exam_file, [page.url])

            with (
                patch("core.exam.runner.EXAM_URLS_FILE", exam_file),
                patch("core.exam.runner.MANUAL_EXAM_FILE", manual_file),
                patch("core.exam.runner.AI_REQUEST_TYPE", "responses"),
                patch("core.exam.runner.AI_ENABLE_WEB_SEARCH", False),
                patch("core.exam.runner.AI_ENABLE_THINKING", False),
                patch("core.exam.runner.AI_REASONING_EFFORT", None),
                patch("core.exam.runner._open_course_exam_tab", new=AsyncMock()),
                patch(
                    "core.exam.runner.check_exam_passed",
                    new=AsyncMock(side_effect=[False, False]),
                ) as mock_check_passed,
                patch("core.exam.runner.wait_for_finish_test", new=AsyncMock()) as mock_wait,
                patch("core.exam.runner._handle_exam_result", new=AsyncMock()),
            ):
                await _run_course_ai_exam(page, page.url, object(), "test-model")

            mock_wait.assert_awaited_once()
            self.assertEqual(mock_check_passed.await_count, 2)
            manual_entries = _read_manual_exam_queue(manual_file)
            self.assertEqual(len(manual_entries), 1)
            self.assertEqual(manual_entries[0]["url"], page.url)
            self.assertEqual(manual_entries[0]["reason"], "ai_failed")
            self.assertEqual(
                manual_entries[0]["ai_failed_model_configs"],
                [_model_config()],
            )
            entries = json.loads(exam_file.read_text(encoding="utf-8"))
            self.assertEqual(
                entries[0]["ai_failed_model_configs"],
                [_model_config()],
            )

    async def test_run_ai_exam_batch_keeps_empty_exam_queue_file_after_processing(self):
        from core.exam.runner import run_ai_exam_batch

        class FakePage:
            async def evaluate(self, _script):
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

            async def goto(self, url, **kwargs):
                return None

            async def wait_for_load_state(self, state):
                return None

            async def close(self):
                return None

        class FakeContext:
            async def new_page(self):
                return FakePage()

        class FakeBrowserContextManager:
            async def __aenter__(self):
                return None, FakeContext()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            exam_file = root / "exam.json"
            manual_file = root / "manual.json"
            _write_exam_queue_fixture(
                exam_file,
                ["https://kc.zhixueyun.com/#/study/course/detail/test-course"],
            )

            with (
                patch("core.exam.runner.EXAM_URLS_FILE", exam_file),
                patch("core.exam.runner.MANUAL_EXAM_FILE", manual_file),
                patch(
                    "core.exam.runner.create_browser_context",
                    return_value=FakeBrowserContextManager(),
                ),
                patch("core.exam.runner._build_exam_client", return_value=(object(), "test-model")),
                patch("core.exam.runner._run_course_ai_exam", new=AsyncMock(return_value=None)) as mock_run_exam,
            ):
                manual_count = await run_ai_exam_batch(auto_submit=False)

            self.assertEqual(manual_count, 0)
            self.assertTrue(exam_file.exists())
            self.assertEqual(json.loads(exam_file.read_text(encoding="utf-8")), [])
            mock_run_exam.assert_awaited_once()
            self.assertFalse(mock_run_exam.await_args.kwargs["auto_submit"])

    async def test_run_ai_exam_batch_preserves_queue_when_redirected_to_login(self):
        from core.abort import UserCancelRequested
        from core.exam.runner import run_ai_exam_batch

        class FakePage:
            def __init__(self):
                self.url = "https://open.mylearning.cn/open/authorize"

            async def goto(self, _url, **_kwargs):
                return None

            async def wait_for_load_state(self, _state):
                return None

            async def title(self):
                return "智慧学习平台登录"

            async def close(self):
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

        class FakeContext:
            async def new_page(self):
                return FakePage()

        class FakeBrowserContextManager:
            async def __aenter__(self):
                return None, FakeContext()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            exam_file = root / "exam.json"
            manual_file = root / "manual.json"
            urls = [
                "https://kc.zhixueyun.com/#/exam/exam/answer-paper/a",
                "https://kc.zhixueyun.com/#/exam/exam/answer-paper/b",
            ]
            _write_exam_queue_fixture(exam_file, urls)

            with (
                patch("core.exam.runner.EXAM_URLS_FILE", exam_file),
                patch("core.exam.runner.MANUAL_EXAM_FILE", manual_file),
                patch(
                    "core.exam.runner.create_browser_context",
                    return_value=FakeBrowserContextManager(),
                ),
                patch("core.exam.runner._build_exam_client", return_value=(object(), "test-model")),
                patch(
                    "core.exam.runner._run_paper_ai_exam",
                    new=AsyncMock(
                        side_effect=UserCancelRequested(
                            "已保留当前及剩余考试链接，请先更新登录凭证后重试"
                        )
                    ),
                ),
            ):
                with self.assertRaises(UserCancelRequested) as ctx:
                    await run_ai_exam_batch()

            self.assertIn("更新登录凭证", str(ctx.exception))
            self.assertEqual(_read_exam_queue_urls(exam_file), urls)
            self.assertFalse(manual_file.exists())

    async def test_run_ai_exam_batch_keeps_page_open_after_question_extraction_failure(self):
        from core.abort import UserCancelRequested
        from core.exam.flow import ExamQuestionExtractionError
        from core.exam.runner import run_ai_exam_batch

        class FakeBrowser:
            def is_connected(self):
                return True

        class FakePage:
            def __init__(self):
                self.url = "https://kc.zhixueyun.com/#/exam/exam/answer-paper/a"
                self.close_waits = []
                self.close_count = 0

            async def goto(self, _url, **_kwargs):
                return None

            async def wait_for_load_state(self, _state):
                return None

            async def wait_for_event(self, event, timeout=0):
                self.close_waits.append((event, timeout))

            async def close(self):
                self.close_count += 1

        page = FakePage()

        class FakeContext:
            browser = FakeBrowser()

            async def new_page(self):
                return page

        class FakeBrowserContextManager:
            async def __aenter__(self):
                return None, FakeContext()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        async def fail_extraction(*_args, **_kwargs):
            raise ExamQuestionExtractionError("无法提取任何题目信息", page=page)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            exam_file = root / "exam.json"
            manual_file = root / "manual.json"
            urls = [
                page.url,
                "https://kc.zhixueyun.com/#/exam/exam/answer-paper/b",
            ]
            _write_exam_queue_fixture(exam_file, urls)
            status_messages = []

            with (
                patch("core.exam.runner.EXAM_URLS_FILE", exam_file),
                patch("core.exam.runner.MANUAL_EXAM_FILE", manual_file),
                patch(
                    "core.exam.runner.create_browser_context",
                    return_value=FakeBrowserContextManager(),
                ),
                patch("core.exam.runner._build_exam_client", return_value=(object(), "test-model")),
                patch("core.exam.runner._run_paper_ai_exam", new=fail_extraction),
            ):
                with self.assertRaises(UserCancelRequested) as ctx:
                    await run_ai_exam_batch(status_callback=status_messages.append)

            self.assertIn("答题页将保持打开", str(ctx.exception))
            self.assertEqual(page.close_waits, [("close", 0)])
            self.assertEqual(page.close_count, 1)
            self.assertEqual(_read_exam_queue_urls(exam_file), urls)
            self.assertFalse(manual_file.exists())
            self.assertTrue(
                any("答题页将保持打开" in message for message in status_messages)
            )

    async def test_run_ai_exam_batch_skips_link_when_current_model_already_failed_it(self):
        from core.exam.runner import run_ai_exam_batch

        class FakePage:
            async def evaluate(self, _script):
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

            async def goto(self, url, **kwargs):
                return None

            async def wait_for_load_state(self, state):
                return None

            async def close(self):
                return None

        class FakeContext:
            async def new_page(self):
                return FakePage()

        class FakeBrowserContextManager:
            async def __aenter__(self):
                return None, FakeContext()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            exam_file = root / "exam.json"
            manual_file = root / "manual.json"
            url = "https://kc.zhixueyun.com/#/study/course/detail/test-course"
            _write_exam_queue_fixture(exam_file, [url], {url: [_model_config()]})

            with (
                patch("core.exam.runner.EXAM_URLS_FILE", exam_file),
                patch("core.exam.runner.MANUAL_EXAM_FILE", manual_file),
                patch("core.exam.runner.AI_REQUEST_TYPE", "responses"),
                patch("core.exam.runner.AI_ENABLE_WEB_SEARCH", False),
                patch("core.exam.runner.AI_ENABLE_THINKING", False),
                patch("core.exam.runner.AI_REASONING_EFFORT", None),
                patch(
                    "core.exam.runner.create_browser_context",
                    return_value=FakeBrowserContextManager(),
                ),
                patch("core.exam.runner._build_exam_client", return_value=(object(), "test-model")),
                patch("core.exam.runner._run_course_ai_exam", new=AsyncMock()) as mock_run_exam,
                patch("core.exam.runner.logging.info") as mock_info,
            ):
                status_messages: list[str] = []
                manual_count = await run_ai_exam_batch(status_callback=status_messages.append)

            self.assertEqual(manual_count, 0)
            self.assertEqual(_read_exam_queue_urls(exam_file), [url])
            self.assertFalse(manual_file.exists())
            mock_run_exam.assert_not_awaited()
            self.assertFalse(
                any("更换模型" in message for message in status_messages)
            )
            self.assertTrue(
                any("更换模型" in call.args[0] for call in mock_info.call_args_list)
            )

    async def test_run_paper_ai_exam_uses_direct_answer_page_without_start_button(self):
        from core.exam.runner import _run_paper_ai_exam

        class FakeLocator:
            def __init__(self, *, count=0, text=""):
                self._count = count
                self._text = text

            @property
            def first(self):
                return self

            async def count(self):
                return self._count

            async def wait_for(self, timeout=0, state="visible"):
                if self._count <= 0:
                    raise AssertionError("wait_for should not be called for missing direct-answer selector")

            async def inner_text(self):
                return self._text

        class FakePage:
            async def evaluate(self, _script):
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

            def __init__(self):
                self.url = "https://kc.zhixueyun.com/#/exam/exam/answer-paper/test-paper"
                self._locators = {
                    ".banner-handler-btn.themeColor-border-color.themeColor-background-color": FakeLocator(count=0),
                    "button:has-text('提交')": FakeLocator(count=0),
                    "button:has-text('确认')": FakeLocator(count=0),
                    "button:has-text('下一题')": FakeLocator(count=0),
                    ".single-btn-next": FakeLocator(count=0),
                    ".btn.new-radius": FakeLocator(count=0),
                    ".question-type-item": FakeLocator(count=5),
                    ".single-title": FakeLocator(count=0),
                    ".single-btns": FakeLocator(count=0),
                    ".question-type-item, .single-title, .single-btns": FakeLocator(count=1),
                }

            def locator(self, selector):
                return self._locators[selector]

        page = FakePage()
        client = object()

        with (
            patch(
                "core.exam.runner._raise_if_login_required", new=AsyncMock()
            ),
            patch(
                "core.exam.runner._handle_attempt_limit_if_present",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "core.exam.runner._has_ready_answer_question",
                new=AsyncMock(return_value=True),
            ),
            patch("core.exam.runner.ai_exam", new=AsyncMock(return_value=None)) as mock_ai_exam,
            patch("core.exam.runner.AI_REQUEST_TYPE", "responses"),
            patch("core.exam.runner.AI_ENABLE_WEB_SEARCH", False),
            patch("core.exam.runner.AI_ENABLE_THINKING", False),
            patch("core.exam.runner.AI_REASONING_EFFORT", None),
        ):
            await _run_paper_ai_exam(page, page.url, client, "test-model")

        mock_ai_exam.assert_awaited_once_with(
            client,
            "test-model",
            page,
            page.url,
            auto_submit=True,
            ai_model_config=_model_config(),
        )

    async def test_run_paper_ai_exam_skips_gracefully_when_attempt_limit_page_is_shown(self):
        from core.exam import runner as exam_runner

        class FakeLocator:
            def __init__(self, *, count=0, text="", wait_error=None):
                self._count = count
                self._text = text
                self._wait_error = wait_error

            @property
            def first(self):
                return self

            async def count(self):
                return self._count

            async def wait_for(self, timeout=0, state="visible"):
                if self._wait_error:
                    raise self._wait_error
                if self._count <= 0:
                    raise RuntimeError("wait_for called for missing locator")

            async def inner_text(self):
                return self._text

        class FakePage:
            async def evaluate(self, _script):
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

            def __init__(self):
                self.url = "https://kc.zhixueyun.com/#/exam/exam/answer-paper/test-paper"
                self._locators = {
                    ".question-type-item, .single-title, .single-btns": FakeLocator(
                        count=0,
                        wait_error=RuntimeError(
                            'Locator.wait_for: Timeout 5000ms exceeded.\n'
                            'Call log:\n'
                            '  - waiting for locator(".question-type-item, .single-title, .single-btns") to be visible\n'
                        ),
                    ),
                    ".banner-handler-btn.themeColor-border-color.themeColor-background-color": FakeLocator(
                        count=0,
                        wait_error=RuntimeError(
                            'Locator.wait_for: Timeout 5000ms exceeded.\n'
                            'Call log:\n'
                            '  - waiting for locator(".banner-handler-btn.themeColor-border-color.themeColor-background-color") to be visible\n'
                        ),
                    ),
                    "button:has-text('提交')": FakeLocator(count=0),
                    "button:has-text('确认')": FakeLocator(count=0),
                    "button:has-text('下一题')": FakeLocator(count=0),
                    ".single-btn-next": FakeLocator(count=0),
                    ".btn.new-radius": FakeLocator(count=0),
                    "[data-region='modal:modal']": FakeLocator(
                        count=1,
                        text="当前已触发考试次数限制，不能再次进入考试详情页",
                    ),
                    "body": FakeLocator(
                        count=1,
                        text="当前已触发考试次数限制，不能再次进入考试详情页",
                    ),
                }

            def locator(self, selector):
                return self._locators[selector]

        page = FakePage()

        with TemporaryDirectory() as tmp:
            manual_file = Path(tmp) / "manual.json"
            with (
                patch("core.exam.runner.MANUAL_EXAM_FILE", manual_file),
                patch(
                    "core.exam.runner._raise_if_login_required", new=AsyncMock()
                ),
                patch("core.exam.runner.ai_exam", new=AsyncMock(return_value=None)) as mock_ai_exam,
                patch("core.exam.runner.logging.info") as mock_info,
            ):
                await exam_runner._run_paper_ai_exam(page, page.url, object(), "test-model")

            mock_ai_exam.assert_not_awaited()
            self.assertEqual(
                _read_manual_exam_queue(manual_file),
                [
                    {
                        "url": page.url,
                        "reason": "attempt_limit",
                        "reason_text": "当前已触发考试次数限制，不能再次进入考试详情页",
                        "remaining_attempts": 0,
                        "threshold": 1,
                        "ai_failed_model_configs": [],
                    }
                ],
            )
            self.assertTrue(
                any("考试次数限制" in call.args[0] for call in mock_info.call_args_list)
            )

    async def test_run_ai_exam_batch_propagates_user_abort_requested(self):
        from core.abort import UserAbortRequested
        from core.exam.runner import run_ai_exam_batch

        class FakePage:
            async def evaluate(self, _script):
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

            def __init__(self):
                self.url = "https://kc.zhixueyun.com/#/exam/exam/answer-paper/test-paper"

            async def goto(self, url, **kwargs):
                return None

            async def wait_for_load_state(self, state):
                return None

            async def close(self):
                return None

        class FakeContext:
            async def new_page(self):
                return FakePage()

        class FakeBrowserContextManager:
            async def __aenter__(self):
                return None, FakeContext()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            exam_file = root / "exam.json"
            manual_file = root / "manual.json"
            _write_exam_queue_fixture(
                exam_file,
                ["https://kc.zhixueyun.com/#/exam/exam/answer-paper/test-paper"],
            )

            with (
                patch("core.exam.runner.EXAM_URLS_FILE", exam_file),
                patch("core.exam.runner.MANUAL_EXAM_FILE", manual_file),
                patch(
                    "core.exam.runner.create_browser_context",
                    return_value=FakeBrowserContextManager(),
                ),
                patch("core.exam.runner._build_exam_client", return_value=(object(), "test-model")),
                patch(
                    "core.exam.runner._run_paper_ai_exam",
                    new=AsyncMock(
                        side_effect=UserAbortRequested(
                            "考试已超过时长，系统已自动交卷，程序退出",
                            save_pending_urls=False,
                        )
                    ),
                ),
            ):
                with self.assertRaises(UserAbortRequested):
                    await run_ai_exam_batch()

    async def test_run_ai_exam_batch_propagates_exam_ai_configuration_error_without_saving_manual(self):
        from core.exam.answers import ExamAiConfigurationError
        from core.exam.runner import run_ai_exam_batch

        class FakePage:
            async def evaluate(self, _script):
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

            async def goto(self, url, **kwargs):
                return None

            async def wait_for_load_state(self, state):
                return None

            async def close(self):
                return None

        class FakeContext:
            async def new_page(self):
                return FakePage()

        class FakeBrowserContextManager:
            async def __aenter__(self):
                return None, FakeContext()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            exam_file = root / "exam.json"
            manual_file = root / "manual.json"
            _write_exam_queue_fixture(
                exam_file,
                ["https://kc.zhixueyun.com/#/study/course/detail/test-course"],
            )

            with (
                patch("core.exam.runner.EXAM_URLS_FILE", exam_file),
                patch("core.exam.runner.MANUAL_EXAM_FILE", manual_file),
                patch(
                    "core.exam.runner.create_browser_context",
                    return_value=FakeBrowserContextManager(),
                ),
                patch("core.exam.runner._build_exam_client", return_value=(object(), "test-model")),
                patch(
                    "core.exam.runner._run_course_ai_exam",
                    new=AsyncMock(
                        side_effect=ExamAiConfigurationError("AI 配置错误")
                    ),
                ),
            ):
                with self.assertRaises(ExamAiConfigurationError):
                    await run_ai_exam_batch()

            self.assertFalse(manual_file.exists())

    async def test_run_manual_paper_exam_waits_on_direct_answer_page(self):
        from core.exam.runner import _run_manual_paper_exam

        class FakePage:
            def __init__(self):
                self.events = []

            async def wait_for_event(self, event, timeout=0):
                self.events.append((event, timeout))

        page = FakePage()
        with (
            patch(
                "core.exam.runner._has_ready_answer_question",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "core.exam.runner._raise_if_login_required", new=AsyncMock()
            ) as mock_auth,
            patch(
                "core.exam.runner._wait_for_manual_paper_test", new=AsyncMock()
            ) as mock_wait_popup,
        ):
            await _run_manual_paper_exam(
                page,
                "https://kc.zhixueyun.com/#/exam/exam/answer-paper/test-paper",
            )

        self.assertEqual(page.events, [("close", 0)])
        mock_auth.assert_awaited_once_with(
            page,
            "https://kc.zhixueyun.com/#/exam/exam/answer-paper/test-paper",
        )
        mock_wait_popup.assert_not_awaited()

    async def test_run_ai_exam_batch_returns_to_menu_when_browser_closed_before_new_page(self):
        from core.abort import UserCancelRequested
        from core.exam.runner import run_ai_exam_batch

        class FakeBrowser:
            def is_connected(self):
                return False

        class FakeContext:
            def __init__(self):
                self.browser = FakeBrowser()

            async def new_page(self):
                raise TargetClosedError("Target page, context or browser has been closed")

        class FakeBrowserContextManager:
            async def __aenter__(self):
                return None, FakeContext()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with TemporaryDirectory() as tmp:
            exam_file = Path(tmp) / "exam.json"
            manual_file = Path(tmp) / "manual.json"
            urls = [
                "https://kc.zhixueyun.com/#/study/course/detail/test-course-a",
                "https://kc.zhixueyun.com/#/study/course/detail/test-course-b",
            ]
            _write_exam_queue_fixture(exam_file, urls)

            with (
                patch("core.exam.runner.EXAM_URLS_FILE", exam_file),
                patch("core.exam.runner.MANUAL_EXAM_FILE", manual_file),
                patch(
                    "core.exam.runner.create_browser_context",
                    return_value=FakeBrowserContextManager(),
                ),
                patch("core.exam.runner._build_exam_client", return_value=(object(), "test-model")),
            ):
                with self.assertRaises(UserCancelRequested) as ctx:
                    await run_ai_exam_batch()

            self.assertIn("返回主菜单", str(ctx.exception))
            self.assertEqual(_read_exam_queue_urls(exam_file), urls)
            self.assertFalse(manual_file.exists())

    async def test_run_ai_exam_batch_returns_to_menu_on_cancelled_error(self):
        from core.abort import UserCancelRequested
        from core.exam.runner import run_ai_exam_batch

        class FakePage:
            async def evaluate(self, _script):
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

            async def goto(self, url, **kwargs):
                return None

            async def wait_for_load_state(self, state):
                return None

            async def close(self):
                return None

        class FakeContext:
            async def new_page(self):
                return FakePage()

        class FakeBrowserContextManager:
            async def __aenter__(self):
                return None, FakeContext()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with TemporaryDirectory() as tmp:
            exam_file = Path(tmp) / "exam.json"
            manual_file = Path(tmp) / "manual.json"
            urls = [
                "https://kc.zhixueyun.com/#/study/course/detail/test-course-a",
                "https://kc.zhixueyun.com/#/study/course/detail/test-course-b",
            ]
            _write_exam_queue_fixture(exam_file, urls)

            with (
                patch("core.exam.runner.EXAM_URLS_FILE", exam_file),
                patch("core.exam.runner.MANUAL_EXAM_FILE", manual_file),
                patch(
                    "core.exam.runner.create_browser_context",
                    return_value=FakeBrowserContextManager(),
                ),
                patch("core.exam.runner._build_exam_client", return_value=(object(), "test-model")),
                patch(
                    "core.exam.runner._run_course_ai_exam",
                    new=AsyncMock(side_effect=asyncio.CancelledError()),
                ),
            ):
                with self.assertRaises(UserCancelRequested) as ctx:
                    await run_ai_exam_batch()

            self.assertIn("返回主菜单", str(ctx.exception))
            # 取消时剩余考试链接已写回，不丢失
            self.assertEqual(_read_exam_queue_urls(exam_file), urls)
            self.assertFalse(manual_file.exists())

    async def test_run_ai_exam_batch_ignores_close_error_after_closed_exam_tab_skip(self):
        from core.exam.runner import run_ai_exam_batch

        class FakeBrowser:
            def is_connected(self):
                return True

        class FakePage:
            async def evaluate(self, _script):
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

            async def goto(self, url, **kwargs):
                return None

            async def wait_for_load_state(self, state):
                return None

            async def close(self):
                raise TargetClosedError("Target page, context or browser has been closed")

        class FakeContext:
            def __init__(self):
                self.browser = FakeBrowser()

            async def new_page(self):
                return FakePage()

        class FakeBrowserContextManager:
            async def __aenter__(self):
                return None, FakeContext()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            exam_file = root / "exam.json"
            manual_file = root / "manual.json"
            _write_exam_queue_fixture(
                exam_file,
                ["https://kc.zhixueyun.com/#/study/course/detail/test-course"],
            )

            with (
                patch("core.exam.runner.EXAM_URLS_FILE", exam_file),
                patch("core.exam.runner.MANUAL_EXAM_FILE", manual_file),
                patch(
                    "core.exam.runner.create_browser_context",
                    return_value=FakeBrowserContextManager(),
                ),
                patch("core.exam.runner._build_exam_client", return_value=(object(), "test-model")),
                patch(
                    "core.exam.runner._run_course_ai_exam",
                    new=AsyncMock(
                        side_effect=TargetClosedError(
                            "Target page, context or browser has been closed"
                        )
                    ),
                ),
            ):
                manual_count = await run_ai_exam_batch()

            self.assertEqual(manual_count, 0)
            self.assertEqual(json.loads(exam_file.read_text(encoding="utf-8")), [])
            self.assertFalse(manual_file.exists())

    async def test_run_course_ai_exam_marks_attempt_limit_when_start_exam_shows_limit_modal(self):
        from core.exam import runner as exam_runner

        class FakeLocator:
            def __init__(self, *, count=0, text=""):
                self._count = count
                self._text = text

            @property
            def first(self):
                return self

            async def count(self):
                return self._count

            async def inner_text(self):
                return self._text

        class FakePage:
            async def evaluate(self, _script):
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

            def __init__(self):
                self.url = "https://kc.zhixueyun.com/#/study/course/detail/test-course"
                self._locators = {
                    ".btn.new-radius": FakeLocator(count=1, text="开始考试"),
                    ".neer-status": FakeLocator(count=0),
                    "[data-region='modal:modal']": FakeLocator(
                        count=1,
                        text="您好，当前已触发考试次数限制，不能再次进入考试详情页",
                    ),
                    "body": FakeLocator(
                        count=1,
                        text="您好，当前已触发考试次数限制，不能再次进入考试详情页",
                    ),
                }

            def locator(self, selector):
                return self._locators[selector]

        page = FakePage()

        with TemporaryDirectory() as tmp:
            manual_file = Path(tmp) / "manual.json"
            with (
                patch("core.exam.runner.MANUAL_EXAM_FILE", manual_file),
                patch("core.exam.runner._open_course_exam_tab", new=AsyncMock()),
                patch("core.exam.runner.wait_for_finish_test", new=AsyncMock(side_effect=RuntimeError("Popup timeout"))),
                patch("core.exam.runner.logging.info") as mock_info,
            ):
                await exam_runner._run_course_ai_exam(page, page.url, object(), "test-model")

            self.assertEqual(
                _read_manual_exam_queue(manual_file),
                [
                    {
                        "url": page.url,
                        "reason": "attempt_limit",
                        "reason_text": "您好，当前已触发考试次数限制，不能再次进入考试详情页",
                        "remaining_attempts": 0,
                        "threshold": 1,
                        "ai_failed_model_configs": [],
                    }
                ],
            )
            self.assertTrue(
                any("考试次数限制" in call.args[0] for call in mock_info.call_args_list)
            )

    async def test_run_course_ai_exam_routes_to_manual_when_remaining_attempts_unparseable(self):
        from core.exam import runner as exam_runner

        class FakeLocator:
            def __init__(self, *, count=0, text=""):
                self._count = count
                self._text = text

            async def count(self):
                return self._count

            async def inner_text(self):
                return self._text

        class FakePage:
            async def evaluate(self, _script):
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

            def __init__(self):
                self.url = "https://kc.zhixueyun.com/#/study/course/detail/test-course"
                self._locators = {
                    ".btn.new-radius": FakeLocator(count=1, text="继续考试（剩余次数未知）"),
                    ".neer-status": FakeLocator(count=0),
                }

            def locator(self, selector):
                return self._locators[selector]

        page = FakePage()

        with TemporaryDirectory() as tmp:
            manual_file = Path(tmp) / "manual.json"
            with (
                patch("core.exam.runner.MANUAL_EXAM_FILE", manual_file),
                patch("core.exam.runner._open_course_exam_tab", new=AsyncMock()),
            ):
                await exam_runner._run_course_ai_exam(page, page.url, object(), "test-model")

            self.assertEqual(
                _read_manual_exam_queue(manual_file),
                [
                    {
                        "url": page.url,
                        "reason": "attempt_unknown",
                        "reason_text": "页面显示剩余次数但无法解析，转为人工考试处理",
                        "remaining_attempts": None,
                        "threshold": exam_runner.COURSE_EXAM_ATTEMPT_THRESHOLD,
                        "ai_failed_model_configs": [],
                    }
                ],
            )

    async def test_run_ai_exam_batch_routes_unknown_url_to_manual_exam(self):
        from core.exam.runner import run_ai_exam_batch

        class FakePage:
            async def evaluate(self, _script):
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

            async def goto(self, url, **kwargs):
                return None

            async def wait_for_load_state(self, state):
                return None

            async def close(self):
                return None

        class FakeContext:
            async def new_page(self):
                return FakePage()

        class FakeBrowserContextManager:
            async def __aenter__(self):
                return None, FakeContext()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        unknown_url = "https://kc.zhixueyun.com/#/study/unknown/test-resource"

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            exam_file = root / "exam.json"
            manual_file = root / "manual.json"
            _write_exam_queue_fixture(exam_file, [unknown_url])

            with (
                patch("core.exam.runner.EXAM_URLS_FILE", exam_file),
                patch("core.exam.runner.MANUAL_EXAM_FILE", manual_file),
                patch(
                    "core.exam.runner.create_browser_context",
                    return_value=FakeBrowserContextManager(),
                ),
                patch("core.exam.runner._build_exam_client", return_value=(object(), "test-model")),
            ):
                manual_count = await run_ai_exam_batch()

            self.assertEqual(manual_count, 1)
            self.assertEqual(_read_exam_queue_urls(exam_file), [])
            self.assertEqual(
                _read_manual_exam_queue(manual_file),
                [
                    {
                        "url": unknown_url,
                        "reason": "unknown_url_type",
                        "reason_text": "未知考试链接类型",
                        "remaining_attempts": None,
                        "threshold": None,
                        "ai_failed_model_configs": [],
                    }
                ],
            )

    async def test_run_manual_exam_batch_preserves_queue_on_user_cancel(self):
        from core.abort import UserCancelRequested
        from core.exam.runner import run_manual_exam_batch

        class FakePage:
            async def goto(self, _url, **_kwargs):
                return None

            async def wait_for_load_state(self, _state):
                return None

            async def close(self):
                return None

        class FakeContext:
            async def new_page(self):
                return FakePage()

        class FakeBrowserContextManager:
            async def __aenter__(self):
                return None, FakeContext()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with TemporaryDirectory() as tmp:
            manual_file = Path(tmp) / "manual.json"
            urls = [
                "https://kc.zhixueyun.com/#/study/course/detail/test-course-a",
                "https://kc.zhixueyun.com/#/study/course/detail/test-course-b",
            ]
            _write_manual_exam_queue_fixture(manual_file, urls)

            with (
                patch(
                    "core.exam.runner.create_browser_context",
                    return_value=FakeBrowserContextManager(),
                ),
                patch(
                    "core.exam.runner._run_manual_course_exam",
                    new=AsyncMock(side_effect=UserCancelRequested("返回主菜单")),
                ),
            ):
                with self.assertRaises(UserCancelRequested):
                    await run_manual_exam_batch(manual_exam_file=manual_file)

            self.assertEqual(
                [entry["url"] for entry in _read_manual_exam_queue(manual_file)],
                urls,
            )

    async def test_run_manual_exam_batch_deletes_manual_exam_file_when_all_processed(self):
        from core.exam.runner import run_manual_exam_batch

        class FakePage:
            async def evaluate(self, _script):
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

            async def goto(self, url, **kwargs):
                return None

            async def wait_for_load_state(self, state):
                return None

            async def close(self):
                return None

        class FakeContext:
            async def new_page(self):
                return FakePage()

        class FakeBrowserContextManager:
            async def __aenter__(self):
                return None, FakeContext()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with TemporaryDirectory() as tmp:
            manual_file = Path(tmp) / "manual.json"
            _write_manual_exam_queue_fixture(
                manual_file,
                ["https://kc.zhixueyun.com/#/study/course/detail/test-course"],
            )

            with (
                patch(
                    "core.exam.runner.create_browser_context",
                    return_value=FakeBrowserContextManager(),
                ),
                patch("core.exam.runner._run_manual_course_exam", new=AsyncMock(return_value=None)),
            ):
                processed = await run_manual_exam_batch(manual_exam_file=manual_file)

        self.assertEqual(processed, 1)
        self.assertFalse(manual_file.exists())

    async def test_run_manual_exam_batch_keeps_unknown_urls_for_later(self):
        from core.exam.runner import run_manual_exam_batch

        class FakePage:
            async def evaluate(self, _script):
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

            async def goto(self, url, **kwargs):
                return None

            async def wait_for_load_state(self, state):
                return None

            async def close(self):
                return None

        class FakeContext:
            async def new_page(self):
                return FakePage()

        class FakeBrowserContextManager:
            async def __aenter__(self):
                return None, FakeContext()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with TemporaryDirectory() as tmp:
            manual_file = Path(tmp) / "manual.json"
            unknown_url = "https://invalid.local/unknown"
            _write_manual_exam_queue_fixture(manual_file, [unknown_url])

            with patch(
                "core.exam.runner.create_browser_context",
                return_value=FakeBrowserContextManager(),
            ):
                processed = await run_manual_exam_batch(manual_exam_file=manual_file)

            self.assertEqual(processed, 0)
            self.assertEqual(
                [entry["url"] for entry in _read_manual_exam_queue(manual_file)],
                [unknown_url],
            )

    async def test_run_manual_exam_batch_keeps_failed_urls_and_continues(self):
        from core.exam.runner import run_manual_exam_batch

        class FakePage:
            async def evaluate(self, _script):
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

            async def goto(self, url, **kwargs):
                return None

            async def wait_for_load_state(self, state):
                return None

            async def close(self):
                return None

        class FakeContext:
            async def new_page(self):
                return FakePage()

        class FakeBrowserContextManager:
            async def __aenter__(self):
                return None, FakeContext()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with TemporaryDirectory() as tmp:
            manual_file = Path(tmp) / "manual.json"
            failed_url = "https://kc.zhixueyun.com/#/study/course/detail/test-course-a"
            passed_url = "https://kc.zhixueyun.com/#/study/course/detail/test-course-b"
            _write_manual_exam_queue_fixture(manual_file, [failed_url, passed_url])

            with (
                patch(
                    "core.exam.runner.create_browser_context",
                    return_value=FakeBrowserContextManager(),
                ),
                patch(
                    "core.exam.runner._run_manual_course_exam",
                    new=AsyncMock(side_effect=[RuntimeError("boom"), None]),
                ),
            ):
                processed = await run_manual_exam_batch(manual_exam_file=manual_file)

            self.assertEqual(processed, 1)
            self.assertEqual(
                [entry["url"] for entry in _read_manual_exam_queue(manual_file)],
                [failed_url],
            )


class RunAsyncInterruptionTests(unittest.TestCase):
    def test_interrupt_running_async_cancels_running_task(self):
        from core.config import interrupt_running_async, run_async

        started = threading.Event()
        seen_cancel = {"value": False}

        async def long_running():
            started.set()
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                seen_cancel["value"] = True
                raise

        def canceller():
            started.wait(timeout=5)
            time.sleep(0.1)
            interrupt_running_async()

        threading.Thread(target=canceller, daemon=True).start()

        with self.assertRaises(asyncio.CancelledError):
            run_async(long_running())

        self.assertTrue(seen_cancel["value"])

    def test_interrupt_running_async_returns_false_when_nothing_running(self):
        from core.config import interrupt_running_async

        self.assertFalse(interrupt_running_async())


if __name__ == "__main__":
    unittest.main()
