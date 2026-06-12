from __future__ import annotations

import logging
import traceback

from core.question_parser import (
    detect_question_type_by_dom,
    extract_options_with_selector,
    parse_question_type,
)


async def detect_exam_mode(page):
    """检测考试模式：根据是否存在下一题按钮来判断"""
    try:
        single_btns = page.locator(".single-btns")
        await single_btns.wait_for(state="visible", timeout=3000)
        logging.info("检测为单题目模式(有下一题按钮)")
        return "single"
    except Exception:
        logging.info("检测为多题目模式(无下一题按钮)")
        return "multi"


async def extract_single_question_data(page):
    """提取单题目信息"""
    try:
        question_type_text = await page.locator(".o-score").last.inner_text()
        logging.debug(f"题目类型文本: {question_type_text}")

        question_type = parse_question_type(question_type_text)
        if question_type == "unknown":
            question_type = await detect_question_type_by_dom(page)

        question_text = await page.locator(
            ".single-title .rich-text-style"
        ).inner_text()
        logging.debug(f"题目内容: {question_text}")

        options, option_click_selector = await extract_options_with_selector(
            page, question_type
        )
        logging.debug(f"选项: {options}")

        question_data = {"type": question_type, "text": question_text, "options": options}
        if option_click_selector:
            question_data["option_click_selector"] = option_click_selector
        return question_data
    except Exception as exc:
        logging.error(f"提取题目信息出错: {exc}")
        logging.error(traceback.format_exc())
        return None


async def extract_multi_questions_data(page):
    """提取页面中所有题目的信息(多题目模式)"""
    try:
        question_items = page.locator(".question-type-item")
        count = await question_items.count()
        logging.info(f"检测到 {count} 个题目")

        all_questions = []
        for i in range(count):
            question_item = question_items.nth(i)
            question_type_text = await question_item.locator(".o-score").last.inner_text()
            logging.debug(f"题目 {i+1} 类型文本: {question_type_text}")

            question_type = parse_question_type(question_type_text)
            if question_type == "unknown":
                question_type = await detect_question_type_by_dom(question_item)

            try:
                if await question_item.locator(".stem-content-main").count() > 0:
                    question_text = await question_item.locator(
                        ".stem-content-main"
                    ).inner_text()
                else:
                    question_text = await question_item.locator(
                        ".single-title .rich-text-style"
                    ).inner_text()
            except Exception:
                logging.error(f"无法获取题目 {i+1} 的内容")
                continue

            logging.debug(f"题目 {i+1} 内容: {question_text}")
            options, option_click_selector = await extract_options_with_selector(
                question_item, question_type
            )
            logging.debug(f"题目 {i+1} 选项: {options}")

            item_id = await question_item.get_attribute("data-dynamic-key") or f"item-{i}"
            question_data = {
                "index": i,
                "type": question_type,
                "text": question_text,
                "options": options,
                "item_id": item_id,
            }
            if option_click_selector:
                question_data["option_click_selector"] = option_click_selector
            all_questions.append(question_data)

        return all_questions
    except Exception as exc:
        logging.error(f"提取所有题目信息出错: {exc}")
        logging.error(traceback.format_exc())
        return []
