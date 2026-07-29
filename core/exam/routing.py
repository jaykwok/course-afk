from __future__ import annotations

import logging
from pathlib import Path

from core.config import EXAM_URLS_FILE, MANUAL_EXAM_FILE
from core.exam.rules import extract_attempt_limit_message, parse_remaining_attempts
from core.queues.exam import append_exam_url, remove_exam_url
from core.queues.manual_exam import append_manual_exam_entry


def queue_exam_url_by_attempt_text(
    url: str,
    attempt_text: str,
    *,
    threshold: int,
    exam_file: Path | None = None,
    manual_exam_file: Path | None = None,
) -> str:
    """按页面已知的考试次数将链接写入 AI 或人工考试队列。

    返回 ``ai``、``manual`` 或 ``skipped``。只有页面明确出现次数信号时才
    做保守分流；URL 本身不携带剩余次数，不能据此猜测。
    """
    normalized_url = str(url or "").strip()
    if not normalized_url:
        return "skipped"

    exam_file = exam_file or EXAM_URLS_FILE
    manual_exam_file = manual_exam_file or MANUAL_EXAM_FILE
    normalized_text = str(attempt_text or "").strip()
    attempt_limit_message = extract_attempt_limit_message(normalized_text)

    if attempt_limit_message:
        append_manual_exam_entry(
            normalized_url,
            reason="attempt_limit",
            reason_text=attempt_limit_message,
            remaining_attempts=0,
            threshold=threshold,
            file_path=manual_exam_file,
        )
        remove_exam_url(normalized_url, file_path=exam_file)
        logging.info(f"{attempt_limit_message}，已转为人工考试: {normalized_url}")
        return "manual"

    if "剩余" not in normalized_text:
        append_exam_url(normalized_url, file_path=exam_file)
        return "ai"

    remaining = parse_remaining_attempts(normalized_text)
    if remaining is None:
        reason_text = "页面显示剩余次数但无法解析，转为人工考试处理"
        append_manual_exam_entry(
            normalized_url,
            reason="attempt_unknown",
            reason_text=reason_text,
            threshold=threshold,
            file_path=manual_exam_file,
        )
        remove_exam_url(normalized_url, file_path=exam_file)
        logging.info(f"{reason_text}: {normalized_url}")
        return "manual"

    if remaining <= threshold:
        reason_text = (
            f"当前考试剩余次数为 {remaining}, 小于等于 {threshold} 次, 转为人工考试"
        )
        append_manual_exam_entry(
            normalized_url,
            reason="attempt_threshold",
            reason_text=reason_text,
            remaining_attempts=remaining,
            threshold=threshold,
            file_path=manual_exam_file,
        )
        remove_exam_url(normalized_url, file_path=exam_file)
        logging.info(f"{reason_text}: {normalized_url}")
        return "manual"

    append_exam_url(normalized_url, file_path=exam_file)
    logging.info(
        f"当前考试剩余次数为 {remaining}, 大于 {threshold} 次, 保留在 AI 考试队列"
    )
    return "ai"
