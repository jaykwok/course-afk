from __future__ import annotations

import re


def extract_attempt_limit_message(text: str) -> str | None:
    """从页面文本中提取考试次数限制提示。"""
    for raw_line in str(text or "").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if line and "考试次数限制" in line:
            return line
    return None


def parse_remaining_attempts(text: str) -> int | None:
    """解析“剩余 N 次”，兼容数字两侧存在空白或换行。"""
    match = re.search(r"剩余\s*(\d+)\s*次", str(text or ""))
    if not match:
        return None
    return int(match.group(1))
