from __future__ import annotations

import asyncio
import logging
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.abort import UserAbortRequested
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
    normalize_url,
)
from core.learning import course_learning, is_subject_url_completed, subject_learning
from core.learning_queue import (
    read_learning_failures,
    read_learning_urls,
    record_learning_failure,
    remove_learning_failure,
    write_learning_urls,
)


StatusCallback = Callable[[str], None]


@dataclass
class AfkBatch:
    urls: list[str]
    is_retry: bool


def _write_learning_queue(urls: list[str], *, learning_file: Path | None = None) -> None:
    if learning_file is None:
        learning_file = LEARNING_URLS_FILE
    if urls or learning_file.exists():
        write_learning_urls(urls, file_path=learning_file)


def _is_user_abort_exception(exc: BaseException) -> bool:
    return isinstance(exc, (UserAbortRequested, KeyboardInterrupt, asyncio.CancelledError))


def prepare_afk_batch(
    *,
    learning_file: Path | None = None,
) -> AfkBatch:
    if learning_file is None:
        learning_file = LEARNING_URLS_FILE
    learning_urls = read_learning_urls(file_path=learning_file)
    _write_learning_queue(learning_urls, learning_file=learning_file)
    return AfkBatch(urls=learning_urls, is_retry=False)


async def _process_url(context, url: str, handler) -> bool:
    """处理单条学习链接，返回是否需要保留在待学习队列。"""

    await ensure_controller_page(context)
    page = await context.new_page()
    try:
        await page.goto(url)
        await handler(page)
        return False
    except Exception as exc:
        if is_target_closed_exception(exc):
            if is_browser_connected(context):
                logging.info(f"当前课程标签页已关闭，保留当前学习链接: {url}")
                return True
            raise UserAbortRequested(
                "已关闭浏览器窗口，程序退出",
                save_pending_urls=False,
            ) from None
        logging.error(f"发生错误: {exc}")
        logging.error(traceback.format_exc())
        if str(exc) == "无权限查看该资源":
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
        try:
            await page.close()
        except Exception:
            pass


async def _recheck_url_type_links(context) -> None:
    url_type_links = [
        entry
        for entry in read_learning_failures(file_path=LEARNING_FAILURES_FILE)
        if entry.reason == "url_type_pending"
    ]
    if not url_type_links:
        return

    for entry in url_type_links:
        url = entry.url
        await ensure_controller_page(context)
        page = await context.new_page()
        try:
            await page.goto(url)
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
            try:
                await page.close()
            except Exception:
                pass


async def run_afk_once(status_callback: StatusCallback | None = None) -> bool:
    batch = prepare_afk_batch()
    if not batch.urls:
        if status_callback:
            status_callback("未检测到可处理的学习链接")
        return False

    normalized_urls = list(dict.fromkeys(normalize_url(raw_url.strip()) for raw_url in batch.urls))
    pending_learning_urls = list(normalized_urls)
    _write_learning_queue(pending_learning_urls)

    try:
        async with create_browser_context(slow_mo=AFK_SLOW_MO) as (_, context):
            for index, url in enumerate(normalized_urls, start=1):
                if status_callback:
                    status_callback(f"挂课 {index}/{len(normalized_urls)}: {url}")
                logging.info(f"({index}/{len(normalized_urls)})当前学习链接为: {url}")

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

                if "subject" in url:
                    keep_pending = await _process_url(context, url, subject_learning)
                elif "course" in url:
                    keep_pending = await _process_url(context, url, course_learning)
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

                if not keep_pending and url in pending_learning_urls:
                    pending_learning_urls.remove(url)
                    _write_learning_queue(pending_learning_urls)

            await _recheck_url_type_links(context)
            _write_learning_queue(pending_learning_urls)
    except BaseException as exc:
        if _is_user_abort_exception(exc):
            if isinstance(exc, KeyboardInterrupt):
                save_pending_urls = True
                message = "已收到 Ctrl+C，已保存当前和剩余学习链接，程序退出"
            else:
                save_pending_urls = getattr(exc, "save_pending_urls", True)
                message = str(exc) or (
                    "已保存当前和剩余学习链接，程序退出"
                    if save_pending_urls
                    else "已关闭浏览器窗口，程序退出"
                )
            if save_pending_urls:
                _write_learning_queue(pending_learning_urls)
            logging.debug(f"用户主动终止挂课流程: {message}")
            raise UserAbortRequested(
                message,
                save_pending_urls=save_pending_urls,
            ) from None
        raise

    logging.info("本轮自动挂课完成")
    return False


async def run_afk_until_complete(status_callback: StatusCallback | None = None) -> None:
    await run_afk_once(status_callback=status_callback)
