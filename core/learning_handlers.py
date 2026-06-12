from __future__ import annotations

import asyncio
import logging

from core.config import (
    DOCUMENT_INITIAL_WAIT,
    DOCUMENT_SYNC_EXTRA_WAIT,
)
from core.learning_common import (
    build_video_timing_plan,
    get_course_url,
    is_learned,
    timer,
)
from core.learning_queue import record_learning_failure
from core.learning_popups import check_and_handle_rating_popup, check_rating_popup_periodically


async def _cleanup_background_tasks(*tasks) -> None:
    active_tasks = [task for task in tasks if task is not None]
    if not active_tasks:
        return

    for task in active_tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*active_tasks, return_exceptions=True)


async def handle_video(box, page):
    """处理视频类型课程"""
    resume_button = await page.locator(".register-mask-layer").all()
    if resume_button:
        await resume_button[0].click()
    await page.locator(".vjs-progress-control").first.wait_for()
    await page.locator(".vjs-duration-display").wait_for()

    await check_and_handle_rating_popup(page)

    section_text = await box.locator(".section-item-wrapper").inner_text()
    timing_plan = build_video_timing_plan(section_text)
    logging.info(f"课程总时长: {timing_plan.total_time} 秒")
    logging.info(f"还需学习: {timing_plan.learning_wait_time} 秒")
    logging.info(f"fallback 进度日志间隔: {timing_plan.learning_fallback_interval} 秒")
    logging.info(f"预计额外等待同步: {timing_plan.sync_wait_time} 秒")
    if timing_plan.sync_wait_time > 0:
        logging.info(
            f"同步确认轮询间隔: {timing_plan.sync_poll_interval} 秒"
        )

    timer_task = asyncio.create_task(
        timer(
            timing_plan.learning_wait_time,
            fallback_interval=timing_plan.learning_fallback_interval,
            description="视频学习进度",
        )
    )
    popup_check_task = asyncio.create_task(
        check_rating_popup_periodically(page, timing_plan.learning_wait_time)
    )
    try:
        await page.wait_for_timeout(timing_plan.learning_wait_time * 1000)
        await timer_task
        await popup_check_task
    finally:
        await _cleanup_background_tasks(timer_task, popup_check_task)

    logging.info("课程学习完毕, 确认课程进度同步状态...")
    current_text = await box.locator(".section-item-wrapper").inner_text()
    if is_learned(current_text):
        logging.info("课程进度已同步到服务器")
        return

    elapsed_sync_wait = 0
    while elapsed_sync_wait < timing_plan.sync_wait_time:
        await check_and_handle_rating_popup(page)
        current_text = await box.locator(".section-item-wrapper").inner_text()
        if is_learned(current_text):
            logging.info(f"课程进度已同步到服务器, 额外等待 {elapsed_sync_wait} 秒")
            return

        wait_seconds = min(
            timing_plan.sync_poll_interval,
            timing_plan.sync_wait_time - elapsed_sync_wait,
        )
        logging.info(
            f"课程进度仍未同步完成, 已额外等待 {elapsed_sync_wait + wait_seconds} 秒, 继续等待..."
        )
        await page.wait_for_timeout(wait_seconds * 1000)
        elapsed_sync_wait += wait_seconds

    current_text = await box.locator(".section-item-wrapper").inner_text()
    if not is_learned(current_text):
        logging.info(
            f"超时: 已额外等待{timing_plan.sync_wait_time}秒, 课程进度仍未同步"
        )
        raise Exception("课程进度未能在理论等待时间内同步完成")


async def handle_document(page, box):
    """处理文档、网页类型课程"""
    await page.locator("[class*='fullScreen-content']").first.wait_for()
    await timer(DOCUMENT_INITIAL_WAIT, fallback_interval=1, description="文档学习进度")

    logging.info("课程学习完毕, 确认课程进度同步状态...")
    current_text = await box.locator(".section-item-wrapper").inner_text()
    if is_learned(current_text):
        logging.info("课程进度已同步到服务器")
        return

    for i in range(1, DOCUMENT_SYNC_EXTRA_WAIT + 1):
        await page.wait_for_timeout(1000)
        current_text = await box.locator(".section-item-wrapper").inner_text()
        if is_learned(current_text):
            logging.info(f"课程进度已同步到服务器, 额外等待 {i} 秒")
            return
        logging.info(f"课程进度仍未同步完成, 已额外等待 {i} 秒, 继续等待...")

    logging.info(f"超时: 已额外等待{DOCUMENT_SYNC_EXTRA_WAIT}秒, 课程进度仍未同步")
    raise Exception("课程进度未能在额外等待时间内同步完成")


async def handle_h5(page, learn_item):
    """处理h5类型课程"""
    logging.info("h5课程类型, 记录为需要人工处理")
    record_learning_failure(
        await get_course_url(learn_item),
        reason="h5_manual_required",
        reason_text="H5 课程类型需要人工处理",
        detail={"source": "course_chapter"},
    )
