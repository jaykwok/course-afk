from __future__ import annotations

from core.browser.bootstrap import navigate_platform_page
import asyncio
import logging
from typing import Callable
from urllib.parse import urlparse
from core.browser.session import create_browser_context
from core.browser.bootstrap import is_session_internal_page
from core.browser.course_links import resolve_course_page_url
from core.browser.overlays import prepare_page_after_navigation_async
from core.config import EXAM_URLS_FILE, LEARNING_URLS_FILE, summarize_exception_message
from core.file_ops import is_compliant_url_regex, is_exam_url, normalize_url
from core.queues.exam import append_exam_url, read_exam_urls
from core.queues.learning import append_learning_urls, read_learning_urls

StatusCallback = Callable[[str], None]
MANUAL_SELECTION_NEW_PAGE_IGNORE_WAIT_MS = 100
MANUAL_SELECTION_POPUP_URL_WAIT_MS = 10000
MANUAL_SELECTION_POPUP_URL_POLL_MS = 500


def _track_background_task(
    task: asyncio.Task, pending_tasks: set[asyncio.Task]
) -> None:
    pending_tasks.add(task)

    def _cleanup(completed_task: asyncio.Task) -> None:
        pending_tasks.discard(completed_task)
        try:
            completed_task.exception()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logging.debug(f"后台任务结束时读取异常失败: {exc}")

    task.add_done_callback(_cleanup)


def _format_status_error_message(action: str, exc: Exception) -> str:
    return summarize_exception_message(exc, action)


def _is_recordable_manual_popup_url(url: str) -> bool:
    stripped_url = (url or "").strip()
    if not stripped_url or stripped_url == "about:blank":
        return False

    parsed_url = urlparse(stripped_url)
    if parsed_url.scheme not in {"http", "https"}:
        return False

    hostname = (parsed_url.hostname or "").lower()
    return (
        hostname == "kc.zhixueyun.com"
        or hostname.endswith(".zhixueyun.com")
        or hostname == "www.mylearning.cn"
        or hostname.endswith(".mylearning.cn")
    )


async def _wait_for_manual_popup_record_url(new_page) -> str | None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + MANUAL_SELECTION_POPUP_URL_WAIT_MS / 1000
    fallback_url: str | None = None

    while True:
        current_url = await resolve_course_page_url(new_page) or normalize_url(
            (new_page.url or "").strip()
        )
        if is_compliant_url_regex(current_url) or is_exam_url(current_url):
            return current_url
        if _is_recordable_manual_popup_url(current_url):
            fallback_url = current_url

        if loop.time() >= deadline:
            return fallback_url

        try:
            await new_page.wait_for_timeout(MANUAL_SELECTION_POPUP_URL_POLL_MS)
        except Exception:
            return fallback_url


