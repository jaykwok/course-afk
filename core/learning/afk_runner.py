from __future__ import annotations

import asyncio
import json
import logging
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from core.abort import (
    LearningFlowError,
    NoPermissionError,
    UserAbortRequested,
    UserCancelRequested,
    WafBlockError,
)
from core.browser.session import (
    create_browser_context,
    ensure_controller_page,
    get_context_browser,
    get_controller_page,
    is_browser_connected,
    is_controller_window_closed,
    is_target_closed_exception,
)
from core.config import (
    COURSE_GAP_MAX,
    COURSE_GAP_MIN,
    LEARNING_FAILURES_FILE,
    LEARNING_URLS_FILE,
    sample_afk_slow_mo,
)
from core.humanize import sample_between
from core.file_ops import (
    is_compliant_url_regex,
    is_course_detail_url,
    is_subject_detail_url,
)
from core.links import normalize_urls
from core.learning.exam_bridge import is_subject_url_completed
from core.learning.flows import course_learning, subject_learning
from core.learning.common import ensure_course_page_ready
from core.queues.learning import (
    read_learning_failures,
    read_learning_urls,
    record_learning_failure,
    remove_learning_failure,
    write_learning_urls,
)


StatusCallback = Callable[[str], None]
CaptureCallback = Callable[[object, str], Awaitable[None]]
_COURSE_INFO_API_MARKER = "/api/v1/course-study/course-front/info/"
_COURSE_BOOTSTRAP_TIMEOUT_SECONDS = 30
# 判定「整窗关闭 vs 仅关标签」前等待页面 close 事件落定的时长：
# target-closed 异常与 Playwright 内部错误（僵尸连接下 goto 抛
# AttributeError 等）都可能先于 close 事件派发到达，连接判断 / 心跳闩锁
# / pages 状态此刻尚未更新，不等就会把整窗关闭误判成「仅关标签」。
_WINDOW_CLOSE_SETTLE_SECONDS = 0.5


async def browser_still_usable(context) -> bool:
    """整窗关闭的组合判定：连接存活 且 context 中仍有打开的页面。

    单一信号都不可靠（实测，2026-09-02）：
    - is_connected：Edge 后台模式在窗口关闭后进程存活，恒 True；
    - 心跳闩锁 / is_closed 标志：依赖 close 事件派发，异常常先于事件到达；
    - 僵尸连接下 new_page 甚至能成功，随后 goto 才以内部错误崩掉。
    因此这里在事件落定后综合判断：所有页面都已关闭 = 整窗关闭 → False。
    无浏览器对象的测试替身无法证伪，按存活返回。
    """
    if get_context_browser(context) is None:
        return True
    if not is_browser_connected(context):
        return False
    pages = getattr(context, "pages", None)
    if pages is None:
        return True
    await asyncio.sleep(_WINDOW_CLOSE_SETTLE_SECONDS)
    if not is_browser_connected(context):  # 闩锁可能在落定期内置位
        return False
    return any(_is_page_open(page) for page in pages)


async def _pause_between_courses(status_callback=None) -> float:
    """两门课之间随机歇一会。

    整批链接无缝连做（上一门结束的同一秒就开下一门）在后台是一条很直的线，
    真人总要挑下一门、切页面。用 ``asyncio.sleep`` 以便 Ctrl+C / 取消能立刻打断。
    """
    pause_seconds = sample_between(COURSE_GAP_MIN, COURSE_GAP_MAX)
    if pause_seconds <= 0:
        return 0.0
    logging.info(f"课程间隔休息 {pause_seconds:.0f} 秒")
    if status_callback:
        status_callback(f"课程间隔休息 {pause_seconds:.0f} 秒")
    await asyncio.sleep(pause_seconds)
    return pause_seconds


@dataclass
class AfkBatch:
    urls: list[str]


class _CourseInfoMonitor:
    """导航前监听课程详情接口，只保留状态码与业务错误，不保存鉴权数据。"""

    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.result: dict[str, object] | None = None
        self.tasks: set[asyncio.Task] = set()

    def observe(self, response) -> None:
        response_url = str(getattr(response, "url", "") or "")
        if _COURSE_INFO_API_MARKER not in response_url:
            return
        task = asyncio.create_task(self._record(response))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _record(self, response) -> None:
        status = int(getattr(response, "status", 0) or 0)
        result: dict[str, object] = {"status": status}
        if status >= 400:
            try:
                payload = json.loads(await response.text())
            except Exception:
                payload = {}
            if isinstance(payload, dict):
                result["error_code"] = payload.get("errorCode")
                result["message"] = str(payload.get("message") or "")[:300]
        self.result = result
        self.event.set()


