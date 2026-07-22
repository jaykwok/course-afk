from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from core.config import (
    VIDEO_SYNC_EXTRA_WAIT,
    VIDEO_SYNC_MIN_WAIT,
    VIDEO_SYNC_POLL_INTERVAL,
    ZHIXUEYUN_COURSE_PREFIX,
    ZHIXUEYUN_EXAM_PREFIX,
)


@dataclass(frozen=True)
class VideoTimingPlan:
    learning_wait_time: int
    sync_wait_time: int
    sync_poll_interval: int
    total_time: int


# (页面文案片段, failure.reason, 写入失败链接的说明)
_ACCESS_DENIAL_MARKERS: tuple[tuple[str, str, str], ...] = (
    (
        "该资源已不存在",
        "resource_gone",
        "该资源已不存在，已从课程链接清理",
    ),
    (
        "该资源已下架",
        "resource_delisted",
        "该资源已下架，已从课程链接清理",
    ),
    (
        "您没有权限查看该资源",
        "no_permission",
        "无权限访问该学习资源，已从课程链接清理",
    ),
)


def match_access_denial(text: str) -> tuple[str, str] | None:
    """根据页面文案识别不可访问原因。返回 (reason, reason_text) 或 None。"""
    content = text or ""
    for needle, reason, reason_text in _ACCESS_DENIAL_MARKERS:
        if needle in content:
            return reason, reason_text
    return None


async def detect_access_denial(frame) -> tuple[str, str] | None:
    """检查 frame 是否不可访问。返回 (reason, reason_text) 或 None。"""
    try:
        text_content = await frame.content()
    except Exception as exc:
        logging.error(f"检查frame时出错: {exc}")
        return (
            "no_permission",
            f"检查资源访问权限失败，已从课程链接清理: {exc}",
        )
    return match_access_denial(text_content)


async def check_permission(frame) -> bool:
    """检查是否有权限查看资源（True=可访问）。"""
    return await detect_access_denial(frame) is None


async def ensure_resource_accessible(frame) -> None:
    """不可访问则抛 NoPermissionError（带 reason / reason_text）。"""
    from core.abort import NoPermissionError

    denial = await detect_access_denial(frame)
    if denial is None:
        return
    reason, reason_text = denial
    raise NoPermissionError(reason_text, reason=reason, reason_text=reason_text)


# 平台整页限流（非弹窗），实勘文案见 tools/capture/concurrent_study_probe.json
_CONCURRENT_STUDY_MARKERS: tuple[str, ...] = (
    "您已打开新的课程详情页",
    "已打开新的课程详情页",
)


def match_concurrent_study_limit(text: str) -> str | None:
    """识别并发学习限流整页提示。命中则返回说明文案。"""
    content = text or ""
    for needle in _CONCURRENT_STUDY_MARKERS:
        if needle in content:
            return "平台并发学习限流（整页提示，非弹窗）: 已打开新的课程"
    return None


async def detect_concurrent_study_limit(frame_or_page) -> str | None:
    """从 frame/page 内容识别并发限流。"""
    try:
        # Page 与 Frame 均有 content()
        text_content = await frame_or_page.content()
    except Exception as exc:
        logging.debug(f"检测并发限流时读 content 失败: {exc}")
        return None
    return match_concurrent_study_limit(text_content)


async def ensure_no_concurrent_study_limit(frame_or_page) -> None:
    """命中并发限流则抛 ConcurrentStudyLimitError（保留队列待重试）。"""
    from core.abort import ConcurrentStudyLimitError

    message = await detect_concurrent_study_limit(frame_or_page)
    if message:
        raise ConcurrentStudyLimitError(message, reason_text=message)


async def ensure_course_page_ready(page) -> None:
    """课页可学前置：访问权限 + 非并发限流页。须在等进度条/章节前调用。"""
    frame = getattr(page, "main_frame", page)
    await ensure_resource_accessible(frame)
    await ensure_no_concurrent_study_limit(frame)


def is_learned(text: str) -> bool:
    """判断章节是否已学完。

    实勘：已学/未学 class 相同；未学文案含「需学」「需再学」。
    空文案不能当已学（避免 DOM 未渲染时误跳过）。
    """
    if not (text or "").strip():
        return False
    return re.search(r"需学|需再学", text) is None


