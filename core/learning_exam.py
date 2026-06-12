from __future__ import annotations

import logging
import re

from core.exam_queue import append_exam_url
from core.learning_common import get_course_url


async def check_exam_passed(page):
    """检测考试是否通过"""
    await page.wait_for_timeout(1000)
    try:
        status_element = await page.locator(".neer-status").count()
        if status_element > 0:
            highest_score_text = await page.locator(".neer-status").inner_text()
            if "考试中" in highest_score_text:
                logging.info("考试状态: 考试中")
                return False

        table_exists = await page.locator("div.tab-container table.table").count()
        if table_exists == 0:
            logging.info("考试状态: 未找到考试表格")
            return False

        first_row = page.locator("div.tab-container table.table tbody tr:first-child")
        if await first_row.count() == 0:
            logging.info("首次进入考试页面, 未进行考试")
            return False

        await first_row.wait_for(state="visible", timeout=1500)
        row_text = (await first_row.inner_text(timeout=3000)).strip()
        compact_row_text = re.sub(r"\s+", "", row_text)

        if any(
            keyword in compact_row_text
            for keyword in ("暂无考试记录", "暂未考试", "暂无记录")
        ):
            logging.info("考试状态: 暂无考试记录")
            return False

        if any(
            keyword in compact_row_text
            for keyword in ("去考试", "开始考试", "继续考试", "剩余")
        ):
            logging.info(f"考试状态: 未开始/可继续考试 ({row_text})")
            return False

        if any(keyword in compact_row_text for keyword in ("不及格", "未通过")):
            logging.info(f"考试状态: 未通过 ({row_text})")
            return False

        if "待评卷" in compact_row_text:
            logging.info("考试状态: 待评卷")
            return True

        if "及格" in compact_row_text or "通过" in compact_row_text:
            logging.info("考试状态: 通过")
            return True

        status_cell_element = first_row.locator("td:nth-child(4)")
        if await status_cell_element.count() == 0:
            logging.info(f"考试状态: 未识别到明确状态 ({row_text})")
            return False

        await status_cell_element.wait_for(state="visible", timeout=1500)
        status_cell = (await status_cell_element.inner_text(timeout=3000)).strip()
        compact_status = re.sub(r"\s+", "", status_cell)

        if compact_status == "及格":
            logging.info("考试状态: 通过")
            return True
        if compact_status == "待评卷":
            logging.info("考试状态: 待评卷")
            return True
        if compact_status in {"去考试", "开始考试", "继续考试"} or "剩余" in compact_status:
            logging.info(f"考试状态: 未开始/可继续考试 ({status_cell})")
            return False

        if compact_status in {"不及格", "未通过"}:
            logging.info(f"考试状态: 未通过 ({status_cell})")
            return False

        logging.info(f"考试状态: 未通过 ({status_cell})")
        return False
    except Exception as exc:
        logging.error(f"获取考试状态时出错: {exc}")
        return False


async def handle_examination(page, learn_item=None, exam_passed: bool | None = None):
    """处理考试类型课程"""
    if exam_passed is None:
        exam_passed = await check_exam_passed(page)

    if exam_passed:
        logging.info("考试已通过, 跳过该节")
        return

    logging.info("学习课程考试类型, 存入文档")
    if learn_item:
        course_url = await get_course_url(learn_item)
        append_exam_url(course_url)
        logging.info(f"链接: {course_url}\n")
    else:
        append_exam_url(page.url)
        logging.info(f"链接: {page.url}\n")


async def is_subject_url_completed(page):
    """判断学习主题中的URL是否学习完毕"""
    await page.wait_for_load_state("load")
    await page.locator(".item.current-hover").last.wait_for()
    await page.locator(".item.current-hover").locator(".section-type").last.wait_for()

    content = (
        await page.locator(".item.current-hover", has_not_text="重新学习")
        .filter(has_text="URL")
        .all()
    )
    return not bool(content)
