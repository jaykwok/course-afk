from __future__ import annotations

import asyncio
import logging
import hashlib
from typing import Callable

from openai import AsyncOpenAI
from playwright.async_api import Locator
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from core.abort import UserAbortRequested, UserCancelRequested
from core.browser.session import (
    create_browser_context,
    get_page_context,
    is_target_closed_exception,
    raise_for_certificate_error,
)
from core.config import (
    COURSE_EXAM_ATTEMPT_THRESHOLD,
    EXAM_URLS_FILE,
    MANUAL_EXAM_FILE,
    PAPER_EXAM_ATTEMPT_THRESHOLD,
    ZHIXUEYUN_COURSE_PREFIX,
    ZHIXUEYUN_EXAM_PREFIXES,
    ZHIXUEYUN_SUBJECT_PREFIX,
)
from core.exam.flow import ExamQuestionExtractionError, ai_exam, wait_for_finish_test
from core.exam.answers import ExamAiConfigurationError
from core.exam.settings import AiSettings
from core.exam.entry_api import (
    ExamEntryUnavailable, exam_id_from_url, read_exam_entry, validate_exam_entry,
)
from core.runtime import close_safely
from core.exam.rules import extract_attempt_limit_message as _extract_attempt_limit_message, parse_remaining_attempts
from core.exam.routing import allows_auto_submit, queue_exam_url_by_attempt_text, requeue_ai_exam_reviews
from core.file_ops import normalize_url
from core.queues.exam import (
    has_ai_failed_model_config,
    read_exam_urls,
    record_ai_failed_model_config,
    remove_exam_url,
    requires_manual_submission,
)
from core.learning.exam_bridge import get_course_exam_outcome
from core.learning.exam_api import read_exam_state
from core.queues.history import record_submission_intent, pending_submission, finish_submission
from core.auth.credential import load_credential_metadata
from core.learning.popups import handle_rating_popup
from core.queues.manual_exam import (
    append_manual_exam_entry,
    read_manual_exam_queue,
    remove_manual_exam_url,
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

LOGIN_REDIRECT_TIMEOUT_MS = 120000
LOGIN_REDIRECT_POLL_MS = 500


def classify_exam_entry_url(url: str) -> str:
    """按标准 hash 路由区分考试入口，避免普通查询参数误命中。"""
    normalized = normalize_url(url)
    if normalized.startswith(ZHIXUEYUN_EXAM_PREFIXES):
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
    require_entry_dom: bool = True,
) -> bool:
    """等待 OAuth 返回原考试路由；接口调用可跳过按钮/题目 DOM 等待。

    自动认证有绝对期限；人工答题等待另行处理，不能让登录无限挂起。
    """
    expected_url = normalize_url(target_url)
    elapsed = 0
    observed_external_route = False
    timeout_ms = max(1, timeout_ms)
    interval_ms = max(1, interval_ms)
    logging.info("等待登录授权完成并加载考试页面（最多 %g 秒）", timeout_ms / 1000)
    async def wait_ready():
        nonlocal elapsed, observed_external_route
        while elapsed < timeout_ms:
            if await check_ready():
                return True
            wait_ms = min(interval_ms, timeout_ms - elapsed)
            await page.wait_for_timeout(wait_ms)
            elapsed += wait_ms
        return False

    async def check_ready():
        nonlocal observed_external_route
        current_url = normalize_url(str(getattr(page, "url", "") or ""))
        if current_url != expected_url:
            observed_external_route = True
            return False
        auth_completed = observed_external_route or await _has_authorization_cookie(page)
        if not auth_completed or (require_entry_dom and not await _is_paper_entry_ready(page)):
            return False
        logging.info("登录授权已完成，考试入口已就绪")
        return True

    try:
        async with asyncio.timeout(timeout_ms / 1000):
            return await wait_ready()
    except TimeoutError:
        return False


async def _raise_if_login_required(page, target_url: str) -> None:
    if not await _wait_for_target_route_after_auth(page, target_url, require_entry_dom=False):
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


def _build_exam_client() -> tuple[AsyncOpenAI, str]:
    settings = AiSettings.load()
    client = AsyncOpenAI(api_key=settings.api_key, base_url=settings.base_url, timeout=settings.request_timeout, max_retries=0)
    client._course_afk_settings = settings
    return client, settings.model


def _current_account() -> str:
    metadata = load_credential_metadata()
    identity = (metadata.account_name or metadata.account_label) if metadata else "unbound"
    return hashlib.sha256(identity.encode()).hexdigest()[:16]


def _build_ai_exam_model_config(model: str, client=None) -> dict[str, object]:
    settings = getattr(client, "_course_afk_settings", None) or AiSettings.load()
    return {**settings.fingerprint(), "model": model, "account": _current_account()}


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




