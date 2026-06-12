from __future__ import annotations

import logging
import traceback

from core.abort import UserAbortRequested
from core.config import MANUAL_EXAM_FILE
from core.manual_exam_queue import append_manual_exam_entry


def _get_option_click_selector(question_data) -> str:
    return question_data.get("option_click_selector", ".preview-list dd")


def _is_exam_auto_submitted_error(exc: Exception) -> bool:
    message = str(exc)
    return "已超过考试时长" in message and (
        "自动提交" in message or "自动交卷" in message
    )


def _raise_if_exam_auto_submitted(exc: Exception) -> None:
    if _is_exam_auto_submitted_error(exc):
        raise UserAbortRequested(
            "考试已超过时长，系统已自动交卷，程序退出",
            save_pending_urls=False,
        ) from None


async def select_answers(
    page,
    question_data,
    answers,
    course_url,
    selector_prefix="",
    ai_model_config: dict[str, object] | None = None,
):
    """
    根据AI答案选择选项(统一处理单题目和多题目模式)。

    Args:
        selector_prefix: CSS选择器前缀, 多题目模式传入 "[data-dynamic-key='xxx'] "
    """
    try:
        question_index = question_data.get("index", 0)
        log_prefix = f"题目 {question_index + 1}: " if selector_prefix else ""

        if not answers:
            if question_data["type"] == "fill_blank":
                logging.info(f"{log_prefix}检测到填空题, 存入人工考试链接备查")
                reason = "fill_blank"
                reason_text = f"{log_prefix}检测到填空题"
            else:
                logging.info(
                    f"{log_prefix}没有获取到有效答案, 可能是 AI 作答失败或题目解析不完整, 存入人工考试链接备查"
                )
                reason = "ai_no_answer"
                reason_text = (
                    f"{log_prefix}没有获取到有效答案, 可能是 AI 作答失败或题目解析不完整"
                )
            append_manual_exam_entry(
                course_url,
                reason=reason,
                reason_text=reason_text,
                ai_failed_model_config=ai_model_config,
                file_path=MANUAL_EXAM_FILE,
            )
            return

        logging.info(f"{log_prefix}选择答案: {answers}")

        if question_data["type"] == "fill_blank":
            logging.info(f"{log_prefix}填空题, 跳过自动作答")
            return

        if question_data["type"] == "ordering":
            answer_sequence = "".join(answers)
            logging.info(f"{log_prefix}输入排序顺序: {answer_sequence}")
            try:
                selector = f"{selector_prefix}.answer-input-shot"
                await page.fill(selector, answer_sequence)
                logging.info(f"{log_prefix}已输入排序顺序: {answer_sequence}")
            except Exception as exc:
                _raise_if_exam_auto_submitted(exc)
                logging.warning(f"{log_prefix}输入排序顺序失败: {exc}")
            return

        if question_data["type"] == "judge":
            answer_label = "T" if answers[0] == "正确" else "F"
            answer_index = next(
                (
                    index
                    for index, option in enumerate(question_data.get("options", []))
                    if option.get("label") == answer_label
                ),
                0 if answer_label == "T" else 1,
            )
            try:
                selector = f"{selector_prefix}{_get_option_click_selector(question_data)}"
                await page.locator(selector).nth(answer_index).click(timeout=2000)
                logging.info(f"{log_prefix}已点击判断题选项: {answers[0]}")
            except Exception as exc:
                _raise_if_exam_auto_submitted(exc)
                logging.warning(f"{log_prefix}点击判断题选项失败: {exc}")
            return

        for answer in answers:
            option_index = ord(answer) - ord("A")
            if 0 <= option_index < len(question_data["options"]):
                try:
                    selector = f"{selector_prefix}{_get_option_click_selector(question_data)}"
                    await page.locator(selector).nth(option_index).click(timeout=2000)
                    logging.info(f"{log_prefix}已点击选项: {answer}")
                    await page.wait_for_timeout(300)
                except Exception as exc:
                    _raise_if_exam_auto_submitted(exc)
                    logging.warning(f"{log_prefix}点击选项 {answer} 失败: {exc}")
    except UserAbortRequested:
        raise
    except Exception as exc:
        logging.error(f"选择答案出错: {exc}")
        logging.error(traceback.format_exc())


async def close_exam_notice_if_present(page):
    try:
        popup = page.locator(".dialog.animated")
        if await popup.count() > 0:
            logging.info("检测到考试提示弹窗, 准备关闭")
            await popup.locator(".dialog-footer .btn").first.click()
            await page.wait_for_timeout(1000)
            logging.info("弹窗已关闭")
        else:
            logging.info("未检测到考试提示弹窗")
    except Exception as exc:
        logging.error(f"处理考试提示弹窗时出错: {exc}")
        await page.wait_for_timeout(2000)


async def submit_exam(page):
    await page.locator("text=我要交卷").click()
    await page.wait_for_timeout(1000)
    await page.locator("button:has-text('确 定')").click()
    await page.wait_for_timeout(1000)
    close_button = page.locator(
        "[data-region='modal:modal'] .btn.white.border:has-text('确定')"
    )
    if await close_button.count() > 0:
        await close_button.last.click()
