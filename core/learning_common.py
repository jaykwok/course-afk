from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass

from core.config import (
    VIDEO_PROGRESS_LONG_INTERVAL,
    VIDEO_PROGRESS_MEDIUM_INTERVAL,
    VIDEO_PROGRESS_MEDIUM_THRESHOLD,
    VIDEO_PROGRESS_SHORT_INTERVAL,
    VIDEO_PROGRESS_SHORT_THRESHOLD,
    VIDEO_SYNC_EXTRA_WAIT,
    ZHIXUEYUN_COURSE_PREFIX,
    ZHIXUEYUN_EXAM_PREFIX,
)


@dataclass(frozen=True)
class VideoTimingPlan:
    learning_wait_time: int
    learning_fallback_interval: int
    sync_wait_time: int
    sync_poll_interval: int
    total_time: int


async def check_permission(frame):
    """检查是否有权限查看资源"""
    try:
        text_content = await frame.content()
        return not (
            "您没有权限查看该资源" in text_content
            or "该资源已不存在" in text_content
            or "该资源已下架" in text_content
        )
    except Exception as exc:
        logging.error(f"检查frame时出错: {exc}")
        return False


def is_learned(text: str) -> bool:
    """判断课程是否已学习"""
    return re.search(r"需学|需再学", text) is None


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


def get_video_status_interval(duration: int) -> int:
    """根据视频时长选择同步轮询与 fallback 日志的间隔。"""
    duration = max(1, math.ceil(duration))

    if duration <= VIDEO_PROGRESS_SHORT_THRESHOLD:
        return VIDEO_PROGRESS_SHORT_INTERVAL
    if duration <= VIDEO_PROGRESS_MEDIUM_THRESHOLD:
        return VIDEO_PROGRESS_MEDIUM_INTERVAL
    return VIDEO_PROGRESS_LONG_INTERVAL


def build_video_timing_plan(text: str) -> VideoTimingPlan:
    """根据剩余学习时长生成视频学习与同步确认的时序计划。"""
    learning_wait_time, total_time = calculate_remaining_time(text)
    sync_wait_time = calculate_video_sync_wait_time(learning_wait_time, total_time)
    return VideoTimingPlan(
        learning_wait_time=learning_wait_time,
        learning_fallback_interval=get_video_status_interval(learning_wait_time),
        sync_wait_time=sync_wait_time,
        sync_poll_interval=(
            get_video_status_interval(sync_wait_time) if sync_wait_time > 0 else 0
        ),
        total_time=total_time,
    )


async def timer(
    duration: int,
    fallback_interval: int = 30,
    description: str = "学习进度",
):
    """等待指定时长；Rich 进度不可用时按 fallback 间隔输出日志。"""
    duration = math.ceil(duration)
    if duration <= 0:
        return
    fallback_interval = max(1, math.ceil(fallback_interval))
    logging.info(f"开始时间: {time.ctime()}")
    try:
        from core.ui import wait_with_progress

        await wait_with_progress(duration, description=description)
    except Exception:
        for elapsed in range(0, duration, fallback_interval):
            wait_seconds = min(fallback_interval, duration - elapsed)
            await asyncio.sleep(wait_seconds)
            logging.info(f"已学习 {elapsed + wait_seconds} / {duration} (秒)")
    logging.info(f"结束时间: {time.ctime()}")


async def get_course_url(learn_item, section_type="course"):
    """根据学习项构造课程或考试URL"""
    course_id = await learn_item.get_attribute("data-resource-id")
    if section_type == "exam":
        prefix = ZHIXUEYUN_EXAM_PREFIX
    else:
        prefix = ZHIXUEYUN_COURSE_PREFIX
    return str(prefix + course_id)
