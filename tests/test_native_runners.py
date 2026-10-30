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

    async def test_open_course_page_cancels_when_heartbeat_closed(self):
        """心跳页（mylearning 主控页）被关 = 整窗被关：即使浏览器进程仍存活
        （Edge 后台模式、new_page 会成功），开课前也必须立即取消本轮挂课。"""
        from core.abort import UserCancelRequested
        from core.learning.afk_runner import _open_course_page

        class FakeContext:
            def __init__(self):
                self.new_page_calls = 0

            async def new_page(self):
                self.new_page_calls += 1
                raise AssertionError("整窗已关时不应再开新课")

        with (
            patch(
                "core.learning.afk_runner.ensure_controller_page",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "core.learning.afk_runner.browser_still_usable",
                new=AsyncMock(return_value=False),
            ),
        ):
            with self.assertRaises(UserCancelRequested):
                await _open_course_page(FakeContext())

    async def test_process_url_propagates_cancel_without_recording_failure(self):
        """_open_course_page 抛出的 UserCancelRequested 必须原样上抛——
        落进通用 except 会被记成「可重试失败」并继续下一门（关不掉的根源）。"""
        from core.abort import UserCancelRequested
        from core.learning.afk_runner import _process_url

        class FakeContext:
            async def new_page(self):
                raise AssertionError("不应到达")

        async def never_called(_page):
            raise AssertionError("handler 不应被执行")

        with TemporaryDirectory() as tmp:
            failures_file = Path(tmp) / "failures.json"
            with (
                patch("core.learning.afk_runner.LEARNING_FAILURES_FILE", failures_file),
                patch("core.learning.afk_runner.ensure_controller_page", new=AsyncMock()),
                patch(
                    "core.learning.afk_runner.browser_still_usable",
                    new=AsyncMock(return_value=False),
                ),
            ):
                with self.assertRaises(UserCancelRequested):
                    await _process_url(
                        FakeContext(),
                        "https://kc.zhixueyun.com/#/study/course/detail/a",
                        never_called,
                    )
            self.assertFalse(
                failures_file.exists(),
                "取消信号不得被记成挂课失败",
            )

    async def test_browser_still_usable_detects_window_close_with_alive_process(self):
        """组合判定：连接存活（Edge 后台模式）但所有页面都已关闭 = 整窗关闭。"""
        from core.learning.afk_runner import browser_still_usable

        class FakeBrowser:
            def is_connected(self):
                return True  # 模拟 Edge 后台模式：窗口关了进程还在

        class FakePage:
            def __init__(self, closed):
                self._closed = closed

            def is_closed(self):
                return self._closed

        class FakeContext:
            def __init__(self, pages):
                self.browser = FakeBrowser()
                self.pages = pages

        with patch(
            "core.learning.afk_runner._WINDOW_CLOSE_SETTLE_SECONDS", 0
        ):
            # 全部页面已关（含心跳页）→ 整窗关闭
            self.assertFalse(
                await browser_still_usable(FakeContext([FakePage(True), FakePage(True)]))
            )
            # 心跳页仍在 → 只是关了课程标签/窗口，继续
            self.assertTrue(
                await browser_still_usable(FakeContext([FakePage(True), FakePage(False)]))
            )

        class DisconnectedContext:
            def __init__(self):
                class DeadBrowser:
                    def is_connected(self):
                        return False

                self.browser = DeadBrowser()
                self.pages = [FakePage(False)]

        self.assertFalse(await browser_still_usable(DisconnectedContext()))

        class BareContext:  # 无 browser 属性的测试替身：无法证伪按存活
            pass

        self.assertTrue(await browser_still_usable(BareContext()))

    async def test_process_url_cancels_when_window_closed_with_alive_process(self):
        """端到端：课程操作抛 target-closed，连接仍显示存活（Edge 后台），
        但页面全关 → 必须取消而不是「保留链接继续下一门」。"""
        from core.abort import UserCancelRequested
        from core.learning.afk_runner import _process_url

        class FakePage:
            def is_closed(self):
                return False

            async def close(self):
                return None

            async def goto(self, _url, **_kwargs):
                # 页面刚开就遇到整窗关闭：导航即失败
                raise TargetClosedError(
                    "Target page, context or browser has been closed"
                )

        class FakeContext:
            async def new_page(self):
                return FakePage()

        async def dead_handler(_page):
            raise AssertionError("handler 不应被执行（导航阶段即失败）")

        with TemporaryDirectory() as tmp:
            failures_file = Path(tmp) / "failures.json"
            with (
                patch("core.learning.afk_runner.LEARNING_FAILURES_FILE", failures_file),
                patch("core.learning.afk_runner.ensure_controller_page", new=AsyncMock()),
                # 开课时可用；课程操作抛 target-closed 后（整窗关闭事件落定）不可用
                patch(
                    "core.learning.afk_runner.browser_still_usable",
                    new=AsyncMock(side_effect=[True, False]),
                ),
            ):
                with self.assertRaises(UserCancelRequested):
                    await _process_url(
                        FakeContext(),
                        "https://kc.zhixueyun.com/#/study/course/detail/a",
                        dead_handler,
                    )
            self.assertFalse(
                failures_file.exists(),
                "整窗关闭不得被记成挂课失败",
            )

    async def test_process_url_cancels_on_zombie_playwright_internal_error(self):
        """整窗关闭的僵尸连接可能抛 Playwright 内部 AttributeError，而非
        TargetClosedError；确认浏览器不可用后应按取消处理且不记录失败。"""
        from core.abort import UserCancelRequested
        from core.learning.afk_runner import _process_url

        class FakePage:
            def __init__(self):
                self.closed = False

            def is_closed(self):
                return self.closed

            async def goto(self, _url, **_kwargs):
                raise AttributeError("'dict' object has no attribute '_object'")

            async def close(self):
                self.closed = True

        page = FakePage()

        class FakeContext:
            async def new_page(self):
                return page

        async def never_called(_page):
            raise AssertionError("导航阶段失败后不应执行 handler")

        with TemporaryDirectory() as tmp:
            failures_file = Path(tmp) / "failures.json"
            with (
                patch("core.learning.afk_runner.LEARNING_FAILURES_FILE", failures_file),
                patch(
                    "core.learning.afk_runner.ensure_controller_page",
                    new=AsyncMock(),
                ),
                # 开课前存活；导航抛内部错误后，组合判定发现整窗已关闭。
                patch(
                    "core.learning.afk_runner.browser_still_usable",
                    new=AsyncMock(side_effect=[True, False]),
                ),
            ):
                with self.assertRaises(UserCancelRequested):
                    await _process_url(
                        FakeContext(),
                        "https://kc.zhixueyun.com/#/study/course/detail/a",
                        never_called,
                    )

            self.assertTrue(page.closed)
            self.assertFalse(
                failures_file.exists(),
                "整窗关闭的内部异常不得被记成可重试课程失败",
            )

    async def test_heartbeat_close_finishes_current_course_then_stops(self):
        """心跳页设计语义（用户确认）：课程进行中关闭心跳页 → 当前课程完整
        挂完（不被打断），下一门开课前停止并保存剩余链接。钉成回归，防止
        以后被当成偶然行为改掉。"""
        from core.abort import UserCancelRequested
        from core.browser.session import (
            _remember_controller_page,
            release_controller_page,
        )
        from core.learning.afk_runner import AfkBatch, run_afk_once

        class FakeBrowser:
            def is_connected(self):
                return True  # 模拟 Edge 后台模式：心跳关了进程仍存活

        class FakeControllerPage:
            def __init__(self):
                self.handlers: dict[str, list] = {}
                self.closed = False

            def is_closed(self):
                return self.closed

            def on(self, event, handler):
                self.handlers.setdefault(event, []).append(handler)

            def close(self):
                self.closed = True
                for handler in self.handlers.get("close", []):
                    handler()

        class FakeCoursePage:
            def __init__(self):
                self.closed = False

            def is_closed(self):
                return self.closed

            async def close(self):
                self.closed = True

            async def goto(self, _url, **_kwargs):
                return None

            async def evaluate(self, _script):
                return None  # 无顶层弹窗

            async def wait_for_timeout(self, _milliseconds):
                return None

        class FakeContext:
            def __init__(self):
                self.browser = FakeBrowser()
                self.controller = FakeControllerPage()
                # 与生产一致：心跳页由 context 创建并常驻 pages 列表
                self.pages = [self.controller]
                _remember_controller_page(self, self.controller)

            async def new_page(self):
                page = FakeCoursePage()
                self.pages.append(page)
                return page

        class FakeBrowserContextManager:
            async def __aenter__(self):
                return None, context

            async def __aexit__(self, exc_type, exc, tb):
                return False

        statuses: list[str] = []
        handler_state: dict = {}

        async def learning_handler(_page):
            handler_state["started"] = True
            await asyncio.sleep(0.3)  # 模拟挂课进行中（视频等待等）
            handler_state["completed"] = True
            return True

        context = FakeContext()
        url_a = "https://kc.zhixueyun.com/#/study/course/detail/a"
        url_b = "https://kc.zhixueyun.com/#/study/course/detail/b"

        with TemporaryDirectory() as tmp:
            learning_file = Path(tmp) / "learning.json"
            batch = AfkBatch(urls=[url_a, url_b])

            async def scenario() -> None:
                task = asyncio.create_task(run_afk_once(status_callback=statuses.append))
                deadline = asyncio.get_running_loop().time() + 5.0
                while not handler_state.get("started"):
                    self.assertLess(
                        asyncio.get_running_loop().time(),
                        deadline,
                        "课程 handler 未在预期时间内启动",
                    )
                    await asyncio.sleep(0.01)
                # 用户此刻只关心跳页：当前课程仍在挂，必须等它挂完
                context.controller.close()
                return await task

            with (
                patch("core.learning.afk_runner.LEARNING_URLS_FILE", learning_file),
                patch("core.learning.afk_runner.LEARNING_FAILURES_FILE", Path(tmp) / "f.json"),
                patch("core.learning.afk_runner.prepare_afk_batch", return_value=batch),
                patch(
                    "core.learning.afk_runner.create_browser_context",
                    return_value=FakeBrowserContextManager(),
                ),
                patch("core.learning.afk_runner.normalize_urls", side_effect=lambda urls: list(urls or [])),
                patch("core.learning.afk_runner.is_compliant_url_regex", return_value=True),
                patch("core.learning.afk_runner.course_learning", new=AsyncMock(wraps=learning_handler)),
            ):
                try:
                    with self.assertRaises(UserCancelRequested) as ctx_manager:
                        await scenario()
                finally:
                    release_controller_page(context)

            self.assertIn("心跳页已关闭", str(ctx_manager.exception))
            self.assertTrue(
                handler_state.get("completed"),
                "当前课程必须完整挂完，不能因心跳页关闭被打断",
            )
            self.assertTrue(
                any("心跳页已关闭" in message for message in statuses),
                "关闭瞬间必须有状态提示（告知停止已登记）",
            )
            # 课程 a 已完成并移出队列；课程 b 保留待下次
            self.assertEqual(_read_learning_queue_urls(learning_file), [url_b])

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
                with self.assertRaises(WafBlockError):
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
                            True,
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
            classify_exam_entry_url(
                "https://kc.zhixueyun.com/#/exam/exam/exam-detail/"
                "9ace2356-f9a1-4ad1-a0c2-5c4a1d4e6a92?from=manual"
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
        self.assertEqual(page.load_count, 0)
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

    async def test_wait_for_target_route_zero_timeout_is_bounded(self):
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

        self.assertFalse(completed)
        self.assertEqual(page.wait_count, 1)
        self.assertEqual(page.load_states, [])

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

    async def test_api_auth_wait_does_not_require_button_or_question_dom(self):
        from core.exam import runner as exam_runner

        page = AsyncMock()
        page.url = (
            "https://kc.zhixueyun.com/#/exam/exam/exam-detail/"
            "9ace2356-f9a1-4ad1-a0c2-5c4a1d4e6a92"
        )
        with (
            patch.object(exam_runner, "_has_authorization_cookie", new=AsyncMock(return_value=True)),
            patch.object(exam_runner, "_is_paper_entry_ready", new=AsyncMock()) as dom_ready,
        ):
            self.assertTrue(await exam_runner._wait_for_target_route_after_auth(
                page, page.url, require_entry_dom=False,
            ))
        dom_ready.assert_not_awaited()



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
