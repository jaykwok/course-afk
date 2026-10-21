from __future__ import annotations

import logging
from pathlib import Path

from core.config import EXAM_URLS_FILE, MANUAL_EXAM_FILE
from core.exam.rules import extract_attempt_limit_message, parse_remaining_attempts, explicitly_unlimited
from core.queues.exam import append_exam_urls, remove_exam_url
from core.queues.manual_exam import append_manual_exam_entry, read_manual_exam_queue, write_manual_exam_queue
from core.storage import serialized


def allows_auto_submit(attempt_text: str, threshold: int) -> bool:
    """次数只限制自动交卷；至少保留最后一次给用户确认。"""
    if extract_attempt_limit_message(attempt_text):
        return False
    if explicitly_unlimited(attempt_text):
        return True
    remaining = parse_remaining_attempts(attempt_text)
    return remaining is not None and remaining > max(1, threshold)


@serialized
def requeue_ai_exam_reviews(*, exam_file: Path | None = None, manual_exam_file: Path | None = None) -> int:
    """旧待办统一恢复至考试入口，目标落盘成功后才移除源记录。"""
    exam_file = exam_file or EXAM_URLS_FILE
    manual_exam_file = manual_exam_file or MANUAL_EXAM_FILE
    entries = read_manual_exam_queue(manual_exam_file)
    retry_reasons = {"attempt_threshold", "attempt_unknown", "ai_no_answer", "fill_blank", "question_parse_error", "submission_unverified", "ai_exam_error", "state_unknown", "ai_failed", "pending_grading", "attempt_limit"}
    retry = [entry for entry in entries if entry.reason in retry_reasons]
    if not retry:
        return 0
    append_exam_urls([entry.url for entry in retry], file_path=exam_file, requires_manual_submit=True)
    write_manual_exam_queue([entry for entry in entries if entry not in retry], file_path=manual_exam_file, keep_file=True)
    return len(retry)


def queue_exam_url_by_attempt_text(
    url: str,
    attempt_text: str,
    *,
    threshold: int,
    exam_file: Path | None = None,
    manual_exam_file: Path | None = None,
) -> str:
    """统一写入 AI 队列，次数只决定能否自动交卷。

    返回 ``ai``、``manual`` 或 ``skipped``。仅平台明确禁止进入时留作复查。
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
        logging.info(f"{attempt_limit_message}，已保留考试复查: {normalized_url}")
        return "manual"

    manual_submit = not allows_auto_submit(normalized_text, threshold)
    append_exam_urls([normalized_url], file_path=exam_file, requires_manual_submit=manual_submit)
    logging.info("已加入 AI 考试队列%s: %s", "（仅人工交卷）" if manual_submit else "", normalized_url)
    return "ai"
