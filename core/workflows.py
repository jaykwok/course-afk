from __future__ import annotations

import asyncio
import logging
from typing import Callable
from urllib.parse import urlparse

from playwright.async_api import async_playwright
from core.afk_runner import run_afk_until_complete
from core.browser import (
    apply_async_browser_stealth,
    build_browser_context_options,
    launch_async_browser,
)
from core.config import (
    COOKIES_FILE,
    EXAM_URLS_FILE,
    LEARNING_URLS_FILE,
    MANUAL_EXAM_FILE,
    MYLEARNING_HOME,
    is_ai_configured,
)
from core.credential import AccountProfile
from core.exam_runner import run_ai_exam_batch, run_manual_exam_batch
from core.exam_queue import append_exam_url, append_exam_urls, read_exam_urls
from core.learning_zone import collect_learning_links_from_learning_zone_urls
from core.links import extract_urls_from_text, split_manual_selection_urls
from core.file_ops import (
    is_compliant_url_regex,
    is_exam_url,
    load_cookies,
    normalize_url,
)
from core.learning_queue import append_learning_urls, read_learning_urls
from core.login import login_and_save_credential
from core.state import collect_project_state, read_non_empty_lines
from core.config import summarize_exception_message


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
        current_url = normalize_url((new_page.url or "").strip())
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


def refresh_credential(status_callback: StatusCallback | None = None) -> AccountProfile:
    if status_callback:
        status_callback("正在打开浏览器，请完成登录")
    return login_and_save_credential()


async def collect_learning_links_from_entry_urls(
    entry_urls: list[str],
    status_callback: StatusCallback | None = None,
) -> tuple[int, int, int]:
    if not entry_urls:
        return 0, 0, 0

    cookies = load_cookies(COOKIES_FILE)

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

            url = await _wait_for_manual_popup_record_url(new_page)
            if url:
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
                await new_page.close()
            except Exception:
                pass

    async with async_playwright() as playwright:
        browser = await launch_async_browser(playwright, headless=False)
        context = None
        try:
            context = await browser.new_context(
                **build_browser_context_options(headless=False)
            )
            await apply_async_browser_stealth(context)
            await context.add_cookies(cookies)
            auth_page = await context.new_page()
            await auth_page.goto(MYLEARNING_HOME, wait_until="load")
            await auth_page.close()

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
                        f"正在打开入口链接 {index}/{len(entry_urls)}，处理完成后请关闭当前入口页面继续下一条"
                    )
                if not _is_recordable_manual_popup_url(entry_url):
                    logging.warning(
                        f"入口链接不在官方域名内，仍将打开但请确认来源可信: {entry_url}"
                    )
                entry_page = await context.new_page()
                ignored_pages.add(entry_page)
                await entry_page.goto(entry_url, wait_until="load")
                await entry_page.wait_for_event("close", timeout=0)

            if popup_tasks:
                await asyncio.gather(*tuple(popup_tasks), return_exceptions=True)
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
            try:
                await browser.close()
            except Exception:
                pass

    return (
        len(collected_urls),
        new_learning_popup_count,
        new_exam_popup_count,
    )


