from __future__ import annotations

import asyncio
import logging
from typing import Callable
from urllib.parse import urlparse

from core.learning.afk_runner import run_afk_once
from core.queues.learning import remember_discovery_entries
from core.browser.session import create_browser_context, is_controller_page
from core.browser.course_links import resolve_course_page_url
from core.config import (
    EXAM_URLS_FILE,
    LEARNING_URLS_FILE,
)
from core.auth.credential import AccountProfile
from core.exam.runner import run_ai_exam_batch
from core.exam.routing import requeue_ai_exam_reviews
from core.queues.exam import append_exam_url, append_exam_urls, read_exam_urls
from core.links import extract_urls_from_text, split_manual_selection_urls
from core.browser.overlays import prepare_page_after_navigation_async
from core.file_ops import (
    is_compliant_url_regex,
    is_exam_url,
    normalize_url,
)
from core.queues.learning import append_learning_urls, read_learning_urls
from core.auth.login import login_and_save_credential
from core.state import collect_project_state
from core.config import summarize_exception_message
from core.discovery.subject_parse import (
    partition_course_and_subject_urls,
)


StatusCallback = Callable[[str], None]
MANUAL_SELECTION_NEW_PAGE_IGNORE_WAIT_MS = 100
MANUAL_SELECTION_POPUP_URL_WAIT_MS = 10000
MANUAL_SELECTION_POPUP_URL_POLL_MS = 500


def parse_manual_selection_input(text: str) -> list[str]:
    return extract_urls_from_text(text)


def _track_background_task(task: asyncio.Task, pending_tasks: set[asyncio.Task]) -> None:
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
        current_url = await resolve_course_page_url(new_page) or normalize_url((new_page.url or "").strip())
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


async def refresh_credential(status_callback: StatusCallback | None = None, *, confirm_same_account=None) -> AccountProfile:
    if status_callback:
        status_callback("正在打开浏览器，请完成登录")
    return await login_and_save_credential(confirm_same_account=confirm_same_account)


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
            # 主控页（含恢复）永不记链、不关闭
            if is_controller_page(context, new_page):
                return

            url = await _wait_for_manual_popup_record_url(new_page)
            if url:
                from core.tasks.relations import record_discovered_tasks
                record_discovered_tasks([url],source='manual_popup')
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
                if not is_controller_page(context, new_page):
                    await new_page.close()
            except Exception:
                pass

    # 主控页由 create_browser_context 保留；在注册 page 监听后再开入口，
    # 避免主控页被当成 popup 误记。
    context.on(
        "page",
        lambda page: _track_background_task(
            asyncio.create_task(handle_new_page(page)),
            popup_tasks,
        ),
    )

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
        await entry_page.goto(entry_url, wait_until="load")
        await prepare_page_after_navigation_async(entry_page)
        await entry_page.wait_for_event("close", timeout=0)

    if popup_tasks:
        await asyncio.gather(*tuple(popup_tasks), return_exceptions=True)

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


async def run_manual_course_selection(input_text, learning_zone_mode='manual', status_callback=None, result_ready_callback=None):
    from core.auth.access import require_task_account
    from core.app.service import ApplicationService
    require_task_account()
    urls = parse_manual_selection_input(input_text)
    learning, exams, zones, classes, entries = split_manual_selection_urls(urls)
    courses, subjects = partition_course_and_subject_urls(learning)
    added_learning = append_learning_urls(courses,file_path=LEARNING_URLS_FILE)
    added_exams = append_exam_urls(exams,file_path=EXAM_URLS_FILE)
    discoveries = subjects + zones + classes + entries
    remember_discovery_entries(discoveries)
    before_learning = len(read_learning_urls(LEARNING_URLS_FILE))
    outcomes = await ApplicationService().batch(discoveries,'discover',options={'zone_mode': learning_zone_mode},status_callback=status_callback) if discoveries else []
    result = {
        'input_url_count': len(urls), 'direct_learning_count': len(added_learning),
        'direct_exam_count': len(added_exams), 'direct_subject_count': len(subjects),
        'learning_zone_url_count': len(zones), 'learning_zone_parsed_count': 0,
        'train_class_url_count': len(classes), 'train_class_parsed_count': 0,
        'entry_url_count': len(entries), 'manual_record_count': 0, 'manual_exam_record_count': 0,
        'learning_total': len(read_learning_urls(LEARNING_URLS_FILE)),
        'exam_total': len(read_exam_urls(EXAM_URLS_FILE)),
        'blocked_count': sum(o['status']=='BLOCKED' for o in outcomes),
    }
    for outcome in outcomes:
        details = outcome.get('details',{})
        result['manual_record_count'] += details.get('manual_record_count',0)
        result['manual_exam_record_count'] += details.get('manual_exam_record_count',0)
    result['learning_zone_parsed_count'] = max(0,result['learning_total']-before_learning)
    if result_ready_callback:
        result_ready_callback(result)
    return result


async def run_afk_workflow(status_callback: StatusCallback | None = None) -> bool:
    if status_callback:
        status_callback("开始挂课")
    await run_afk_once(status_callback=status_callback)
    state = collect_project_state()
    if status_callback:
        status_callback(f"本轮挂课结束：学习待办 {state.learning_count}，失败/复查 {state.learning_failure_count}，AI 考试 {state.exam_count}")
    return state.exam_count > 0

async def run_ai_exam_workflow(
    status_callback: StatusCallback | None = None,
    *,
    auto_submit: bool = False,
) -> int:
    requeue_ai_exam_reviews()
    state = collect_project_state()
    if state.exam_count == 0:
        if status_callback:
            status_callback("未检测到考试链接，本次流程结束")
        return 0

    if status_callback:
        status_callback(f"开始 AI 自动考试，共 {state.exam_count} 条考试链接")
    manual_count = await run_ai_exam_batch(
        status_callback=status_callback,
        auto_submit=auto_submit,
    )
    if status_callback:
        status_callback(f"AI 自动考试结束，待复查 {manual_count} 条")
    return manual_count


async def run_reference_collection_workflow(
    subject_urls: list[str],
    status_callback: StatusCallback | None = None,
) -> dict:
    from core.app.service import ApplicationService
    outcomes = await ApplicationService().references(subject_urls,status_callback=status_callback)
    details = [o.get('details',{}) for o in outcomes]
    from core.config import REFERENCE_OUTPUT_DIR
    return {
        'complete': all(o['status']=='COMPLETED' for o in outcomes),
        'output_dir': str(REFERENCE_OUTPUT_DIR),
        'course_count': sum(d.get('course_count',0) for d in details),
        'section_count': sum(d.get('section_count',0) for d in details),
        'document_count': sum(d.get('document_count',0) for d in details),
        'pdf_count': sum(d.get('pdf_count',0) for d in details),
        'document_failed_count': sum(d.get('document_failed_count',0) for d in details),
        'course_failed_count': sum(d.get('course_failed_count',0) for d in details),
        'video_count': sum(d.get('video_count',0) for d in details),
        'video_with_items': sum(d.get('video_with_items',0) for d in details),
        'downloaded_count': sum(d.get('downloaded_count',0) for d in details),
        'summary_count': sum(d.get('summary_count',0) for d in details),
        'failed_count': sum(d.get('failed_count',0) for d in details),
        'outcomes': outcomes,
    }
