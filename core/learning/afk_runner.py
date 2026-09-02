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
    AFK_SLOW_MO,
    LEARNING_FAILURES_FILE,
    LEARNING_URLS_FILE,
)
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
from core.browser.overlays import goto_and_prepare_async


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


def _write_learning_queue(urls: list[str], *, learning_file: Path | None = None) -> None:
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
            raise UserCancelRequested(
                "心跳页已关闭，已保留剩余学习链接，返回主菜单"
            )
        raise UserCancelRequested(
            "浏览器窗口已关闭，已保留剩余学习链接，返回主菜单"
        )
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
    context,
    url: str,
    handler,
    *,
    capture_callback: CaptureCallback | None = None,
    failure_file: Path | None = None,
) -> bool:
    """
    新开标签处理单条学习链接，结束（成功/失败）后关闭该页。

    返回是否需要保留在待学习队列。
    """
    page = None
    course_monitor: _CourseInfoMonitor | None = None
    failure_path = failure_file or LEARNING_FAILURES_FILE

    async def _capture(stage: str) -> None:
        if capture_callback is None or page is None:
            return
        try:
            await capture_callback(page, stage)
        except Exception as exc:
            # 诊断捕获不能改变正式挂课结果。
            logging.debug(f"保存页面探针数据失败 ({stage}): {exc}")

    try:
        page = await _open_course_page(context)
        if is_course_detail_url(url):
            course_monitor = _install_course_info_monitor(page)
        # 探针需要在第一次导航前安装网络/控制台监听器。
        await _capture("page_created")
        await goto_and_prepare_async(page, url)
        await _capture("after_navigation")
        await _wait_for_course_bootstrap(page, course_monitor)
        await handler(page)
        await _capture("after_handler")
        return False
    except (UserCancelRequested, UserAbortRequested):
        # 浏览器整窗关闭等取消信号必须原样上抛；落进下面的通用分支会被
        # 记成「可重试失败」，循环继续下一门——窗口关了却停不下来的根源。
        raise
    except Exception as exc:
        await _capture("error")
        if is_target_closed_exception(exc):
            # 仅关课程标签/窗口（心跳页仍在）→ 保留链接继续；整窗关闭 → 回主菜单。
            # 注意先经 browser_still_usable 落定再判（见其 docstring）。
            if await browser_still_usable(context):
                logging.info(f"当前课程标签页已关闭，保留当前学习链接: {url}")
                return True
            raise UserCancelRequested(
                "浏览器窗口已关闭，已保留剩余学习链接，返回主菜单"
            ) from None
        if isinstance(exc, NoPermissionError):
            # 无权限 / 资源不存在 / 下架：移出课程链接，失败文档写明原因（不自动重试）
            reason = getattr(exc, "reason", None) or "no_permission"
            reason_text = (
                getattr(exc, "reason_text", None)
                or str(exc)
                or "无权限访问该学习资源，已从课程链接清理"
            )
            logging.warning(f"不可访问资源: {url} [{reason}] {reason_text}")
            record_learning_failure(
                url,
                reason=reason,
                reason_text=reason_text,
                file_path=failure_path,
            )
            logging.info(
                f"不可访问资源已清理并记入失败链接: {url} [{reason}] {reason_text}"
            )
            return False
        if isinstance(exc, LearningFlowError):
            logging.warning(f"挂课流程失败: {url} [{exc.reason}] {exc.reason_text}")
            logging.debug(traceback.format_exc())
            record_learning_failure(
                url,
                reason=exc.reason,
                reason_text=exc.reason_text,
                detail=getattr(exc, "detail", None) or {},
                file_path=failure_path,
            )
            if isinstance(exc, WafBlockError):
                raise
            return bool(exc.keep_pending)
        # Edge 整窗关闭后 Playwright 偶尔从内部连接层抛出 AttributeError
        # （例如 "'dict' object has no attribute '_object'"），不属于标准的
        # TargetClosedError。未知异常落盘前再做一次组合存活判定：整窗已关就
        # 按用户取消处理，避免误记失败、输出大段堆栈并短暂进入下一条链接。
        if not await browser_still_usable(context):
            raise UserCancelRequested(
                "浏览器窗口已关闭，已保留剩余学习链接，返回主菜单"
            ) from None
        logging.error(f"发生错误: {exc}")
        logging.error(traceback.format_exc())
        record_learning_failure(
            url,
            reason="retryable_error",
            reason_text=f"挂课处理失败，后续可重新加入课程链接: {exc}",
            file_path=failure_path,
        )
        return True
    finally:
        await _close_page_quiet(page)