def _install_course_info_monitor(page) -> _CourseInfoMonitor | None:
    on = getattr(page, "on", None)
    if not callable(on):
        return None
    monitor = _CourseInfoMonitor()
    on("response", monitor.observe)
    return monitor


def _raise_for_course_info_failure(result: dict[str, object]) -> None:
    status = int(result.get("status") or 0)
    if status < 400:
        return
    error_code = result.get("error_code")
    message = str(result.get("message") or "").strip()

    if status == 422 and error_code == 40121:
        raise NoPermissionError(
            "课程链接使用了无效的资源 ID（详情接口 422/40121）",
            reason="invalid_course_link",
            reason_text=(
                "课程链接中的 UUID 不是有效课程资源 ID（详情接口 422/40121），"
                "已从课程链接清理；"
                "请重新从主题或培训班解析"
            ),
        )
    if status == 404:
        raise NoPermissionError(
            "课程详情接口返回 404",
            reason="resource_gone",
            reason_text="课程资源不存在，已从课程链接清理",
        )
    if status == 403:
        raise NoPermissionError(
            "课程详情接口返回 403",
            reason="no_permission",
            reason_text="无权限访问该课程资源，已从课程链接清理",
        )

    detail = {"http_status": status, "error_code": error_code}
    if message:
        detail["api_message"] = message
    raise LearningFlowError(
        f"课程详情接口返回 {status}",
        reason="course_info_api_error",
        reason_text=f"课程详情接口返回 {status}，已保留链接待重试",
        keep_pending=True,
        detail=detail,
    )


async def _wait_for_course_bootstrap(page, monitor: _CourseInfoMonitor | None) -> None:
    """OAuth 回跳后等待课程 API 或章节 DOM，避免在登录页提前等 ``dl``。"""
    if monitor is None:
        return

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _COURSE_BOOTSTRAP_TIMEOUT_SECONDS
    while True:
        if monitor.result is not None:
            _raise_for_course_info_failure(monitor.result)
            return

        await ensure_course_page_ready(page)
        try:
            if await page.locator("dl.chapter-list-box").count() > 0:
                return
        except Exception as exc:
            if is_target_closed_exception(exc):
                raise

        remaining = deadline - loop.time()
        if remaining <= 0:
            raise LearningFlowError(
                "课程页面在认证后未返回详情接口或章节 DOM",
                reason="course_bootstrap_timeout",
                reason_text="课程页面初始化超时，已保留链接待重试",
                keep_pending=True,
            )
        try:
            await asyncio.wait_for(monitor.event.wait(), timeout=min(1, remaining))
        except asyncio.TimeoutError:
            pass


def _write_learning_queue(
    urls: list[str], *, learning_file: Path | None = None
) -> None:
    if learning_file is None:
        learning_file = LEARNING_URLS_FILE
    if urls or learning_file.exists():
        write_learning_urls(urls, file_path=learning_file)


def _is_page_open(page) -> bool:
    """工作页是否仍可用（未 close）。"""
    if page is None:
        return False
    is_closed = getattr(page, "is_closed", None)
    try:
        if callable(is_closed):
            return not bool(is_closed())
    except Exception:
        return False
    return True


async def _close_page_quiet(page) -> None:
    if not _is_page_open(page):
        return
    try:
        await page.close()
    except Exception:
        pass


def prepare_afk_batch(
    *,
    learning_file: Path | None = None,
) -> AfkBatch:
    if learning_file is None:
        learning_file = LEARNING_URLS_FILE
    learning_urls = normalize_urls(read_learning_urls(file_path=learning_file))
    _write_learning_queue(learning_urls, learning_file=learning_file)
    return AfkBatch(urls=learning_urls)


async def _open_course_page(context):
    """
    为单门课/主题新开标签页。

    必须一门一页、处理完 close：同页 goto 下一门会被平台拦到
    /#/study/errors/...「您已打开新的课程详情页…」。心跳页（mylearning
    主控页）始终保留：它被关 = 用户关掉了整窗。开课前用组合判定拦截——
    Edge 后台模式下进程仍存活、new_page 会成功、goto 才崩，所以必须在
    开课前判断，不能等异常。
    """
    await ensure_controller_page(context)
    if not await browser_still_usable(context):
        # 心跳页被关（可能是整窗关闭，也可能是单关心跳页）时给出对应文案
        if is_controller_window_closed(context):
            raise UserCancelRequested("心跳页已关闭，已保留剩余学习链接，返回主菜单")
        raise UserCancelRequested("浏览器窗口已关闭，已保留剩余学习链接，返回主菜单")
    try:
        return await context.new_page()
    except Exception as exc:
        if is_target_closed_exception(exc):
            if await browser_still_usable(context):
                raise
            raise UserCancelRequested(
                "浏览器窗口已关闭，已保留剩余学习链接，返回主菜单"
            ) from None
        raise


