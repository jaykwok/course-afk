from __future__ import annotations

import asyncio
import logging
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.abort import NoPermissionError, UserAbortRequested, UserCancelRequested
from core.browser import (
    create_browser_context,
    ensure_controller_page,
    is_browser_connected,
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
from core.learning_exam import is_subject_url_completed
from core.learning_flows import course_learning, subject_learning
from core.learning_queue import (
    read_learning_failures,
    read_learning_urls,
    record_learning_failure,
    remove_learning_failure,
    write_learning_urls,
)
from core.page_overlays import goto_and_prepare_async


StatusCallback = Callable[[str], None]


@dataclass
class AfkBatch:
    urls: list[str]


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
    /#/study/errors/...「您已打开新的课程详情页…」。控制器页始终保留。
    """
    await ensure_controller_page(context)
    try:
        return await context.new_page()
    except Exception as exc:
        if is_target_closed_exception(exc):
            if is_browser_connected(context):
                raise
            raise UserCancelRequested(
                "浏览器窗口已关闭，已保留剩余学习链接，返回主菜单"
            ) from None
        raise


async def _process_url(context, url: str, handler) -> bool:
    """
    新开标签处理单条学习链接，结束（成功/失败）后关闭该页。

    返回是否需要保留在待学习队列。
    """
    page = None
    try:
        page = await _open_course_page(context)
        await goto_and_prepare_async(page, url)
        await handler(page)
        return False
    except Exception as exc:
        if is_target_closed_exception(exc):
            # 浏览器仍在：仅关标签/页失败 → 保留链接；整窗关闭 → 回主菜单
            if is_browser_connected(context):
                logging.info(f"当前课程标签页已关闭，保留当前学习链接: {url}")
                return True
            raise UserCancelRequested(
                "浏览器窗口已关闭，已保留剩余学习链接，返回主菜单"
            ) from None
        logging.error(f"发生错误: {exc}")
        logging.error(traceback.format_exc())
        if isinstance(exc, NoPermissionError):
            record_learning_failure(
                url,
                reason="no_permission",
                reason_text="无权限访问该学习资源",
                file_path=LEARNING_FAILURES_FILE,
            )
            return False
        record_learning_failure(
            url,
            reason="retryable_error",
            reason_text=f"挂课处理失败，后续可重新加入课程链接: {exc}",
            file_path=LEARNING_FAILURES_FILE,
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
            if is_target_closed_exception(exc) and not is_browser_connected(context):
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
        except Exception as exc:
            if is_target_closed_exception(exc) and not is_browser_connected(context):
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
            # 一门一页 + 处理完 close，避免同页 goto 触发 /study/errors 限流页
            for index, url in enumerate(normalized_urls, start=1):
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
        raise

    logging.info("本轮自动挂课完成")