async def _recheck_url_type_links(context) -> None:
    """复查 url_type_pending：每条独立开页再关，避免同页互斥。"""
    url_type_links = [
        entry
        for entry in read_learning_failures(file_path=LEARNING_FAILURES_FILE)
        if entry.reason == "url_type_pending"
    ]
    if not url_type_links:
        return

    for entry in url_type_links:
        url = entry.url
        page = None
        try:
            page = await _open_course_page(context)
        except UserCancelRequested:
            raise
        except Exception as exc:
            if is_target_closed_exception(exc) and not await browser_still_usable(
                context
            ):
                raise UserCancelRequested(
                    "浏览器窗口已关闭，已保留剩余学习链接，返回主菜单"
                ) from None
            logging.error(f"复查 URL 类型链接无法打开页面: {exc}")
            continue

        try:
            await goto_and_prepare_async(page, url)
            if await is_subject_url_completed(page):
                logging.info(f"URL类型链接学习完成: {url}")
                remove_learning_failure(
                    url,
                    file_path=LEARNING_FAILURES_FILE,
                    keep_file=True,
                )
            else:
                logging.info(f"URL类型链接学习未完成: {url}")
                record_learning_failure(
                    url,
                    reason="url_type_pending",
                    reason_text="URL 类型学习未确认完成，等待后续复查",
                    detail=entry.detail,
                    file_path=LEARNING_FAILURES_FILE,
                )
        except (UserCancelRequested, UserAbortRequested):
            raise
        except Exception as exc:
            if is_target_closed_exception(exc) and not await browser_still_usable(
                context
            ):
                raise UserCancelRequested(
                    "浏览器窗口已关闭，已保留剩余学习链接，返回主菜单"
                ) from None
            logging.error(f"复查 URL 类型链接失败: {exc}")
            logging.error(traceback.format_exc())
            record_learning_failure(
                url,
                reason="url_type_pending",
                reason_text=f"URL 类型学习复查失败: {exc}",
                detail=entry.detail,
                file_path=LEARNING_FAILURES_FILE,
            )
        finally:
            await _close_page_quiet(page)


