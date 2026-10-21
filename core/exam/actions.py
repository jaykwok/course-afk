from __future__ import annotations

import logging

from core.abort import UserAbortRequested, UserCancelRequested
from core.config import EXAM_OPTION_GAP_MAX, EXAM_OPTION_GAP_MIN, EXAM_SUBMIT_GAP_MAX, EXAM_SUBMIT_GAP_MIN, MANUAL_EXAM_FILE
from core.exam.contracts import question_problem, valid_answers
from core.humanize import pause_between
from core.queues.manual_exam import append_manual_exam_entry

SELECTED_STATE_SCRIPT = """
element => {
  const root = element.closest(".preview-list dd, .option-item, .answer-item") || element;
  const input = root.matches("input") ? root : root.querySelector("input[type=checkbox], input[type=radio]");
  if (input) return Boolean(input.checked);
  for (const node of [element, root, ...root.querySelectorAll("[aria-checked]")]) {
    const aria = node.getAttribute("aria-checked");
    if (aria === "true" || aria === "false") return aria === "true";
  }
  const selected = ".selected, .checked, .active, .ant-checkbox-checked, .ant-radio-checked, .is-checked";
  return root.matches(selected) || Boolean(root.querySelector(selected));
}
"""


def _get_option_click_selector(question_data) -> str:
    return question_data.get("option_click_selector", ".preview-list dd")


def _is_exam_auto_submitted_error(exc: Exception) -> bool:
    message = str(exc)
    return "已超过考试时长" in message and ("自动提交" in message or "自动交卷" in message)


def _raise_if_exam_auto_submitted(exc: Exception) -> None:
    if _is_exam_auto_submitted_error(exc):
        raise UserCancelRequested("考试已超时自动交卷，已保留待办供核对结果") from None


async def _selected(option) -> bool:
    value = await option.evaluate(SELECTED_STATE_SCRIPT)
    if not isinstance(value, bool):
        raise ValueError("无法验证选项状态")
    return value


async def read_existing_answers(page, question_data, *, selector_prefix="") -> list[str] | None:
    """读取已填答案；None 表示无法确认，不能把它当成空题覆盖。"""
    try:
        if question_data["type"] == "ordering":
            value = await page.locator(f"{selector_prefix}.answer-input-shot").input_value()
            return list(value.strip())
        options = page.locator(f"{selector_prefix}{_get_option_click_selector(question_data)}")
        labels = [option["label"] for option in question_data["options"]]
        if not labels or await options.count() != len(labels):
            return None
        return [label for index, label in enumerate(labels) if await _selected(options.nth(index))]
    except Exception as exc:
        _raise_if_exam_auto_submitted(exc)
        logging.warning("无法确认已有答案，保留当前题供人工核对：%s", type(exc).__name__)
        return None


async def select_answers(page, question_data, answers, course_url, selector_prefix="", ai_model_config=None):
    """Apply a desired answer set and verify it, rather than toggling blindly."""
    if not valid_answers(question_data, answers):
        reason = "fill_blank" if question_data.get("type") == "fill_blank" else "ai_no_answer"
        detail = question_problem(question_data) or question_data.get("answer_failure_reason") or "AI 未返回合法选项答案"
        number = question_data.get("index")
        identity = f"第 {number + 1} 题" if isinstance(number, int) else f"题目 {question_data.get('item_id', 'single')}"
        message = f"{identity}（{question_data.get('type', 'unknown')}）：{detail}；已关闭自动交卷，请核对该题"
        logging.info(message)
        append_manual_exam_entry(course_url, reason=reason, reason_text=message, ai_failed_model_config=None, file_path=MANUAL_EXAM_FILE)
        return False
    try:
        if question_data["type"] == "ordering":
            selector = f"{selector_prefix}.answer-input-shot"
            sequence = "".join(answers)
            await page.fill(selector, sequence)
            return await page.locator(selector).input_value() == sequence

        labels = [option["label"] for option in question_data["options"]]
        options = page.locator(f"{selector_prefix}{_get_option_click_selector(question_data)}")
        if await options.count() != len(labels):
            return False
        target = set(answers)
        # Radio groups clear the previous answer by selecting the target; clicking
        # a non-target radio would select it. Checkboxes need an explicit diff.
        indices = range(len(labels)) if question_data["type"] == "multiple" else [labels.index(answers[0])]
        for index in indices:
            option = options.nth(index)
            if await _selected(option) != (labels[index] in target):
                await option.click(timeout=2000)
                await pause_between(page, EXAM_OPTION_GAP_MIN, EXAM_OPTION_GAP_MAX)
        for _ in range(5):
            actual = {label for index, label in enumerate(labels) if await _selected(options.nth(index))}
            if actual == target:
                return True
            await page.wait_for_timeout(100)
        logging.warning("选项读回与目标不一致，保留人工核对")
        return False
    except (UserAbortRequested, UserCancelRequested):
        raise
    except Exception as exc:
        _raise_if_exam_auto_submitted(exc)
        logging.warning("选项操作未验证成功：%s", type(exc).__name__)
        return False


async def close_exam_notice_if_present(page):
    popup = page.locator(".dialog.animated")
    if await popup.count() > 0:
        await popup.locator(".dialog-footer .btn").first.click()
        await page.wait_for_timeout(1000)


async def submit_exam(page):
    await page.locator("text=我要交卷").click()
    await pause_between(page, EXAM_SUBMIT_GAP_MIN, EXAM_SUBMIT_GAP_MAX)
    await page.locator("button:has-text('确 定')").click()
    await pause_between(page, EXAM_SUBMIT_GAP_MIN, EXAM_SUBMIT_GAP_MAX)
    receipt = page.locator("[data-region='modal:modal'] .btn.white.border:has-text('确定')").last
    # Missing/closed result pages propagate as an unverified submission. Callers
    # retain the task and reconcile with the server before another attempt.
    await receipt.wait_for(state="visible", timeout=30000)
    await receipt.click()
