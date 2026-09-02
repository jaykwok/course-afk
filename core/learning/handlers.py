from __future__ import annotations

import asyncio
import json
import logging

from core.abort import VideoPlayerNotReadyError
from core.browser.session import is_target_closed_exception
from core.config import (
    DOCUMENT_POLL_INTERVAL,
    DOCUMENT_WAIT,
    VIDEO_STALL_MAX_EXTRA_WAIT,
    VIDEO_WATCH_OVERSHOOT_MAX,
    VIDEO_WATCH_OVERSHOOT_MIN,
    VIDEO_WATCH_SLICE_MAX,
    VIDEO_WATCH_SLICE_MIN,
)
from core.humanize import jitter, next_slice, sample_between
from core.learning.common import (
    build_video_timing_plan,
    get_course_url,
    is_course_section_focused,
    is_learned,
    read_section_progress_text,
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


_VIDEO_PLAYER_STATE_SCRIPT = r"""
() => {
  const isVisible = (element) => {
    if (!element) return false;
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" &&
      Number(style.opacity || 1) !== 0 && rect.width > 0 && rect.height > 0;
  };
  const visibleCount = (selector) =>
    Array.from(document.querySelectorAll(selector)).filter(isVisible).length;
  const videos = Array.from(document.querySelectorAll("video")).map((video) => ({
    visible: isVisible(video),
    readyState: Number(video.readyState || 0),
    networkState: Number(video.networkState || 0),
    paused: Boolean(video.paused),
    duration: Number.isFinite(video.duration) ? Number(video.duration) : null,
    currentTime: Number.isFinite(video.currentTime) ? Number(video.currentTime) : null,
    errorCode: video.error ? Number(video.error.code || 0) : null,
    sourceKind: String(video.currentSrc || "").startsWith("blob:") ? "blob" :
      (video.currentSrc ? "url" : "none"),
  }));
  const selectors = {
    progress: visibleCount(".vjs-progress-control"),
    duration: visibleCount(".vjs-duration-display"),
    video: visibleCount("video"),
    player: visibleCount(".video-js, [class*='video-player']"),
  };
  const mediaReady = videos.some((video) =>
    video.visible && video.errorCode === null && video.readyState >= 1 &&
      video.duration !== null && video.duration > 0
  );
  return {
    ready: mediaReady || (selectors.progress > 0 && selectors.duration > 0),
    selectors,
    videos,
  };
}
"""


async def _read_video_player_state(page) -> dict[str, object]:
    try:
        state = await page.evaluate(_VIDEO_PLAYER_STATE_SCRIPT)
        if isinstance(state, dict):
            return state
        return {"ready": False, "probe_error": "播放器状态探针返回非对象"}
    except Exception as exc:
        if is_target_closed_exception(exc):
            raise
        return {
            "ready": False,
            "probe_error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }


async def _describe_video_target(box) -> dict[str, object]:
    detail: dict[str, object] = {}
    for attribute, key in (
        ("id", "id"),
        ("class", "class"),
        ("data-sectiontype", "section_type"),
    ):
        try:
            detail[key] = await box.get_attribute(attribute)
        except Exception:
            detail[key] = None
    try:
        text = await box.locator(".section-item-wrapper").inner_text(timeout=2000)
        detail["text"] = " ".join((text or "").split())[:200]
    except Exception:
        pass
    return detail


async def _wait_for_video_player_ready(
    page,
    *,
    box,
    timeout_ms: int = 15000,
    poll_interval_ms: int = 500,
) -> dict[str, object]:
    timeout_ms = max(0, int(timeout_ms))
    poll_interval_ms = max(100, int(poll_interval_ms))
    elapsed = 0
    last_state: dict[str, object] = {"ready": False}
    while True:
        target_focused = await is_course_section_focused(box)
        last_state = await _read_video_player_state(page)
        last_state["target_focused"] = target_focused
        if target_focused and bool(last_state.get("ready")):
            return last_state
        if elapsed >= timeout_ms:
            return last_state
        wait_ms = min(poll_interval_ms, timeout_ms - elapsed)
        await page.wait_for_timeout(wait_ms)
        elapsed += wait_ms


async def _reclick_video_target(page, box) -> None:
    """重试时只重新点击当前目标章节，不猜测全局 active 元素。"""
    wrapper = box.locator(".section-item-wrapper")
    try:
        await wrapper.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    try:
        await wrapper.click(timeout=3000)
    except Exception:
        await wrapper.click(timeout=3000, force=True)
    await page.wait_for_timeout(500)


async def _ensure_video_player_ready(
    page,
    *,
    box,
    attempts: int = 3,
    wait_timeout_ms: int = 15000,
) -> None:
    """等待目标视频真正就绪；关闭遮罩，并绑定目标章节进行重试。"""
    last_state: dict[str, object] = {"ready": False}

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

            last_state = await _wait_for_video_player_ready(
                page,
                box=box,
                timeout_ms=wait_timeout_ms,
            )
            if bool(last_state.get("ready")) and bool(
                last_state.get("target_focused")
            ):
                return
        except Exception as exc:
            if is_target_closed_exception(exc):
                raise
            last_state = {
                "ready": False,
                "probe_error": f"{type(exc).__name__}: {str(exc)[:300]}",
            }

        logging.info(
            "视频播放器未就绪 (第%s/%s次): %s",
            attempt,
            attempts,
            json.dumps(last_state, ensure_ascii=False, sort_keys=True),
        )
        if attempt < attempts:
            try:
                await page.wait_for_timeout(800)
                await _reclick_video_target(page, box)
            except Exception as exc:
                if is_target_closed_exception(exc):
                    raise
                logging.debug(f"重新点击目标视频章节失败: {exc}")

    detail = {
        "attempts": max(1, int(attempts)),
        "target": await _describe_video_target(box),
        "player_state": last_state,
    }
    logging.error(
        "视频播放器最终未就绪，诊断=%s",
        json.dumps(detail, ensure_ascii=False, sort_keys=True),
    )
    raise VideoPlayerNotReadyError(detail=detail)


_RESUME_VIDEO_SCRIPT = r"""
() => {
  const video = Array.from(document.querySelectorAll("video")).find(
    (item) =>
      Number(item.readyState || 0) >= 1 &&
      Number.isFinite(item.duration) &&
      item.duration > 0
  );
  if (!video || video.ended || !video.paused) return false;
  const played = video.play();
  if (played && typeof played.catch === "function") played.catch(() => {});
  return true;
}
"""

# 播放位置与墙钟的允许偏差（秒）：小于此值视为正常抖动，不顺延
_VIDEO_LAG_TOLERANCE = 3.0
# 距片尾多近算「已播完」（秒）
_VIDEO_END_TOLERANCE = 1.5


def _pick_active_video(state: dict[str, object]) -> dict[str, object] | None:
    """从播放器探针结果里挑当前在播的那个 video。"""
    videos = state.get("videos")
    if not isinstance(videos, list):
        return None
    candidates = [item for item in videos if isinstance(item, dict)]
    for video in candidates:
        if video.get("visible") and video.get("duration"):
            return video
    return candidates[0] if candidates else None


def _as_seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


async def _resume_stalled_video(page) -> bool:
    """视频被暂停时点回播放；已播完或本就在播则不动。"""
    try:
        return bool(await page.evaluate(_RESUME_VIDEO_SCRIPT))
    except Exception as exc:
        if is_target_closed_exception(exc):
            raise
        logging.debug(f"尝试恢复播放失败: {exc}")
        return False


async def _watch_video_playback(page, box, *, watch_seconds: int) -> None:
    """分段随机等待，期间盯播放位置与章节进度。

    相比「一次 wait_for_timeout 死等满时长」：
    - 等待被切成随机长度的小段，不再是一个精确到秒的整块空窗；
    - 每段结束核对 currentTime：缓冲或被弹窗暂停导致的落后按实际差额顺延，
      不会出现「墙钟到点、视频只放了一半」然后卡在同步确认；
    - 章节进度提前变成已学时立即离开，不再空等剩下的时间。

    顺延累计上限 ``VIDEO_STALL_MAX_EXTRA_WAIT``，避免播放器彻底坏掉时无限等。
    """
    watch_seconds = max(0, int(watch_seconds))
    if watch_seconds <= 0:
        return

    loop = asyncio.get_running_loop()
    deadline = loop.time() + watch_seconds
    extended_total = 0.0
    stall_cap_logged = False
    media_ended = False
    last_media_time: float | None = None

    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return

        slice_seconds = next_slice(
            remaining,
            min_slice=VIDEO_WATCH_SLICE_MIN,
            max_slice=VIDEO_WATCH_SLICE_MAX,
        )
        # 用 page.wait_for_timeout 而非 asyncio.sleep：页面/整窗被关时会直接抛，
        # 由上层的 TargetClosed 路径接管（保存剩余链接并停止）。
        await page.wait_for_timeout(int(slice_seconds * 1000))

        if is_learned(await read_section_progress_text(box)):
            logging.info(
                "章节进度已提前标记为已学, 剩余 %s 秒不再空等",
                max(0, round(deadline - loop.time())),
            )
            return

        video = _pick_active_video(await _read_video_player_state(page))
        if video is None:
            continue

        media_time = _as_seconds(video.get("currentTime"))
        duration = _as_seconds(video.get("duration"))
        paused = bool(video.get("paused"))

        if (
            media_time is not None
            and duration is not None
            and duration > 0
            and media_time >= duration - _VIDEO_END_TOLERANCE
        ):
            # 已播到片尾就不可能再推进了：停止顺延，但原计划时长照等
            # （剩余时长被向上取整到整分钟，片尾之后仍可能差几十秒才同步）。
            if not media_ended:
                media_ended = True
                logging.info("视频已播放至片尾, 继续等待进度同步")
            last_media_time = media_time
            continue

        advanced = (
            media_time - last_media_time
            if media_time is not None and last_media_time is not None
            else None
        )
        last_media_time = media_time

        if paused:
            # 互动练习等弹窗会暂停播放：先处理弹窗再点回播放
            await check_and_handle_rating_popup(page)
            if await _resume_stalled_video(page):
                logging.info("检测到视频暂停, 已恢复播放")

        if advanced is None:
            lag = slice_seconds if paused else 0.0
        else:
            lag = max(0.0, slice_seconds - advanced)
        if lag <= _VIDEO_LAG_TOLERANCE:
            continue

        extra = min(lag, max(0.0, VIDEO_STALL_MAX_EXTRA_WAIT - extended_total))
        if extra <= 0:
            # 只是不再顺延，原计划时长仍要等满
            if not stall_cap_logged:
                stall_cap_logged = True
                logging.warning(
                    "视频播放持续停滞, 顺延已达上限 %s 秒, 后续不再顺延",
                    VIDEO_STALL_MAX_EXTRA_WAIT,
                )
            continue
        deadline += extra
        extended_total += extra
        logging.info(
            "视频播放落后墙钟 %.0f 秒(暂停/缓冲), 本轮顺延 %.0f 秒, 累计 %.0f 秒",
            lag,
            extra,
            extended_total,
        )


async def handle_video(box, page):
    """处理视频类型课程"""
    await _ensure_video_player_ready(page, box=box)

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

    # 剩余时长被向上取整到整分钟，直接照做等于每节都卡整分钟离开；加一段随机
    # 余量，顺带补偿平台按秒结算时的边界。
    watch_seconds = timing_plan.learning_wait_time
    if watch_seconds > 0:
        watch_seconds += round(
            sample_between(VIDEO_WATCH_OVERSHOOT_MIN, VIDEO_WATCH_OVERSHOOT_MAX)
        )
        logging.info(f"本节计划观看: {watch_seconds} 秒(含随机余量)")

    # 观看巡检 / 进度条 / 弹窗巡检并行；以巡检任务为准：它提前结束（进度已同步）
    # 或抛错（页面关闭）时，另外两个后台任务立即取消。
    watch_task = asyncio.create_task(
        _watch_video_playback(page, box, watch_seconds=watch_seconds)
    )
    timer_task = asyncio.create_task(
        timer(
            watch_seconds,
            description="视频学习进度",
        )
    )
    popup_check_task = asyncio.create_task(
        check_rating_popup_periodically(page, watch_seconds)
    )
    try:
        await watch_task
    finally:
        await _cleanup_background_tasks(watch_task, timer_task, popup_check_task)

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

    # 每篇文档都恰好停留 60 秒是固定节拍；抖开上限，轮询间隔由
    # wait_until_learned 内部再抖一次
    max_wait = max(0, round(jitter(DOCUMENT_WAIT, ratio=0.3)))
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