async def run_afk_once(status_callback: StatusCallback | None = None) -> None:
    batch = prepare_afk_batch()
    if not batch.urls:
        if status_callback:
            status_callback("未检测到可处理的学习链接")
        return

    # prepare 已去重；此处再 normalize 一次以防外部注入 batch
    normalized_urls = normalize_urls(batch.urls)
    pending_learning_urls = list(normalized_urls)
    _write_learning_queue(pending_learning_urls)

    try:
        async with create_browser_context(slow_mo=AFK_SLOW_MO) as (_, context):
            # 心跳页（mylearning 主控页）关闭通知：只提示、不打断——
            # 「当前课程挂完后停止」是用户确认的设计语义（确定性：课程主路径
            # 不查连接状态，停止点固定在下一门开课前的 browser_still_usable）。
            heartbeat_announced = False
            watched_controllers: set[int] = set()

            def _announce_heartbeat_closed() -> None:
                nonlocal heartbeat_announced
                if heartbeat_announced:
                    return
                heartbeat_announced = True
                logging.info("心跳页已关闭：当前课程完成后将停止本轮挂课")
                if status_callback:
                    status_callback("心跳页已关闭，当前课程完成后将停止本轮挂课")

            def _watch_heartbeat() -> None:
                controller = get_controller_page(context)
                if controller is None or id(controller) in watched_controllers:
                    return
                watched_controllers.add(id(controller))
                on = getattr(controller, "on", None)
                if callable(on):
                    on("close", _announce_heartbeat_closed)

            # 一门一页 + 处理完 close，避免同页 goto 触发 /study/errors 限流页
            for index, url in enumerate(normalized_urls, start=1):
                _watch_heartbeat()  # 控制器页极少被重建，重建后补挂通知
                if status_callback:
                    status_callback(
                        f"挂课 {index}/{len(normalized_urls)}: {url}"
                    )
                logging.info(
                    f"({index}/{len(normalized_urls)})当前学习链接为: {url}"
                )

                if not is_compliant_url_regex(url):
                    logging.info("不合规链接，已记录到挂课失败链接")
                    record_learning_failure(
                        url,
                        reason="non_compliant_url",
                        reason_text="学习链接不符合课程或主题链接格式",
                        file_path=LEARNING_FAILURES_FILE,
                    )
                    if url in pending_learning_urls:
                        pending_learning_urls.remove(url)
                        _write_learning_queue(pending_learning_urls)
                    continue

                if is_subject_detail_url(url) or "/study/subject/detail/" in url:
                    handler = subject_learning
                elif is_course_detail_url(url) or "/study/course/detail/" in url:
                    handler = course_learning
                else:
                    logging.info(f"无法识别的学习链接类型: {url}")
                    record_learning_failure(
                        url,
                        reason="unknown_learning_type",
                        reason_text="无法识别该学习链接类型",
                        file_path=LEARNING_FAILURES_FILE,
                    )
                    if url in pending_learning_urls:
                        pending_learning_urls.remove(url)
                        _write_learning_queue(pending_learning_urls)
                    continue

                keep_pending = await _process_url(context, url, handler)

                if not keep_pending and url in pending_learning_urls:
                    pending_learning_urls.remove(url)
                    _write_learning_queue(pending_learning_urls)

            await _recheck_url_type_links(context)
            _write_learning_queue(pending_learning_urls)
    except BaseException as exc:
        if isinstance(exc, (SystemExit, GeneratorExit)):
            raise
        if isinstance(exc, asyncio.CancelledError):
            _write_learning_queue(pending_learning_urls)
            logging.debug("挂课流程被取消，已保存剩余学习链接，返回主菜单")
            raise UserCancelRequested(
                "已中断挂课，已保存剩余学习链接，返回主菜单"
            ) from None
        if isinstance(exc, KeyboardInterrupt):
            _write_learning_queue(pending_learning_urls)
            logging.debug("收到 Ctrl+C，已保存当前和剩余学习链接，程序退出")
            raise UserAbortRequested(
                "已收到 Ctrl+C，已保存当前和剩余学习链接，程序退出"
            ) from None
        if is_target_closed_exception(exc):
            _write_learning_queue(pending_learning_urls)
            logging.debug("浏览器窗口已关闭，已保存剩余学习链接，返回主菜单")
            raise UserCancelRequested(
                "浏览器窗口已关闭，已保留剩余学习链接，返回主菜单"
            ) from None
        if isinstance(exc, UserCancelRequested):
            _write_learning_queue(pending_learning_urls)
            logging.debug("挂课流程被取消，已保存剩余学习链接，返回主菜单")
            raise
        if isinstance(exc, UserAbortRequested):
            save_pending_urls = getattr(exc, "save_pending_urls", True)
            message = str(exc) or "已保存当前和剩余学习链接，程序退出"
            if save_pending_urls:
                _write_learning_queue(pending_learning_urls)
            logging.debug(f"用户主动终止挂课流程: {message}")
            raise UserAbortRequested(
                message,
                save_pending_urls=save_pending_urls,
            ) from None
        if isinstance(exc, WafBlockError):
            _write_learning_queue(pending_learning_urls)
            message = exc.reason_text or str(exc)
            logging.warning(f"{message}，本轮挂课已停止")
            if status_callback:
                status_callback(f"{message}，本轮挂课已停止")
            return
        raise

    logging.info("本轮自动挂课完成")