async def _open_course_exam_tab(page) -> None:
    from core.learning.flows import prepare_course_page

    # 评分/归档提示会遮住章节；新版页面还可能默认打开 AI 伴学视图。
    await prepare_course_page(page)
    await page.locator("dl.chapter-list-box").first.wait_for(state="visible", timeout=15000)
    await handle_rating_popup(page)
    sections = page.locator('dl.chapter-list-box[data-sectiontype="9"]')
    await sections.first.wait_for(state="visible", timeout=10000)
    if await sections.count() != 1:
        raise UserCancelRequested("课程包含多个考试章节，无法确认目标；请添加具体试卷链接后重试，原待办已保留")
    target = sections.locator(".section-item-wrapper")
    try:
        await target.click(timeout=5000)
    except PlaywrightTimeoutError:
        # 弹窗可能在章节渲染后才出现；只处理课程评分，不操作答题/交卷弹窗。
        if not await handle_rating_popup(page):
            raise
        await target.click(timeout=5000)
    await page.locator(".tab-container").wait_for(state="visible", timeout=15000)
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
    await close_safely(page.close(), label="exam page")


def _to_manual(url: str, reason: str, message: str, model_config=None) -> bool:
    """目标先落盘；调用方随后删除 AI 待办，崩溃窗口只允许重复，不允许丢失。"""
    if reason == "ai_failed" and model_config:
        record_ai_failed_model_config(url, model_config, file_path=EXAM_URLS_FILE)
    append_manual_exam_entry(url, reason=reason, reason_text=message,
        ai_failed_model_config=model_config if reason == "ai_failed" else None,
        file_path=MANUAL_EXAM_FILE)
    return True


async def _paper_state(page, url):
    try:
        return await read_exam_state(page, exam_id_from_url(url))
    except Exception as exc:
        if is_target_closed_exception(exc):
            raise
        logging.warning("试卷结果无法核对（%s）", type(exc).__name__)
        return None


def _finish_ai_outcome(url, outcome, model_config) -> bool:
    if outcome == "passed":
        finish_submission(url, "verified", EXAM_URLS_FILE, account=model_config["account"])
        remove_manual_exam_url(url, file_path=MANUAL_EXAM_FILE)
        return True
    if outcome == "pending_grading":
        _to_manual(url, "pending_grading", "已提交，等待服务端评卷后复查")
        finish_submission(url, "pending_grading", EXAM_URLS_FILE, account=model_config["account"])
        return True
    if outcome == "failed":
        _to_manual(url, "ai_failed", "本次已交卷但未通过，转人工处理", model_config)
        finish_submission(url, "failed", EXAM_URLS_FILE, account=model_config["account"])
        return True
    return _to_manual(url, "submission_unverified", "交卷结果尚未确认，请核对考试记录；禁止自动重考")


async def _run_course_ai_exam(page, url: str, client: AsyncOpenAI, model: str, *, auto_submit: bool = True) -> bool:
    model_config = _build_ai_exam_model_config(model, client)
    await _open_course_exam_tab(page)
    outcome = await get_course_exam_outcome(page)
    if outcome in {"passed", "pending_grading"}:
        return _finish_ai_outcome(url, outcome, model_config)
    resume = bool(pending_submission(url, EXAM_URLS_FILE, account=model_config["account"]))
    button = page.locator(COURSE_EXAM_BUTTON)
    if await button.count() == 0:
        return _to_manual(url, "attempt_unknown", "未找到可用的开考入口")
    button_text = await button.inner_text()
    if _extract_attempt_limit_message(button_text) or parse_remaining_attempts(button_text) == 0 and "继续考试" not in button_text:
        return _to_manual(url, "attempt_limit", "平台没有剩余开考次数，保留复查")
    if resume and "继续考试" not in button_text:
        return _to_manual(url, "submission_unverified", "上次考试未确认且没有继续考试入口，保留复查，不自动重开")
    can_submit = allows_auto_submit(button_text, COURSE_EXAM_ATTEMPT_THRESHOLD)
    auto_submit = auto_submit and can_submit and not resume and not requires_manual_submission(url, file_path=EXAM_URLS_FILE)
    if not auto_submit:
        logging.info("继续 AI 作答，自动交卷已关闭（次数阈值、历史记录或用户设置）")
    # 在开始任何答题操作前提交意图，覆盖手动提前交卷和强制退出的窗口。
    if not resume:
        record_submission_intent(url, model_config, EXAM_URLS_FILE)
    await wait_for_finish_test(client, model, page, auto_submit=auto_submit, ai_model_config=model_config,
        resume=resume or "继续考试" in button_text or requires_manual_submission(url, file_path=EXAM_URLS_FILE))
    await _handle_exam_result(page)
    await _open_course_exam_tab(page)
    outcome = await get_course_exam_outcome(page)
    # DOM 无记录身份，失败结果也可能仍是上次记录；保守转人工核对。
    if outcome == "failed":
        outcome = "unknown"
    return _finish_ai_outcome(url, outcome, model_config)


