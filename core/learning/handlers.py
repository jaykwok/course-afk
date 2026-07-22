from __future__ import annotations

import asyncio
import logging

from core.config import (
    DOCUMENT_INITIAL_WAIT,
    DOCUMENT_SYNC_EXTRA_WAIT,
    DOCUMENT_SYNC_POLL_INTERVAL,
)
from core.learning.common import (
    build_video_timing_plan,
    get_course_url,
    timer,
    wait_until_learned,
)
from core.queues.learning import record_learning_failure
from core.learning.popups import check_and_handle_rating_popup, check_rating_popup_periodically


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
    logging.info(f"预计额外等待同步: {timing_plan.sync_wait_time} 秒")
    if timing_plan.sync_wait_time > 0:
        logging.info(
            f"同步确认轮询间隔: {timing_plan.sync_poll_interval} 秒"
        )

    learning_ms = timing_plan.learning_wait_time * 1000
    # 墙钟 / 进度条 / 弹窗巡检并行；任一失败（含页面关闭）立即收尾
    timeout_task = asyncio.create_task(page.wait_for_timeout(learning_ms))
    timer_task = asyncio.create_task(
        timer(
            timing_plan.learning_wait_time,
            description="视频学习进度",
        )
    )
    popup_check_task = asyncio.create_task(
        check_rating_popup_periodically(page, timing_plan.learning_wait_time)
    )
    try:
        await asyncio.gather(timeout_task, timer_task, popup_check_task)
    finally:
        await _cleanup_background_tasks(timeout_task, timer_task, popup_check_task)

    logging.info("课程学习完毕, 确认课程进度同步状态...")
    await wait_until_learned(
        box,
        page,
        max_wait=timing_plan.sync_wait_time,
        poll_interval=timing_plan.sync_poll_interval or 1,
        on_tick=lambda: check_and_handle_rating_popup(page),
    )


async def handle_document(page, box):
    """处理文档、网页类型课程"""
    await page.locator("[class*='fullScreen-content']").first.wait_for()
    await timer(DOCUMENT_INITIAL_WAIT, description="文档学习进度")

    logging.info("课程学习完毕, 确认课程进度同步状态...")
    await wait_until_learned(
        box,
        page,
        max_wait=DOCUMENT_SYNC_EXTRA_WAIT,
        poll_interval=DOCUMENT_SYNC_POLL_INTERVAL,
    )


async def handle_h5(page, learn_item=None):
    """处理h5类型课程"""
    logging.info("h5课程类型, 记录为需要人工处理")
    if learn_item is not None:
        failure_url = await get_course_url(learn_item)
    else:
        failure_url = page.url
    record_learning_failure(
        failure_url,
        reason="h5_manual_required",
        reason_text="H5 课程类型需要人工处理",
        detail={"source": "course_chapter"},
    )