async def wait_until_learned(
    box,
    page,
    *,
    max_wait: int,
    poll_interval: int,
    on_tick: Callable[[], Awaitable[Any]] | None = None,
) -> None:
    """轮询章节进度直至 is_learned 或超时抛 SyncTimeoutError。

    进入本函数前若已学完会立即返回。``max_wait`` / ``poll_interval`` 单位秒。
    """
    from core.abort import SyncTimeoutError

    max_wait = max(0, int(max_wait))
    poll_interval = max(1, int(poll_interval)) if max_wait > 0 else 0

    current_text = await box.locator(".section-item-wrapper").inner_text()
    if is_learned(current_text):
        logging.info("课程进度已同步到服务器")
        return

    if max_wait <= 0:
        raise SyncTimeoutError(
            "课程进度未能同步完成",
            reason_text="课程进度同步超时（无额外等待窗），后续可重新加入课程链接",
        )

    elapsed_sync_wait = 0
    while elapsed_sync_wait < max_wait:
        wait_seconds = min(poll_interval, max_wait - elapsed_sync_wait)
        await page.wait_for_timeout(wait_seconds * 1000)
        elapsed_sync_wait += wait_seconds
        if on_tick is not None:
            await on_tick()
        current_text = await box.locator(".section-item-wrapper").inner_text()
        if is_learned(current_text):
            logging.info(
                f"课程进度已同步到服务器, 额外等待 {elapsed_sync_wait} 秒"
            )
            return
        logging.info(
            f"课程进度仍未同步完成, 已额外等待 {elapsed_sync_wait} 秒, 继续等待..."
        )

    logging.info(f"超时: 已额外等待{max_wait}秒, 课程进度仍未同步")
    raise SyncTimeoutError(
        f"课程进度未能在 {max_wait} 秒内同步完成",
        reason_text=f"课程进度同步超时（已额外等待 {max_wait} 秒），后续可重新加入课程链接",
    )


def time_to_seconds(duration: str) -> int:
    """时长转换为秒数"""
    pattern = r"\d+(?::\d{1,2}){1,2}"
    match = re.search(pattern, duration)
    if not match:
        return 0

    units = match.group().split(":")
    total_seconds = sum(
        int(unit) * 60**index for index, unit in enumerate(reversed(units))
    )
    return math.ceil(total_seconds / 10) * 10


def parse_course_durations(text: str) -> tuple[int, int]:
    """从课程文本中解析总时长和剩余时长。"""
    pattern = r"(\d+(?::\d{1,2}){1,2})"
    match = re.findall(pattern, text)
    if len(match) == 1:
        total_time = remaining_time = time_to_seconds(match[0])
    elif len(match) == 2:
        total_time = time_to_seconds(match[0])
        remaining_time = time_to_seconds(match[1])
    else:
        raise Exception(f"无法解析课程时长: {text}")
    return total_time, remaining_time


def calculate_remaining_time(text) -> tuple[int, int]:
    """计算当前课程剩余挂课时间"""
    total_time, remaining_time = parse_course_durations(text)
    return min(math.ceil(remaining_time / 60) * 60, total_time), total_time


def calculate_video_sync_wait_time(remaining_time: int, total_time: int) -> int:
    """按服务端 5 分钟记录周期，推算学完后理论上还需等待多久。"""
    remaining_time = max(0, math.ceil(remaining_time))
    total_time = max(0, math.ceil(total_time))
    if remaining_time <= 0:
        return 0

    theoretical_learning_time = math.ceil(remaining_time / VIDEO_SYNC_EXTRA_WAIT) * VIDEO_SYNC_EXTRA_WAIT
    if theoretical_learning_time >= total_time:
        return 0

    return max(0, theoretical_learning_time - remaining_time)


def build_video_timing_plan(text: str) -> VideoTimingPlan:
    """根据剩余学习时长生成视频学习与同步确认的时序计划。"""
    learning_wait_time, total_time = calculate_remaining_time(text)
    sync_wait_time = calculate_video_sync_wait_time(learning_wait_time, total_time)
    # 理论同步窗为 0 时仍给最短宽限；轮询中确认已同步会提前返回
    if learning_wait_time > 0:
        sync_wait_time = max(sync_wait_time, VIDEO_SYNC_MIN_WAIT)
    return VideoTimingPlan(
        learning_wait_time=learning_wait_time,
        sync_wait_time=sync_wait_time,
        sync_poll_interval=VIDEO_SYNC_POLL_INTERVAL if sync_wait_time > 0 else 0,
        total_time=total_time,
    )


async def timer(duration: int, description: str = "学习进度"):
    """等待指定时长，通过 UI 进度条展示（TUI / CLI 的 wait_with_progress）。"""
    duration = math.ceil(duration)
    if duration <= 0:
        return
    logging.info(f"开始时间: {time.ctime()}")
    from core.ui import wait_with_progress

    await wait_with_progress(duration, description=description)
    logging.info(f"结束时间: {time.ctime()}")


async def get_course_url(learn_item, section_type="course"):
    """
    根据学习项 DOM 构造课程或考试 URL。

    考试：data-resource-id 对应试卷 UUID（与主题 chapter-progress sectionType=9
    的 resourceId 一致，写入 answer-paper；勿用主题小节 id）。
    课程：data-resource-id / 课程详情 id。
    """
    course_id = await learn_item.get_attribute("data-resource-id")
    if section_type == "exam":
        prefix = ZHIXUEYUN_EXAM_PREFIX
    else:
        prefix = ZHIXUEYUN_COURSE_PREFIX
    return str(prefix + course_id)
