from __future__ import annotations

import asyncio
import logging
import traceback
from typing import Callable

from openai import OpenAI
from playwright.async_api import Locator
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from core.abort import UserAbortRequested, UserCancelRequested
from core.browser.session import (
    create_browser_context,
    get_page_context,
    is_browser_connected,
    is_target_closed_exception,
)
from core.config import (
    AI_ENABLE_THINKING,
    AI_ENABLE_WEB_SEARCH,
    AI_REASONING_EFFORT,
    AI_REQUEST_TIMEOUT,
    AI_REQUEST_TYPE,
    COURSE_EXAM_ATTEMPT_THRESHOLD,
    EXAM_URLS_FILE,
    MANUAL_EXAM_FILE,
    MODEL_NAME,
    OPENAI_COMPLETION_API_KEY,
    OPENAI_COMPLETION_BASE_URL,
    PAPER_EXAM_ATTEMPT_THRESHOLD,
    ZHIXUEYUN_COURSE_PREFIX,
    ZHIXUEYUN_EXAM_PREFIX,
    ZHIXUEYUN_SUBJECT_PREFIX,
    validate_ai_base_url,
)
from core.exam.flow import ExamQuestionExtractionError, ai_exam, wait_for_finish_test
from core.exam.answers import ExamAiConfigurationError
from core.exam.rules import (
    extract_attempt_limit_message as _extract_attempt_limit_message,
    parse_remaining_attempts,
)
from core.exam.routing import queue_exam_url_by_attempt_text
from core.file_ops import normalize_url
from core.queues.exam import (
    has_ai_failed_model_config,
    read_exam_urls,
    record_ai_failed_model_config,
    write_exam_urls,
)
from core.learning.exam_bridge import check_exam_passed
from core.learning.popups import handle_rating_popup
from core.queues.manual_exam import (
    ManualExamEntry,
    append_manual_exam_entry,
    read_manual_exam_queue,
    write_manual_exam_queue,
)
StatusCallback = Callable[[str], None]

COURSE_EXAM_BUTTON = ".btn.new-radius"
PAPER_EXAM_BUTTONS = [
    ".banner-handler-btn.themeColor-border-color.themeColor-background-color",
    "button:has-text('开始考试')",
    "button:has-text('继续考试')",
    "button:has-text('去考试')",
    "a:has-text('开始考试')",
    "a:has-text('继续考试')",
    "a:has-text('去考试')",
    ".btn.new-radius",
]

LOGIN_REDIRECT_TIMEOUT_MS = 0
LOGIN_REDIRECT_POLL_MS = 500


def classify_exam_entry_url(url: str) -> str:
    """按标准 hash 路由区分考试入口，避免普通查询参数误命中。"""
    normalized = normalize_url(url)
    if normalized.startswith(ZHIXUEYUN_EXAM_PREFIX):
        return "exam"
    if normalized.startswith(ZHIXUEYUN_COURSE_PREFIX):
        return "course"
    if normalized.startswith(ZHIXUEYUN_SUBJECT_PREFIX):
        return "subject"
    return "unknown"


async def _locate_exam_button(page) -> Locator | None:
    """按入口语义定位开考按钮，兼容不同试卷页布局。"""
    for selector in PAPER_EXAM_BUTTONS:
        try:
            button = page.locator(selector)
            count = await button.count()
            if count > 0:
                for i in range(min(count, 5)):
                    candidate = button.nth(i)
                    if await candidate.is_visible():
                        return candidate
        except Exception:
            continue
    return None


async def _has_authorization_cookie(page) -> bool:
    context = get_page_context(page)
    if context is None:
        return False
    try:
        cookies = await context.cookies()
    except Exception:
        return False
    return any(
        str(cookie.get("name", "")).strip().lower() == "authorization"
        for cookie in cookies
    )


async def _has_ready_answer_question(page) -> bool:
    """确认至少一道题的题型和题干均已挂载，排除空壳/登录页。"""
    try:
        items = page.locator(".question-type-item")
        count = await items.count()
        for index in range(min(count, 3)):
            item = items.nth(index)
            score = item.locator(".o-score")
            if await score.count() <= 0 or not (await score.last.inner_text()).strip():
                continue
            for selector in (".stem-content-main", ".single-title .rich-text-style"):
                question = item.locator(selector)
                if await question.count() > 0 and (await question.first.inner_text()).strip():
                    return True

        # 单题模式部分版本没有 question-type-item 外壳，使用解析器同款全局选择器。
        score = page.locator(".o-score")
        question = page.locator(".single-title .rich-text-style")
        if (
            await score.count() > 0
            and (await score.last.inner_text()).strip()
            and await question.count() > 0
            and (await question.first.inner_text()).strip()
        ):
            return True
    except Exception:
        return False
    return False


