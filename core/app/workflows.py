from __future__ import annotations

import asyncio
import logging
from typing import Callable
from urllib.parse import urlparse

from core.learning.afk_runner import run_afk_once
from core.browser.session import create_browser_context, is_controller_page
from core.config import (
    EXAM_URLS_FILE,
    LEARNING_URLS_FILE,
    is_ai_configured,
)
from core.auth.credential import AccountProfile
from core.exam.runner import run_ai_exam_batch, run_manual_exam_batch
from core.queues.exam import append_exam_url, append_exam_urls, read_exam_urls
from core.learning.zone import collect_learning_links_from_learning_zone_urls
from core.links import extract_urls_from_text, split_manual_selection_urls
from core.browser.overlays import prepare_page_after_navigation_async
from core.file_ops import (
    is_compliant_url_regex,
    is_exam_url,
    normalize_url,
)
from core.discovery.train_class import collect_learning_links_from_train_class_urls
from core.queues.learning import append_learning_urls, read_learning_urls
from core.auth.login import login_and_save_credential
from core.state import collect_project_state
from core.config import summarize_exception_message
from core.discovery.subject_parse import (
    expand_and_append_subject_urls,
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


async def run_manual_course_selection(
    input_text: str,
    learning_zone_mode: str = "manual",
    status_callback: StatusCallback | None = None,
    result_ready_callback: Callable[[dict[str, int]], None] | None = None,
) -> dict[str, int]:
    urls = parse_manual_selection_input(input_text)
    (
        direct_learning_urls,
        direct_exam_urls,
        learning_zone_urls,
        train_class_urls,
        entry_urls,
    ) = split_manual_selection_urls(urls)

    # 直接粘贴的 subject 不入队原链，按方案 C 展开：课→学习队列，考→考试队列，未知→残留
    direct_course_urls, direct_subject_urls = partition_course_and_subject_urls(
        direct_learning_urls
    )
    added_learning = append_learning_urls(
        direct_course_urls,
        file_path=LEARNING_URLS_FILE,
    )
    if status_callback and added_learning:
        status_callback(f"已直接写入 {len(added_learning)} 条课程链接")

    added_exam_urls = append_exam_urls(
        direct_exam_urls,
        file_path=EXAM_URLS_FILE,
    )
    if status_callback and added_exam_urls:
        status_callback(f"已直接写入 {len(added_exam_urls)} 条考试链接")

    subject_expand_learning_added = 0
    subject_expand_exam_added = 0
    result_notified = False

    def build_result(
        learning_zone_parsed_count: int,
        train_class_parsed_count: int,
        manual_record_count: int,
        manual_exam_record_count: int,
        manual_entry_urls: list[str],
    ) -> dict[str, int]:
        return {
            "input_url_count": len(urls),
            "direct_learning_count": len(added_learning) + subject_expand_learning_added,
            "direct_exam_count": len(added_exam_urls) + subject_expand_exam_added,
            "direct_subject_count": len(direct_subject_urls),
            "learning_zone_url_count": len(learning_zone_urls),
            "learning_zone_parsed_count": learning_zone_parsed_count,
            "train_class_url_count": len(train_class_urls),
            "train_class_parsed_count": train_class_parsed_count,
            "entry_url_count": len(manual_entry_urls),
            "manual_record_count": manual_record_count,
            "manual_exam_record_count": manual_exam_record_count,
            "learning_total": len(read_learning_urls(LEARNING_URLS_FILE)),
            "exam_total": len(read_exam_urls(EXAM_URLS_FILE)),
        }

    def notify_result_ready(result: dict[str, int]) -> None:
        nonlocal result_notified
        if result_notified or result_ready_callback is None:
            return
        result_notified = True
        result_ready_callback(result)

    learning_zone_parsed_count = 0
    train_class_parsed_count = 0
    # 学习专区 manual 模式并入入口，稍后与 entry 一并打开
    auto_zone_urls = (
        learning_zone_urls if learning_zone_mode == "auto" else []
    )
    manual_entry_urls = (
        learning_zone_urls + entry_urls
        if learning_zone_urls and learning_zone_mode != "auto"
        else list(entry_urls)
    )

    # Phase A 自动批 + Phase B 入口：同一浏览器会话，串行，只冷启动一次
    needs_auto_browser = bool(
        direct_subject_urls or auto_zone_urls or train_class_urls
    )
    needs_browser = needs_auto_browser or bool(manual_entry_urls)

    async def _run_auto_browser_jobs(shared_context) -> None:
        """Phase A：主题/专区/培训班。无 page 监听，各 job 自管 new_page/close。"""
        nonlocal subject_expand_learning_added, subject_expand_exam_added
        nonlocal learning_zone_parsed_count, train_class_parsed_count

        share_kwargs: dict = {"context": shared_context}

        if direct_subject_urls:
            if status_callback:
                status_callback(
                    f"正在展开 {len(direct_subject_urls)} 个主题链接（课→学习队列，"
                    "考→考试队列，未知类型保留主题）"
                )
            subject_stats = await expand_and_append_subject_urls(
                direct_subject_urls,
                status_callback=status_callback,
                **share_kwargs,
            )
            subject_expand_learning_added = subject_stats["learning_added"]
            subject_expand_exam_added = subject_stats["exam_added"]

        if auto_zone_urls:
            zone_before_close = None
            if not train_class_urls and not manual_entry_urls:
                zone_before_close = lambda parsed_count: notify_result_ready(
                    build_result(parsed_count, 0, 0, 0, manual_entry_urls)
                )
            zone_kwargs = {"status_callback": status_callback, **share_kwargs}
            if zone_before_close is not None:
                zone_kwargs["before_close_callback"] = zone_before_close
            learning_zone_parsed_count = (
                await collect_learning_links_from_learning_zone_urls(
                    auto_zone_urls,
                    **zone_kwargs,
                )
            )

        if train_class_urls:
            class_before_close = None
            if not manual_entry_urls:
                class_before_close = lambda parsed_count: notify_result_ready(
                    build_result(
                        learning_zone_parsed_count,
                        parsed_count,
                        0,
                        0,
                        manual_entry_urls,
                    )
                )
            class_kwargs = {"status_callback": status_callback, **share_kwargs}
            if class_before_close is not None:
                class_kwargs["before_close_callback"] = class_before_close
            train_class_parsed_count = (
                await collect_learning_links_from_train_class_urls(
                    train_class_urls,
                    **class_kwargs,
                )
            )

        # 无入口时，在关浏览器前尽量先回传结果
        if (
            not manual_entry_urls
            and result_ready_callback is not None
            and not result_notified
        ):
            notify_result_ready(
                build_result(
                    learning_zone_parsed_count,
                    train_class_parsed_count,
                    0,
                    0,
                    manual_entry_urls,
                )
            )

    def entry_before_close(counts: tuple[int, int, int]) -> None:
        _, learning_count, exam_count = counts
        notify_result_ready(
            build_result(
                learning_zone_parsed_count,
                train_class_parsed_count,
                learning_count,
                exam_count,
                manual_entry_urls,
            )
        )

    manual_record_count = 0
    manual_exam_record_count = 0

    if needs_browser:
        auto_parts = sum(
            [
                1 if direct_subject_urls else 0,
                1 if auto_zone_urls else 0,
                1 if train_class_urls else 0,
            ]
        )
        if status_callback:
            if auto_parts >= 2 and manual_entry_urls:
                status_callback(
                    "将在同一浏览器会话中完成主题/专区/培训班解析，随后处理入口页"
                )
            elif auto_parts >= 2:
                status_callback("将在同一浏览器会话中完成主题/专区/培训班解析")
            elif needs_auto_browser and manual_entry_urls:
                status_callback(
                    "将在同一浏览器会话中完成自动解析，随后处理入口页"
                )

        # 有入口必须有头；仅自动批保持默认（与 create_browser_context 一致）
        browser_kwargs: dict = {}
        if manual_entry_urls:
            browser_kwargs["headless"] = False

        async with create_browser_context(**browser_kwargs) as (_, shared_context):
            if needs_auto_browser:
                await _run_auto_browser_jobs(shared_context)

            if manual_entry_urls:
                if status_callback and needs_auto_browser:
                    status_callback(
                        "自动解析已完成，请在同一浏览器中处理入口页"
                        "（同时只打开一条，关闭后继续下一条）"
                    )
                entry_kwargs: dict = {
                    "status_callback": status_callback,
                    "context": shared_context,
                }
                if result_ready_callback is not None:
                    entry_kwargs["before_close_callback"] = entry_before_close
                (
                    _,
                    manual_record_count,
                    manual_exam_record_count,
                ) = await collect_learning_links_from_entry_urls(
                    manual_entry_urls,
                    **entry_kwargs,
                )
    result = build_result(
        learning_zone_parsed_count,
        train_class_parsed_count,
        manual_record_count,
        manual_exam_record_count,
        manual_entry_urls,
    )
    notify_result_ready(result)
    return result


async def run_afk_workflow(status_callback: StatusCallback | None = None) -> bool:
    if status_callback:
        status_callback("开始挂课")
    await run_afk_once(status_callback=status_callback)
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
    from core.discovery.reference_collector import collect_reference_materials

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

    if state.learning_count > 0:
        has_exam = await run_afk_workflow(status_callback=status_callback)
    else:
        has_exam = state.exam_count > 0

    if not has_exam:
        if state.manual_exam_count > 0:
            if status_callback:
                status_callback("当前仅有人工考试待处理")
            return "manual-exam-pending"
        if state.learning_count == 0:
            if status_callback:
                status_callback("未检测到学习或考试链接，请先手动录入")
            return "manual-selection"
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