async def _run_paper_ai_exam(page, url: str, client: AsyncOpenAI, model: str, *, auto_submit: bool = True) -> bool:
    model_config = _build_ai_exam_model_config(model, client)
    await _raise_if_login_required(page, url)
    direct_answer = await _has_ready_answer_question(page)
    entry = None
    if direct_answer:
        before = await _paper_state(page, url)
    else:
        try:
            entry = await read_exam_entry(page, url)
        except ExamEntryUnavailable as exc:
            return _to_manual(url, exc.reason, str(exc))
        before = entry.state
    if before and before.outcome in {"passed", "pending_grading"}:
        return _finish_ai_outcome(url, before.outcome, model_config)
    resume = bool(pending_submission(url, EXAM_URLS_FILE, account=model_config["account"]))
    continuing = bool(entry and entry.can_resume)
    if entry and resume and not continuing:
        return _to_manual(url, "submission_unverified", "上次考试未确认且接口没有可续考记录，保留复查，不自动重开")
    if entry:
        try:
            await validate_exam_entry(page, entry)
        except ExamEntryUnavailable as exc:
            return _to_manual(url, exc.reason, str(exc))
    can_submit = bool(before and (before.allowed_attempts == 0 or before.remaining_attempts is not None and before.remaining_attempts > max(1, PAPER_EXAM_ATTEMPT_THRESHOLD)))
    auto_submit = auto_submit and can_submit and not resume and not continuing and not requires_manual_submission(url, file_path=EXAM_URLS_FILE)
    if not auto_submit:
        logging.info("继续 AI 作答，自动交卷已关闭（次数阈值、历史记录或用户设置）")
    if not resume:
        record_submission_intent(url, model_config, EXAM_URLS_FILE)
    if entry:
        # Resolve the official answer route from the API ID; labels and popup
        # behavior do not participate in exam entry or resume decisions.
        await page.goto(entry.answer_url, timeout=30000)
        await _raise_if_login_required(page, entry.answer_url)
    await ai_exam(client, model, page, url, auto_submit=auto_submit, ai_model_config=model_config, resume=resume or direct_answer or continuing)
    after = await _paper_state(page, url)
    outcome = after.outcome if after else "unknown"
    if outcome == "failed" and not (
        before and (after.record_id and after.record_id != before.record_id
        or after.used_attempts is not None and before.used_attempts is not None and after.used_attempts > before.used_attempts)
    ):
        outcome = "unknown"
    return _finish_ai_outcome(url, outcome, model_config)


async def run_ai_exam_batch(status_callback: StatusCallback | None = None, *, auto_submit: bool = False) -> int:
    requeue_ai_exam_reviews(exam_file=EXAM_URLS_FILE, manual_exam_file=MANUAL_EXAM_FILE)
    urls = read_exam_urls(EXAM_URLS_FILE)
    if not urls:
        return 0
    client, model = _build_exam_client()
    try:
        model_config = _build_ai_exam_model_config(model, client)
        async with create_browser_context() as (_, context):
            for index, url in enumerate(urls, start=1):
                allow_submit = auto_submit and not has_ai_failed_model_config(url, model_config, file_path=EXAM_URLS_FILE)
                entry_type = classify_exam_entry_url(url)
                if entry_type not in {"course", "exam"}:
                    _to_manual(url, "unknown_url_type", "此入口需要人工展开或核对")
                    remove_exam_url(url, file_path=EXAM_URLS_FILE)
                    continue
                page = None
                try:
                    page = await context.new_page()
                    if status_callback:
                        status_callback(f"AI 考试 {index}/{len(urls)}: {url}")
                    await page.goto(url, timeout=30000)
                    runner = _run_course_ai_exam if entry_type == "course" else _run_paper_ai_exam
                    completed = await runner(page, url, client, model, auto_submit=allow_submit)
                    if completed is True:
                        remove_exam_url(url, file_path=EXAM_URLS_FILE)
                except (UserAbortRequested, UserCancelRequested, ExamAiConfigurationError):
                    raise
                except ExamQuestionExtractionError as exc:
                    raise UserCancelRequested(f"{exc}；题目提取不完整，已保留考试待办") from exc
                except Exception as exc:
                    raise_for_certificate_error(exc)
                    if is_target_closed_exception(exc):
                        raise UserCancelRequested("考试页面已关闭，未确认完成的待办已保留") from None
                    logging.exception("AI 考试流程失败，转人工核对")
                    _to_manual(url, "ai_exam_error", f"流程失败（{type(exc).__name__}），请核对提交记录")
                    remove_exam_url(url, file_path=EXAM_URLS_FILE)
                finally:
                    await _close_page_safely(page)
    except asyncio.CancelledError:
        raise UserCancelRequested("已中断 AI 考试，未确认完成的待办已保留") from None
    finally:
        await close_safely(client.close(), label="AI client")
    return len(read_manual_exam_queue(MANUAL_EXAM_FILE))