async def _is_paper_entry_ready(page) -> bool:
    if await _has_ready_answer_question(page):
        return True
    if await _locate_exam_button(page) is not None:
        return True
    return await _get_paper_attempt_limit_message(page) is not None


async def _wait_for_target_route_after_auth(
    page,
    target_url: str,
    *,
    timeout_ms: int = LOGIN_REDIRECT_TIMEOUT_MS,
    interval_ms: int = LOGIN_REDIRECT_POLL_MS,
) -> bool:
    """等待 OAuth 完成且原考试路由的可用 DOM 已稳定挂载。

    ``timeout_ms=0`` 表示不设超时：只有页面真正就绪或用户关闭浏览器才会
    结束等待。正数超时仅供诊断工具和单元测试使用。
    """
    expected_url = normalize_url(target_url)
    elapsed = 0
    observed_external_route = False
    no_timeout = timeout_ms <= 0
    timeout_note = "不设超时" if no_timeout else f"最多 {timeout_ms / 1000:g} 秒"
    logging.info(f"等待登录授权完成并加载考试页面（{timeout_note}）")
    while no_timeout or elapsed < timeout_ms:
        current_url = normalize_url(str(getattr(page, "url", "") or ""))
        if current_url != expected_url:
            observed_external_route = True
        else:
            auth_completed = observed_external_route or await _has_authorization_cookie(page)
            if not auth_completed or not await _is_paper_entry_ready(page):
                wait_ms = interval_ms if no_timeout else min(interval_ms, timeout_ms - elapsed)
                await page.wait_for_timeout(wait_ms)
                elapsed += wait_ms
                continue
            try:
                await page.wait_for_load_state("load")
            except Exception:
                pass
            try:
                await page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass
            logging.info("登录授权已完成，考试页面内容已就绪")
            return True

        wait_ms = interval_ms if no_timeout else min(interval_ms, timeout_ms - elapsed)
        await page.wait_for_timeout(wait_ms)
        elapsed += wait_ms
    return False


async def _raise_if_login_required(page, target_url: str) -> None:
    if not await _wait_for_target_route_after_auth(page, target_url):
        raise UserCancelRequested(
            "考试链接进入登录页后未完成授权并加载考试内容；"
            "已保留当前及剩余考试链接，请先更新登录凭证后重试"
        )


async def _open_paper_answer_page(page, exam_button, *, popup_timeout_ms: int = 5000):
    """点击开考按钮，兼容新窗口和当前页两种答题页打开方式。"""
    try:
        async with page.expect_popup(timeout=popup_timeout_ms) as popup_info:
            await exam_button.click()
        return await popup_info.value
    except PlaywrightTimeoutError:
        if await _is_direct_answer_paper_page(page):
            logging.info("开考后在当前页面进入答题页")
            return page
        raise


def _build_exam_client() -> tuple[OpenAI, str]:
    client = OpenAI(
        api_key=OPENAI_COMPLETION_API_KEY,
        base_url=validate_ai_base_url(OPENAI_COMPLETION_BASE_URL),
        timeout=AI_REQUEST_TIMEOUT,
    )
    return client, MODEL_NAME


def _build_ai_exam_model_config(model: str) -> dict[str, object]:
    return {
        "model": model,
        "request_type": AI_REQUEST_TYPE,
        "web_search": AI_ENABLE_WEB_SEARCH,
        "thinking": AI_ENABLE_THINKING,
        "reasoning_effort": AI_REASONING_EFFORT,
    }


async def _is_direct_answer_paper_page(page) -> bool:
    elapsed = 0
    while elapsed < 5000:
        if await _has_ready_answer_question(page):
            return True
        await page.wait_for_timeout(250)
        elapsed += 250
    return False


async def _get_paper_attempt_limit_message(page) -> str | None:
    for selector in ("[data-region='modal:modal']", "body"):
        try:
            locator = page.locator(selector)
            if await locator.count() <= 0:
                continue
            text = (await locator.first.inner_text()).strip()
        except Exception:
            continue
        message = _extract_attempt_limit_message(text)
        if message:
            return message
    return None


