from __future__ import annotations

import logging
import re
import traceback
from typing import Callable

from openai import OpenAI

from core.abort import UserAbortRequested
from core.browser import create_browser_context, is_browser_connected, is_target_closed_exception
from core.config import (
    AI_ENABLE_THINKING,
    AI_ENABLE_WEB_SEARCH,
    AI_REASONING_EFFORT,
    AI_REQUEST_TYPE,
    COURSE_EXAM_ATTEMPT_THRESHOLD,
    EXAM_URLS_FILE,
    MANUAL_EXAM_FILE,
    MODEL_NAME,
    OPENAI_COMPLETION_API_KEY,
    OPENAI_COMPLETION_BASE_URL,
    PAPER_EXAM_ATTEMPT_THRESHOLD,
)
from core.exam_engine import ai_exam, wait_for_finish_test
from core.exam_answers import ExamAiConfigurationError
from core.exam_queue import (
    has_ai_failed_model_config,
    read_exam_urls,
    record_ai_failed_model_config,
    write_exam_urls,
)
from core.learning import check_exam_passed, handle_rating_popup
from core.manual_exam_queue import (
    ManualExamEntry,
    append_manual_exam_entry,
    read_manual_exam_queue,
    write_manual_exam_queue,
)


StatusCallback = Callable[[str], None]

COURSE_EXAM_BUTTON = ".btn.new-radius"
PAPER_EXAM_BUTTON = (
    ".banner-handler-btn.themeColor-border-color.themeColor-background-color"
)


def _extract_attempt_limit_message(text: str) -> str | None:
    for raw_line in text.replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if line and "考试次数限制" in line:
            return line
    return None


def parse_remaining_attempts(button_text: str) -> int | None:
    match = re.search(r"剩余(\d+)次", button_text)
    if not match:
        return None
    return int(match.group(1))


def should_route_exam_to_manual(button_text: str, threshold: int) -> bool:
    if "剩余" not in button_text:
        return False
    remaining = parse_remaining_attempts(button_text)
    if remaining is None:
        return True
    return remaining <= threshold


def _build_exam_client() -> tuple[OpenAI, str]:
    client = OpenAI(
        api_key=OPENAI_COMPLETION_API_KEY,
        base_url=OPENAI_COMPLETION_BASE_URL,
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
    try:
        await page.locator(".question-type-item, .single-title, .single-btns").first.wait_for(
            timeout=5000
        )
        return True
    except Exception:
        return False


async def _get_paper_attempt_limit_message(page) -> str | None:
    for selector in ("[data-region='modal:modal']", "body"):
        locator = page.locator(selector)
        try:
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

    append_manual_exam_entry(
        url,
        reason="attempt_limit",
        reason_text=attempt_limit_message,
        file_path=MANUAL_EXAM_FILE,
    )
    logging.info(f"检测到考试次数限制提示, 跳过当前考试: {attempt_limit_message}")
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
        reason_text = "无法解析剩余次数, 转为人工考试处理"
        logging.info(reason_text)
        append_manual_exam_entry(
            url,
            reason="attempt_unknown",
            reason_text=reason_text,
            threshold=threshold,
            file_path=MANUAL_EXAM_FILE,
        )
        return False

    if remaining <= threshold:
        reason_text = (
            f"当前考试剩余次数为 {remaining}, 小于等于 {threshold} 次, 转为人工考试"
        )
        logging.info(reason_text)
        append_manual_exam_entry(
            url,
            reason="attempt_threshold",
            reason_text=reason_text,
            remaining_attempts=remaining,
            threshold=threshold,
            file_path=MANUAL_EXAM_FILE,
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
    if await _is_direct_answer_paper_page(page):
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

    exam_button = page.locator(PAPER_EXAM_BUTTON)
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
    async with page.expect_popup() as popup_info:
        await exam_button.click()
    popup = await popup_info.value
    await ai_exam(
        client,
        model,
        popup,
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
                if has_ai_failed_model_config(url, model_config, file_path=EXAM_URLS_FILE):
                    message = (
                        f"当前模型配置 {model_config} 已记录为该链接 AI 考试未通过，"
                        f"请更换模型后再运行 AI 自动考试，或改走人工考试；跳过当前链接: {url}"
                    )
                    logging.info(message)
                    retained_urls.append(url)
                    pending_urls.pop(0)
                    continue

                try:
                    page = await context.new_page()
                    if status_callback:
                        status_callback(f"AI 考试 {index}/{len(urls)}: {url}")
                    logging.info(f"当前考试链接为: {url}")
                    await page.goto(url)
                    await page.wait_for_load_state("load")

                    if "course" in url:
                        await _run_course_ai_exam(
                            page,
                            url,
                            client,
                            model,
                            auto_submit=auto_submit,
                        )
                    elif "exam" in url:
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
                            raise UserAbortRequested(
                                "已关闭浏览器窗口，程序退出",
                                save_pending_urls=False,
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
        if isinstance(exc, (UserAbortRequested, ExamAiConfigurationError)):
            raise
        if not isinstance(exc, Exception):
            write_exam_urls(retained_urls + pending_urls, file_path=EXAM_URLS_FILE)
            raise UserAbortRequested("已收到 Ctrl+C，程序退出", save_pending_urls=False) from None
        raise

    write_exam_urls(retained_urls + pending_urls, file_path=EXAM_URLS_FILE)
    return len(read_manual_exam_queue(MANUAL_EXAM_FILE))


async def _wait_for_manual_course_test(page) -> None:
    async with page.expect_popup() as popup_info:
        await page.locator(COURSE_EXAM_BUTTON).click()
    popup = await popup_info.value
    logging.info("等待手动考试完成并关闭页面")
    await popup.wait_for_event("close", timeout=0)


async def _wait_for_manual_paper_test(page) -> None:
    exam_button = page.locator(PAPER_EXAM_BUTTON)
    await exam_button.wait_for(timeout=5000)
    async with page.expect_popup() as popup_info:
        await exam_button.click()
    popup = await popup_info.value
    logging.info("等待手动试卷考试完成并关闭页面")
    await popup.wait_for_event("close", timeout=0)


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

        await page.reload(wait_until="load")
        await page.wait_for_timeout(1500)
        if await handle_rating_popup(page):
            logging.info("五星评价完成")


async def _run_manual_paper_exam(page, url: str) -> None:
    logging.info(f"开始手动试卷考试: {url}")
    await _wait_for_manual_paper_test(page)


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
                try:
                    page = await context.new_page()
                    if status_callback:
                        status_callback(f"人工考试 {index}/{len(entries)}: {url}")
                    logging.info(f"当前人工考试链接为: {url}")
                    await page.goto(url)
                    await page.wait_for_load_state("load")

                    if "course" in url:
                        await _run_manual_course_exam(page, url)
                    elif "exam" in url:
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
                            raise UserAbortRequested(
                                "已关闭浏览器窗口，程序退出",
                                save_pending_urls=False,
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
        if isinstance(exc, UserAbortRequested):
            raise
        if not isinstance(exc, Exception):
            write_manual_exam_queue(
                retained_entries + pending_entries,
                file_path=manual_exam_file,
            )
            raise UserAbortRequested("已收到 Ctrl+C，程序退出", save_pending_urls=False) from None
        raise

    write_manual_exam_queue(
        retained_entries,
        file_path=manual_exam_file,
        keep_file=False,
    )

    return processed
