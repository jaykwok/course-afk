from __future__ import annotations

import asyncio
import logging

from core.config import (
    DOCUMENT_POLL_INTERVAL,
    DOCUMENT_WAIT,
)
from core.learning.common import (
    build_video_timing_plan,
    get_course_url,
    is_learned,
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


async def _ensure_video_player_ready(page, *, attempts: int = 3) -> None:
    """等待视频控件就绪；被遮罩/评分弹窗挡住时关弹窗并重试点击续播。"""
    progress = page.locator(".vjs-progress-control").first
    duration = page.locator(".vjs-duration-display")
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            resume_buttons = await page.locator(".register-mask-layer").all()
            if resume_buttons:
                try:
                    await resume_buttons[0].click(timeout=3000)
                except Exception as exc:
                    logging.debug(f"点击续播遮罩失败 (第{attempt}次): {exc}")

            await check_and_handle_rating_popup(page)
            # 关顶层遮罩，避免控件在 DOM 中但不可见
            try:
                from core.browser.overlays import dismiss_topmost_overlays_async

                await dismiss_topmost_overlays_async(page, max_count=2)
            except Exception:
                pass

            await progress.wait_for(state="visible", timeout=15000)
            await duration.wait_for(state="visible", timeout=10000)
            return
        except Exception as exc:
            last_error = exc
            logging.info(
                f"视频播放器未就绪 (第{attempt}/{attempts}次): {exc}"
            )
            try:
                await page.wait_for_timeout(800)
            except Exception:
                pass
            # 有时章节未真正点开，回退再点当前章节区域
            try:
                await page.locator(".section-item-wrapper.active, .section-item.active").first.click(
                    timeout=2000, force=True
                )
            except Exception:
                pass

    assert last_error is not None
    raise last_error


async def handle_video(box, page):
    """处理视频类型课程"""
    await _ensure_video_player_ready(page)

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
    """处理文档、网页类型课程。

    统一挂 DOCUMENT_WAIT 秒：期间轮询进度，提前同步则提前走；
    到点无论是否同步都离开，不抛同步超时、不记失败（A/B 实勘：
    额外同步窗/滚动均推不动卡在「需学 00:05」的文档）。
    """
    from core.abort import SyncTimeoutError

    content = page.locator("[class*='fullScreen-content']").first
    try:
        await content.wait_for(state="visible", timeout=15000)
    except Exception:
        # 文档区加载慢或被弹窗挡住：关遮罩后短等再试一次
        try:
            from core.browser.overlays import dismiss_topmost_overlays_async

            await dismiss_topmost_overlays_async(page, max_count=2)
        except Exception:
            pass
        await page.wait_for_timeout(800)
        await content.wait_for(state="visible", timeout=15000)

    max_wait = max(0, int(DOCUMENT_WAIT))
    poll = max(1, int(DOCUMENT_POLL_INTERVAL)) if max_wait > 0 else 1
    logging.info(f"文档挂机最多 {max_wait} 秒（提前同步则提前离开）")

    # 与视频一致：用进度条展示剩余挂时；同步检测在 wait_until_learned 内完成
    # 这里拆成「可提前结束」：不先死等满 timer，只在轮询窗内检测。
    try:
        await wait_until_learned(
            box,
            page,
            max_wait=max_wait,
            poll_interval=poll,
        )
        logging.info("文档进度已同步，离开本节")
    except SyncTimeoutError:
        # 到点走人：广目类文档可能长期卡在需学 00:05，不阻断后续章节
        try:
            text = await box.locator(".section-item-wrapper").inner_text(timeout=3000)
        except Exception:
            text = ""
        if is_learned(text):
            logging.info("文档进度已同步，离开本节")
        else:
            logging.info(
                f"文档挂机 {max_wait} 秒结束，进度仍未同步，继续下一节"
                f" (文案: {(text or '').replace(chr(10), ' ')[:80]})"
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