async def _handle_attempt_limit_if_present(page, url: str) -> bool:
    attempt_limit_message = await _get_paper_attempt_limit_message(page)
    if not attempt_limit_message:
        return False

    queue_exam_url_by_attempt_text(
        url,
        attempt_limit_message,
        threshold=PAPER_EXAM_ATTEMPT_THRESHOLD,
        exam_file=EXAM_URLS_FILE,
        manual_exam_file=MANUAL_EXAM_FILE,
    )
    return True


async def _wait_for_paper_exam_button_or_attempt_limit(
    page,
    exam_button,
    *,
    timeout_ms: int = 5000,
    interval_ms: int = 250,
) -> str | None:
    last_exc: Exception | None = None
    checks = max(1, timeout_ms // interval_ms)

    for _ in range(checks):
        try:
            await exam_button.wait_for(timeout=interval_ms)
            return None
        except Exception as exc:
            last_exc = exc
            attempt_limit_message = await _get_paper_attempt_limit_message(page)
            if attempt_limit_message:
                return attempt_limit_message

    if last_exc is not None:
        raise last_exc
    return None


async def _can_continue_ai_exam(
    button_locator,
    *,
    threshold: int,
    url: str,
) -> bool:
    button_text = await button_locator.inner_text()
    if "剩余" not in button_text:
        logging.info("不限制考试次数, 继续 AI 自动考试")
        return True

    remaining = parse_remaining_attempts(button_text)
    if remaining is None:
        queue_exam_url_by_attempt_text(
            url,
            button_text,
            threshold=threshold,
            exam_file=EXAM_URLS_FILE,
            manual_exam_file=MANUAL_EXAM_FILE,
        )
        return False

    if remaining <= threshold:
        queue_exam_url_by_attempt_text(
            url,
            button_text,
            threshold=threshold,
            exam_file=EXAM_URLS_FILE,
            manual_exam_file=MANUAL_EXAM_FILE,
        )
        return False

    logging.info(f"当前考试剩余次数为 {remaining}, 大于 {threshold} 次, 继续 AI 自动考试")
    return True


async def _open_course_exam_tab(page) -> None:
    await page.locator(".top").first.wait_for(timeout=5000)
    await page.locator(".top").first.click()
    await page.locator('dl.chapter-list-box[data-sectiontype="9"]').click()
    await page.locator(".tab-container").wait_for()
    await page.wait_for_timeout(1000)


async def _handle_exam_result(page) -> None:
    # 考试结果页勿跑通用顶层关闭：交卷确认/结果弹窗可能被误关
    await page.reload(wait_until="load")
    await page.wait_for_timeout(1500)
    if await handle_rating_popup(page):
        logging.info("五星评价完成")


async def _close_page_safely(page) -> None:
    if page is None:
        return
    try:
        await page.close()
    except Exception:
        pass


async def _is_course_exam_in_progress(page) -> bool:
    status = page.locator(".neer-status")
    if await status.count() == 0:
        return False
    status_text = await status.inner_text()
    return "考试中" in status_text


async def _run_course_ai_exam(
    page,
    url: str,
    client: OpenAI,
    model: str,
    *,
    auto_submit: bool = True,
) -> None:
    ai_attempted = False
    model_config = _build_ai_exam_model_config(model)
    while True:
        await _open_course_exam_tab(page)

        exam_button = page.locator(COURSE_EXAM_BUTTON)
        if await exam_button.count() > 0:
            can_continue = await _can_continue_ai_exam(
                exam_button,
                threshold=COURSE_EXAM_ATTEMPT_THRESHOLD,
                url=url,
            )
            if not can_continue:
                return

        if await page.locator(".neer-status").count() > 0:
            if await _is_course_exam_in_progress(page):
                logging.info("课程考试正在进行中, 继续 AI 自动考试")
            elif await check_exam_passed(page):
                return
            elif ai_attempted:
                logging.info("AI 自动考试仍未通过, 转为人工考试")
                record_ai_failed_model_config(url, model_config, file_path=EXAM_URLS_FILE)
                logging.info(f"记录 AI 考试未通过模型配置: {model_config}, 考试链接: {url.strip()}")
                append_manual_exam_entry(
                    url,
                    reason="ai_failed",
                    reason_text="AI 自动考试仍未通过",
                    ai_failed_model_config=model_config,
                    file_path=MANUAL_EXAM_FILE,
                )
                return
            else:
                logging.info(
                    "考试结果未通过但剩余次数满足 AI 考试条件, 继续 AI 自动考试一次"
                )

        logging.info("开始 AI 自动考试")
        try:
            await wait_for_finish_test(
                client,
                model,
                page,
                auto_submit=auto_submit,
                ai_model_config=model_config,
            )
        except Exception:
            if await _handle_attempt_limit_if_present(page, url):
                return
            raise
        ai_attempted = True
        await _handle_exam_result(page)


async def _run_paper_ai_exam(
    page,
    url: str,
    client: OpenAI,
    model: str,
    *,
    auto_submit: bool = True,
) -> None:
    model_config = _build_ai_exam_model_config(model)
    await _raise_if_login_required(page, url)
    if await _handle_attempt_limit_if_present(page, url):
        return

    if await _has_ready_answer_question(page):
        logging.info("试卷页已直接进入答题页, 继续 AI 自动考试")
        await ai_exam(
            client,
            model,
            page,
            page.url,
            auto_submit=auto_submit,
            ai_model_config=model_config,
        )
        return

    exam_button = await _locate_exam_button(page)
    if exam_button is None:
        logging.warning("授权回跳后仍无法定位考试按钮")
        exam_button = page.locator(PAPER_EXAM_BUTTONS[0])
    attempt_limit_message = await _wait_for_paper_exam_button_or_attempt_limit(
        page,
        exam_button,
    )
    if attempt_limit_message:
        await _handle_attempt_limit_if_present(page, url)
        return

    can_continue = await _can_continue_ai_exam(
        exam_button,
        threshold=PAPER_EXAM_ATTEMPT_THRESHOLD,
        url=url,
    )
    if not can_continue:
        return

    logging.info("等待作答完毕并关闭试卷考试页面")
    answer_page = await _open_paper_answer_page(page, exam_button)
    await ai_exam(
        client,
        model,
        answer_page,
        page.url,
        auto_submit=auto_submit,
        ai_model_config=model_config,
    )


async def run_ai_exam_batch(
    status_callback: StatusCallback | None = None,
    *,
    auto_submit: bool = False,
) -> int:
    urls = read_exam_urls(EXAM_URLS_FILE)
    if not urls:
        return 0

    pending_urls = list(urls)
    client, model = _build_exam_client()
    model_config = _build_ai_exam_model_config(model)
    retained_urls: list[str] = []
    try:
        async with create_browser_context() as (_, context):
            for index, url in enumerate(urls, start=1):
                page = None
                entry_type = classify_exam_entry_url(url)
                if has_ai_failed_model_config(url, model_config, file_path=EXAM_URLS_FILE):
                    message = (
                        f"当前模型配置 {model_config} 已记录为该链接 AI 考试未通过，"
                        f"请更换模型后再运行 AI 自动考试，或改走人工考试；跳过当前链接: {url}"
                    )
                    logging.info(message)
                    retained_urls.append(url)
                    pending_urls.pop(0)
                    continue

                if entry_type == "subject":
                    logging.warning(
                        f"主题链接误入考试队列，应先展开主题中的课程/考试；"
                        f"已保留当前链接: {url}"
                    )
                    retained_urls.append(url)
                    pending_urls.pop(0)
                    continue

                try:
                    page = await context.new_page()
                    if status_callback:
                        status_callback(f"AI 考试 {index}/{len(urls)}: {url}")
                    logging.info(f"当前考试链接为: {url}")
                    # 考试全流程不接入通用顶层弹窗关闭，避免误关交卷/提示框
                    await page.goto(url)
                    await page.wait_for_load_state("load")

                    if entry_type == "course":
                        await _run_course_ai_exam(
                            page,
                            url,
                            client,
                            model,
                            auto_submit=auto_submit,
                        )
                    elif entry_type == "exam":
                        await _run_paper_ai_exam(
                            page,
                            url,
                            client,
                            model,
                            auto_submit=auto_submit,
                        )
                    else:
                        logging.info("未知考试链接类型, 转为人工考试")
                        append_manual_exam_entry(
                            url,
                            reason="unknown_url_type",
                            reason_text="未知考试链接类型",
                            file_path=MANUAL_EXAM_FILE,
                        )
                except UserAbortRequested as exc:
                    if getattr(exc, "save_pending_urls", True):
                        write_exam_urls(retained_urls + pending_urls, file_path=EXAM_URLS_FILE)
                    raise
                except UserCancelRequested:
                    write_exam_urls(retained_urls + pending_urls, file_path=EXAM_URLS_FILE)
                    raise
                except ExamQuestionExtractionError as exc:
                    write_exam_urls(retained_urls + pending_urls, file_path=EXAM_URLS_FILE)
                    message = (
                        f"{exc}；已保留当前及剩余考试链接。"
                        "答题页将保持打开，请检查页面；关闭后返回主菜单"
                    )
                    logging.error(message)
                    if status_callback:
                        status_callback(message)
                    answer_page = getattr(exc, "page", None) or page
                    try:
                        if answer_page is not None and is_browser_connected(context):
                            await answer_page.wait_for_event("close", timeout=0)
                    except Exception as close_exc:
                        if not is_target_closed_exception(close_exc):
                            logging.debug(f"等待异常答题页关闭时出错: {close_exc}")
                    raise UserCancelRequested(message) from None
                except ExamAiConfigurationError:
                    write_exam_urls(retained_urls + pending_urls, file_path=EXAM_URLS_FILE)
                    raise
                except Exception as exc:
                    if is_target_closed_exception(exc):
                        if is_browser_connected(context):
                            logging.info(f"考试标签页已关闭，跳过当前链接: {url}")
                            pending_urls.pop(0)
                            continue
                        else:
                            write_exam_urls(retained_urls + pending_urls, file_path=EXAM_URLS_FILE)
                            raise UserCancelRequested(
                                "浏览器窗口已关闭，已保留剩余考试链接，返回主菜单"
                            ) from None
                    else:
                        logging.error(f"AI 自动考试失败: {exc}")
                        logging.error(traceback.format_exc())
                        append_manual_exam_entry(
                            url,
                            reason="ai_exam_error",
                            reason_text=f"AI 自动考试失败: {exc}",
                            ai_failed_model_config=model_config,
                            file_path=MANUAL_EXAM_FILE,
                        )
                finally:
                    await _close_page_safely(page)
                pending_urls.pop(0)
    except BaseException as exc:
        if isinstance(exc, (SystemExit, GeneratorExit)):
            raise
        if isinstance(exc, (UserAbortRequested, UserCancelRequested, ExamAiConfigurationError)):
            raise
        if isinstance(exc, asyncio.CancelledError):
            # TUI Ctrl+C / 任务取消：保存剩余考试链接，返回主菜单
            write_exam_urls(retained_urls + pending_urls, file_path=EXAM_URLS_FILE)
            raise UserCancelRequested(
                "已中断 AI 自动考试，已保存剩余考试链接，返回主菜单"
            ) from None
        if not isinstance(exc, Exception):
            # 命令行 Ctrl+C：保存后退出
            write_exam_urls(retained_urls + pending_urls, file_path=EXAM_URLS_FILE)
            raise UserAbortRequested(
                "已收到 Ctrl+C，已保存剩余考试链接，程序退出"
            ) from None
        raise

    write_exam_urls(retained_urls + pending_urls, file_path=EXAM_URLS_FILE)
    return len(read_manual_exam_queue(MANUAL_EXAM_FILE))


async def _wait_for_manual_course_test(page) -> None:
    async with page.expect_popup() as popup_info:
        await page.locator(COURSE_EXAM_BUTTON).click()
    popup = await popup_info.value
    logging.info("等待手动考试完成并关闭页面")
    await popup.wait_for_event("close", timeout=0)


async def _wait_for_manual_paper_test(page, exam_button=None) -> None:
    if exam_button is None:
        exam_button = await _locate_exam_button(page)
    if exam_button is None:
        logging.warning("无法定位考试按钮，使用默认选择器")
        exam_button = page.locator(PAPER_EXAM_BUTTONS[0])
    await exam_button.wait_for(timeout=5000)
    answer_page = await _open_paper_answer_page(page, exam_button)
    logging.info("等待手动试卷考试完成并关闭页面")
    await answer_page.wait_for_event("close", timeout=0)


async def _run_manual_course_exam(page, url: str) -> None:
    while True:
        await page.wait_for_timeout(1000)
        await _open_course_exam_tab(page)
        if await page.locator(".neer-status").count() > 0:
            if await check_exam_passed(page):
                return
            logging.info(f"课程考试未通过，重新考试: {url}")
            await _wait_for_manual_course_test(page)
        else:
            logging.info(f"开始手动课程考试: {url}")
            await _wait_for_manual_course_test(page)

        # 考试结果 reload 后同样不跑通用关闭
        await page.reload(wait_until="load")
        await page.wait_for_timeout(1500)
        if await handle_rating_popup(page):
            logging.info("五星评价完成")


async def _run_manual_paper_exam(page, url: str) -> None:
    logging.info(f"开始手动试卷考试: {url}")
    await _raise_if_login_required(page, url)
    if await _has_ready_answer_question(page):
        logging.info("试卷已直接进入答题页，等待手动完成并关闭页面")
        await page.wait_for_event("close", timeout=0)
        return

    exam_button = await _locate_exam_button(page)
    await _wait_for_manual_paper_test(page, exam_button=exam_button)


async def run_manual_exam_batch(
    status_callback: StatusCallback | None = None,
    manual_exam_file=MANUAL_EXAM_FILE,
) -> int:
    entries = read_manual_exam_queue(manual_exam_file)
    if not entries:
        return 0

    processed = 0
    pending_entries = list(entries)
    retained_entries: list[ManualExamEntry] = []
    try:
        async with create_browser_context() as (_, context):
            for index, entry in enumerate(entries, start=1):
                url = entry.url
                page = None
                entry_type = classify_exam_entry_url(url)
                try:
                    page = await context.new_page()
                    if status_callback:
                        status_callback(f"人工考试 {index}/{len(entries)}: {url}")
                    logging.info(f"当前人工考试链接为: {url}")
                    await page.goto(url)
                    await page.wait_for_load_state("load")

                    if entry_type == "course":
                        await _run_manual_course_exam(page, url)
                    elif entry_type == "exam":
                        await _run_manual_paper_exam(page, url)
                    else:
                        logging.info("未知人工考试链接类型, 保留待处理")
                        retained_entries.append(entry)
                        pending_entries.pop(0)
                        continue

                    processed += 1
                except UserAbortRequested as exc:
                    if getattr(exc, "save_pending_urls", True):
                        write_manual_exam_queue(
                            retained_entries + pending_entries,
                            file_path=manual_exam_file,
                        )
                    raise
                except UserCancelRequested:
                    write_manual_exam_queue(
                        retained_entries + pending_entries,
                        file_path=manual_exam_file,
                    )
                    raise
                except Exception as exc:
                    if is_target_closed_exception(exc):
                        if is_browser_connected(context):
                            logging.info(f"考试标签页已关闭，跳过当前链接: {url}")
                            pending_entries.pop(0)
                            continue
                        else:
                            write_manual_exam_queue(
                                retained_entries + pending_entries,
                                file_path=manual_exam_file,
                            )
                            raise UserCancelRequested(
                                "浏览器窗口已关闭，已保留剩余人工考试链接，返回主菜单"
                            ) from None
                    else:
                        logging.error(f"人工考试流程失败: {exc}")
                        logging.error(traceback.format_exc())
                        retained_entries.append(entry)
                        pending_entries.pop(0)
                        continue
                finally:
                    await _close_page_safely(page)
                pending_entries.pop(0)
    except BaseException as exc:
        if isinstance(exc, (SystemExit, GeneratorExit)):
            raise
        if isinstance(exc, (UserAbortRequested, UserCancelRequested)):
            raise
        if isinstance(exc, asyncio.CancelledError):
            # TUI Ctrl+C / 任务取消：保存剩余人工考试链接，返回主菜单
            write_manual_exam_queue(
                retained_entries + pending_entries,
                file_path=manual_exam_file,
            )
            raise UserCancelRequested(
                "已中断人工考试，已保存剩余人工考试链接，返回主菜单"
            ) from None
        if not isinstance(exc, Exception):
            # 命令行 Ctrl+C：保存后退出
            write_manual_exam_queue(
                retained_entries + pending_entries,
                file_path=manual_exam_file,
            )
            raise UserAbortRequested(
                "已收到 Ctrl+C，已保存剩余人工考试链接，程序退出"
            ) from None
        raise

    write_manual_exam_queue(
        retained_entries,
        file_path=manual_exam_file,
        keep_file=False,
    )

    return processed
