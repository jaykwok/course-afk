from __future__ import annotations

import logging

from core.exam_actions import close_exam_notice_if_present, select_answers, submit_exam
from core.exam_answers import get_ai_answers
from core.exam_parsing import (
    detect_exam_mode,
    extract_multi_questions_data,
    extract_single_question_data,
)

MANUAL_SUBMIT_RESULT_CLOSE_SELECTOR = (
    "[data-region='modal:modal'] .btn.white.border:has-text('确定')"
)


def _format_question_options(question_data) -> str:
    options = question_data.get("options") or []
    formatted_options = []
    for option in options:
        label = str(option.get("label", "")).strip()
        text = str(option.get("text", "")).strip()
        if label and text:
            formatted_options.append(f"{label}. {text}")
        elif text:
            formatted_options.append(text)
        elif label:
            formatted_options.append(label)
    return "\n".join(formatted_options) if formatted_options else "无"


def _log_question_snapshot(question_data, *, index: int | None = None) -> None:
    option_prefix = "题目选项" if index is None else f"题目 {index} 选项"
    logging.info(f"{option_prefix}:\n{_format_question_options(question_data)}")


def _should_disable_auto_submit(question_data, answers) -> bool:
    return question_data.get("type") == "fill_blank" or not answers


def _ensure_manual_submit(auto_submit: bool, question_data, answers) -> bool:
    if auto_submit and _should_disable_auto_submit(question_data, answers):
        logging.info("检测到需要人工处理的题目，已自动切换为手动交卷")
        return False
    return auto_submit


def _page_is_closed(page) -> bool:
    checker = getattr(page, "is_closed", None)
    if not callable(checker):
        return False
    try:
        return bool(checker())
    except Exception:
        return False


async def _wait_for_manual_submit_completion(page) -> None:
    while True:
        if _page_is_closed(page):
            return

        try:
            close_button = page.locator(MANUAL_SUBMIT_RESULT_CLOSE_SELECTOR)
            if await close_button.count() > 0:
                logging.info("检测到交卷结果弹窗, 准备关闭")
                await close_button.last.click()
                await page.wait_for_timeout(500)
                return
        except Exception as exc:
            if _page_is_closed(page):
                return
            logging.debug(f"等待手动交卷完成时检查结果弹窗失败: {exc}")

        await page.wait_for_timeout(500)


async def ai_exam(client, model, page, course_url, auto_submit=True, ai_model_config=None):
    """AI自动答题主函数"""
    logging.info("AI考试开始")

    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(1000)
    await close_exam_notice_if_present(page)
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(1000)

    exam_mode = await detect_exam_mode(page)

    if exam_mode == "single":
        while True:
            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(1000)

            question_data = await extract_single_question_data(page)
            if not question_data:
                logging.error("无法提取题目信息")
                break

            logging.info(f"当前题目: {question_data['text']}")
            logging.info(f"题目类型: {question_data['type']}")
            _log_question_snapshot(question_data)

            answers = await get_ai_answers(client, model, question_data)
            auto_submit = _ensure_manual_submit(auto_submit, question_data, answers)
            await select_answers(
                page,
                question_data,
                answers,
                course_url,
                ai_model_config=ai_model_config,
            )

            next_button = page.locator(".single-btn-next")
            next_button_classes = await next_button.get_attribute("class") or ""

            if "next-disabled" in next_button_classes:
                if auto_submit:
                    logging.info("已经是最后一题, 准备交卷")
                    await submit_exam(page)
                else:
                    logging.info("自动交卷已取消, 请手动交卷")
                    logging.info("页面将保持打开状态, 等待手动交卷完成...")
                    await _wait_for_manual_submit_completion(page)
                break

            logging.info("点击下一题")
            await next_button.click()
            await page.wait_for_timeout(1000)
    else:
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1000)

        all_questions = await extract_multi_questions_data(page)
        if not all_questions:
            logging.error("无法提取任何题目信息")
            return

        logging.info(f"本页共有 {len(all_questions)} 道题目")
        for question_data in all_questions:
            question_number = question_data["index"] + 1
            logging.info(f"处理题目 {question_number}: {question_data['text']}")
            logging.info(f"题目 {question_number} 类型: {question_data['type']}")
            _log_question_snapshot(question_data, index=question_number)
            answers = await get_ai_answers(client, model, question_data)
            auto_submit = _ensure_manual_submit(auto_submit, question_data, answers)
            item_id = question_data["item_id"]
            await select_answers(
                page,
                question_data,
                answers,
                course_url,
                selector_prefix=f"[data-dynamic-key='{item_id}'] ",
                ai_model_config=ai_model_config,
            )
            await page.wait_for_timeout(500)

        if auto_submit:
            try:
                await submit_exam(page)
            except Exception as exc:
                logging.error(f"点击交卷按钮失败: {exc}")
        else:
            logging.info("自动交卷已取消, 请手动交卷")
            logging.info("页面将保持打开状态, 等待手动交卷完成...")
            await _wait_for_manual_submit_completion(page)

    logging.info("考试完成")


async def wait_for_finish_test(client, model, page1, auto_submit=True, ai_model_config=None):
    """打开考试弹窗并执行AI考试"""
    async with page1.expect_popup() as page2_info:
        await page1.locator(".btn.new-radius").click()
    page2 = await page2_info.value
    logging.info("等待作答完毕并关闭页面")
    await ai_exam(
        client,
        model,
        page2,
        page1.url,
        auto_submit=auto_submit,
        ai_model_config=ai_model_config,
    )
    if _page_is_closed(page2):
        return
    await page2.wait_for_event("close", timeout=0)