async def _process_url(
    context, url, handler, *, capture_callback=None, failure_file=None
):
    """One fresh page activity. A typed outcome never implies queue deletion."""
    from core.workflow.contracts import TaskOutcome, Status
    from core.workflow.errors import error_outcome
    from core.workflow.host import activity_context
    from core.tasks.identity import parse_entry, account_id
    from core.browser.session import raise_for_certificate_error

    page = None
    monitor = None
    failure_path = failure_file or LEARNING_FAILURES_FILE
    current = activity_context.get()
    task_id = (
        current["task_id"] if current else parse_entry(url, account_id()).key.task_id
    )

    async def capture(stage):
        if capture_callback is not None and page is not None:
            try:
                await capture_callback(page, stage)
            except Exception:
                logging.debug("页面探针捕获失败", exc_info=True)

    try:
        page = await _open_course_page(context)
        if is_course_detail_url(url):
            monitor = _install_course_info_monitor(page)
        await capture("page_created")
        # Business dialogs precede generic overlay cleanup in CourseSession.
        await page.goto(url, wait_until="load")
        await capture("after_navigation")
        await _wait_for_course_bootstrap(page, monitor)
        await ensure_course_page_ready(page)
        completed = await handler(page)
        await capture("after_handler")
        if completed is True:
            from core.queues.learning import (
                RETRIABLE_LEARNING_FAILURE_REASONS,
                ACCESS_DENIED_LEARNING_FAILURE_REASONS,
            )

            # A verified successful course resolves prior automatic learning
            # failures, while manual/discovery/exam issues remain independent.
            for reason in (
                RETRIABLE_LEARNING_FAILURE_REASONS
                | ACCESS_DENIED_LEARNING_FAILURE_REASONS
            ):
                remove_learning_failure(url, reason=reason, file_path=failure_path)
        issues = [e for e in read_learning_failures(failure_path) if e.url == url]
        status = (
            Status.COMPLETED
            if completed is True and not issues
            else Status.WAITING_CHILDREN
            if completed is False or issues
            else Status.WAITING_SYNC
        )
        return TaskOutcome(
            task_id,
            status,
            [e.reason for e in issues],
            evidence_refs=["server_section_progress"] if completed is True else [],
            details={"local_work_complete": completed is True},
        )
    except WafBlockError as exc:
        record_learning_failure(
            url, reason=exc.reason, reason_text=exc.reason_text, file_path=failure_path
        )
        raise
    except (UserCancelRequested, UserAbortRequested):
        raise
    except Exception as exc:
        await capture("error")
        raise_for_certificate_error(exc)
        if is_target_closed_exception(exc):
            if not await browser_still_usable(context):
                raise UserCancelRequested(
                    "浏览器窗口已关闭，当前及剩余任务保留"
                ) from None
            return TaskOutcome(
                task_id,
                Status.INTERRUPTED,
                ["course_page_closed"],
                retry_policy="next_operation",
            )
        if not isinstance(
            exc, (NoPermissionError, LearningFlowError)
        ) and not await browser_still_usable(context):
            raise UserCancelRequested("浏览器窗口已关闭，当前及剩余任务保留") from None
        result = error_outcome(task_id, exc)
        reason = getattr(exc, "reason", "retryable_error")
        record_learning_failure(
            url,
            reason=reason,
            reason_text=getattr(
                exc, "reason_text", f"挂课处理失败，后续可重新加入课程链接: {exc}"
            ),
            detail=getattr(exc, "detail", {}),
            file_path=failure_path,
        )
        return result
    finally:
        if monitor:
            for task in tuple(getattr(monitor, "tasks", ())):
                task.cancel()
            await asyncio.gather(
                *tuple(getattr(monitor, "tasks", ())), return_exceptions=True
            )
        await _close_page_quiet(page)


async def run_afk_once(status_callback=None):
    from core.app.service import ApplicationService

    return await ApplicationService().learn(status_callback=status_callback)