async def run_manual_course_selection(
    input_text: str,
    learning_zone_mode: str = "manual",
    status_callback: StatusCallback | None = None,
) -> dict[str, int]:
    urls = parse_manual_selection_input(input_text)
    (
        direct_learning_urls,
        direct_exam_urls,
        learning_zone_urls,
        entry_urls,
    ) = split_manual_selection_urls(urls)

    added_learning = append_learning_urls(
        direct_learning_urls,
        file_path=LEARNING_URLS_FILE,
    )
    if status_callback and added_learning:
        status_callback(f"已直接写入 {len(added_learning)} 条学习链接")

    added_exam_urls = append_exam_urls(
        direct_exam_urls,
        file_path=EXAM_URLS_FILE,
    )
    if status_callback and added_exam_urls:
        status_callback(f"已直接写入 {len(added_exam_urls)} 条考试链接")

    learning_zone_parsed_count = 0
    manual_entry_urls = entry_urls
    if learning_zone_urls:
        if learning_zone_mode == "auto":
            learning_zone_parsed_count = (
                await collect_learning_links_from_learning_zone_urls(
                    learning_zone_urls,
                    status_callback=status_callback,
                )
            )
        else:
            manual_entry_urls = learning_zone_urls + entry_urls

    (
        _,
        manual_record_count,
        manual_exam_record_count,
    ) = await collect_learning_links_from_entry_urls(
        manual_entry_urls, status_callback=status_callback
    )
    return {
        "input_url_count": len(urls),
        "direct_learning_count": len(added_learning),
        "direct_exam_count": len(added_exam_urls),
        "learning_zone_url_count": len(learning_zone_urls),
        "learning_zone_parsed_count": learning_zone_parsed_count,
        "entry_url_count": len(manual_entry_urls),
        "manual_record_count": manual_record_count,
        "manual_exam_record_count": manual_exam_record_count,
        "learning_total": len(read_learning_urls(LEARNING_URLS_FILE)),
        "exam_total": len(read_exam_urls(EXAM_URLS_FILE)),
    }


async def run_afk_workflow(status_callback: StatusCallback | None = None) -> bool:
    if status_callback:
        status_callback("开始挂课")
    await run_afk_until_complete(status_callback=status_callback)
    state = collect_project_state()
    if status_callback:
        if state.exam_count > 0:
            status_callback(f"挂课完成，检测到 {state.exam_count} 条考试链接")
        else:
            status_callback("挂课完成，未检测到考试链接")
    return state.exam_count > 0

async def run_ai_exam_workflow(
    status_callback: StatusCallback | None = None,
    *,
    auto_submit: bool = False,
) -> int:
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
        status_callback(f"AI 自动考试结束，人工处理 {manual_count} 条")
    return manual_count


async def run_manual_exam_workflow(status_callback: StatusCallback | None = None) -> int:
    state = collect_project_state()
    if state.manual_exam_count == 0:
        if status_callback:
            status_callback("未检测到人工考试链接")
        return 0

    if status_callback:
        status_callback(f"开始人工考试，共 {state.manual_exam_count} 条链接")
    processed_count = await run_manual_exam_batch(status_callback=status_callback)
    if status_callback:
        status_callback("人工考试流程完成")
    return processed_count


async def run_reference_collection_workflow(
    subject_urls: list[str],
    status_callback: StatusCallback | None = None,
) -> dict:
    from core.reference_collector import collect_reference_materials

    if status_callback:
        status_callback("开始保存课程课件和视频 AI 导学资料")
    result = await collect_reference_materials(
        subject_urls,
        status_callback=status_callback,
    )
    if status_callback:
        status_callback(f"资料保存完成：{result['output_dir']}")
    return result


async def run_recommended_flow(
    status_callback: StatusCallback | None = None,
    *,
    ask_auto_submit: Callable[[], bool] | None = None,
) -> str:
    state = collect_project_state()
    if not state.has_credential or state.credential_expired:
        if status_callback:
            status_callback("登录凭证不可用，请先更新登录凭证")
        return "credential"

    if state.learning_count == 0:
        if status_callback:
            status_callback("未检测到学习链接，请先手动选择课程或录入链接")
        return "manual-selection"

    has_exam = await run_afk_workflow(status_callback=status_callback)
    if not has_exam:
        if status_callback:
            status_callback("未检测到考试链接，本次流程结束")
        return "afk-only"

    if not is_ai_configured():
        if status_callback:
            status_callback("未填写 AI 配置，跳过 AI 自动考试；可改用人工考试")
        return "ai-not-configured"

    auto_submit = ask_auto_submit() if ask_auto_submit else False
    manual_count = await run_ai_exam_workflow(
        status_callback=status_callback,
        auto_submit=auto_submit,
    )
    if manual_count > 0:
        if status_callback:
            status_callback("AI 自动考试完成，仍有人工考试待处理")
        return "manual-exam-pending"
    return "done"
