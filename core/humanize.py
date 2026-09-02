"""挂课节奏随机化工具。

固定节拍是服务端最容易统计出来的自动化特征：每个动作恰好隔 3 秒、
每 30 秒一次弹窗巡检、每节课恰好等满整分钟——这些在后台日志里都是直线。
本模块集中提供带抖动的时长计算，调用方只需描述「大概等多久」。

用普通 ``random`` 即可（不是安全用途），所有函数保证返回值非负。
"""

from __future__ import annotations

import random


def jitter(
    base: float,
    *,
    ratio: float = 0.25,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> float:
    """在 ``base`` 上下按 ``ratio`` 比例抖动，返回同单位的浮点数。

    ``base <= 0`` 时直接返回 0（不给「本来就不用等」的场景硬塞等待）。
    """
    base = max(0.0, float(base))
    if base <= 0:
        return 0.0
    spread = base * max(0.0, float(ratio))
    value = random.uniform(base - spread, base + spread)
    value = max(float(minimum), value)
    if maximum is not None:
        value = min(float(maximum), value)
    return max(0.0, value)


def sample_between(low: float, high: float) -> float:
    """在闭区间内均匀取样；入参顺序写反也能用。"""
    low, high = sorted((max(0.0, float(low)), max(0.0, float(high))))
    return random.uniform(low, high)


def sample_int_between(low: int, high: int) -> int:
    """整数版 :func:`sample_between`，用于毫秒等整型参数。"""
    low, high = sorted((max(0, int(low)), max(0, int(high))))
    return random.randint(low, high)


async def pause_between(page, low: float, high: float) -> float:
    """在页面上停顿一段随机时长（秒），返回实际停顿秒数。

    刻意用 ``page.wait_for_timeout`` 而不是 ``asyncio.sleep``：页面或整窗被关时
    它会直接抛错，交给上层的 TargetClosed 路径保存进度并收尾。
    """
    seconds = sample_between(low, high)
    if seconds > 0:
        await page.wait_for_timeout(int(seconds * 1000))
    return seconds


def next_slice(remaining: float, *, min_slice: float, max_slice: float) -> float:
    """把 ``remaining`` 切出一段随机长度的等待。

    收尾规则：剩余不足一段上限时一次等完，避免切出几秒的碎尾巴，
    反而在日志里制造出「一串极短轮询」的新特征。
    """
    remaining = max(0.0, float(remaining))
    if remaining <= 0:
        return 0.0
    low, high = sorted((float(min_slice), float(max_slice)))
    low = max(0.1, low)
    high = max(low, high)
    if remaining <= high:
        return remaining
    piece = random.uniform(low, high)
    if remaining - piece < low:
        return remaining
    return piece