async def _collect_learning_links_on_entry_context(
    context,
    entry_urls: list[str],
    status_callback: StatusCallback | None = None,
    before_close_callback: Callable[[tuple[int, int, int]], None] | None = None,
) -> tuple[int, int, int]:
    """
    在已有 context 上串行打开入口页并记录 popup（Phase B）。

    不关闭 context。入口页串行 wait close：同时只处理一个入口，避免多页并行。
    page 监听仅在此阶段注册，避免与自动批 new_page 冲突。
    """
    if not entry_urls:
        return 0, 0, 0

    collected_urls = set(read_learning_urls(LEARNING_URLS_FILE))
    collected_exam_urls = set(read_exam_urls(EXAM_URLS_FILE))
    new_learning_popup_count = 0
    new_exam_popup_count = 0
    popup_tasks: set[asyncio.Task] = set()
    ignored_pages: set[object] = set()

    async def handle_new_page(new_page):
        nonlocal new_learning_popup_count, new_exam_popup_count
        try:
            await new_page.wait_for_timeout(MANUAL_SELECTION_NEW_PAGE_IGNORE_WAIT_MS)
            if new_page in ignored_pages:
                return
            # 认证临时页与空白占位页永不记链、不关闭
            if is_session_internal_page(context, new_page):
                return

            url = await _wait_for_manual_popup_record_url(new_page)
            if is_session_internal_page(context, new_page):
                return
            if url:
                from core.tasks.relations import record_discovered_tasks

                record_discovered_tasks([url], source="manual_popup")
                if is_exam_url(url):
                    normalized_exam_url = normalize_url(url)
                    append_exam_url(normalized_exam_url, file_path=EXAM_URLS_FILE)
                    if normalized_exam_url not in collected_exam_urls:
                        collected_exam_urls.add(normalized_exam_url)
                        new_exam_popup_count += 1
                        if status_callback:
                            status_callback(f"已记录考试链接: {normalized_exam_url}")
                else:
                    added = append_learning_urls([url], file_path=LEARNING_URLS_FILE)
                    if added:
                        collected_urls.update(added)
                        new_learning_popup_count += len(added)
                        if status_callback:
                            if is_compliant_url_regex(added[0]):
                                status_callback(f"已记录学习链接: {added[0]}")
                            else:
                                status_callback(f"已记录新页面链接: {added[0]}")
            await new_page.close()
        except Exception as exc:
            if status_callback:
                status_callback(_format_status_error_message("记录新页面链接失败", exc))
            try:
                if not is_session_internal_page(context, new_page):
                    await new_page.close()
            except Exception:
                pass

    # The shared context outlives this activity: detach listeners and drain
    # popup work before another activity may lease its tabs.
    def page_created(page):
        _track_background_task(asyncio.create_task(handle_new_page(page)), popup_tasks)

    context.on("page", page_created)
    try:
        for index, entry_url in enumerate(entry_urls, start=1):
            if status_callback:
                status_callback(
                    f"正在打开入口链接 {index}/{len(entry_urls)}，"
                    "处理完成后请关闭当前入口页面继续下一条"
                )
            if not _is_recordable_manual_popup_url(entry_url):
                logging.warning(
                    f"入口链接不在官方域名内，仍将打开但请确认来源可信: {entry_url}"
                )
            entry_page = await context.new_page()
            ignored_pages.add(entry_page)
            # Generic portals may redirect to catalogs; known course/exam/subject
            # inputs still pass the navigator's resource and source-identity gate.
            await navigate_platform_page(entry_page, entry_url, wait_until="load")
            await prepare_page_after_navigation_async(entry_page)
            await entry_page.wait_for_event("close", timeout=0)

        if popup_tasks:
            await asyncio.gather(*tuple(popup_tasks), return_exceptions=True)

    finally:
        remove_listener = getattr(context, "remove_listener", None)
        if callable(remove_listener):
            remove_listener("page", page_created)
        for task in tuple(popup_tasks):
            if not task.done():
                task.cancel()
        if popup_tasks:
            from core.runtime import close_safely
            await close_safely(asyncio.gather(*tuple(popup_tasks), return_exceptions=True),
                               label="manual popup tasks")

    result = (
        len(collected_urls),
        new_learning_popup_count,
        new_exam_popup_count,
    )
    if before_close_callback:
        before_close_callback(result)
    return result


async def collect_learning_links_from_entry_urls(
    entry_urls: list[str],
    status_callback: StatusCallback | None = None,
    before_close_callback: Callable[[tuple[int, int, int]], None] | None = None,
    *,
    context=None,
) -> tuple[int, int, int]:
    """
    手动入口页：记录 popup 学习/考试链接。

    可传入已有 context 复用浏览器（Phase B）；否则自建 headless=False 会话。
    入口页始终串行打开，同时只处理一条。
    """
    if not entry_urls:
        return 0, 0, 0

    if context is not None:
        return await _collect_learning_links_on_entry_context(
            context,
            entry_urls,
            status_callback=status_callback,
            before_close_callback=before_close_callback,
        )

    async with create_browser_context(headless=False) as (_, new_context):
        return await _collect_learning_links_on_entry_context(
            new_context,
            entry_urls,
            status_callback=status_callback,
            before_close_callback=before_close_callback,
        )
