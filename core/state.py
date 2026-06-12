from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.config import (
    EXAM_URLS_FILE,
    LEARNING_FAILURES_FILE,
    LEARNING_URLS_FILE,
    MANUAL_EXAM_FILE,
)
from core.credential import (
    load_credential_metadata,
    parse_expires_at,
    parse_saved_at,
    is_credential_expired_at,
    is_credential_expired,
)
from core.exam_queue import count_exam_urls, read_exam_urls
from core.file_ops import read_unique_lines
from core.learning_queue import (
    count_learning_failures,
    count_learning_urls,
    read_learning_urls,
)
from core.manual_exam_queue import count_manual_exam_urls, read_manual_exam_urls


@dataclass
class ProjectState:
    has_credential: bool
    credential_expired: bool
    learning_count: int
    learning_failure_count: int
    exam_count: int
    manual_exam_count: int


def read_non_empty_lines(file_path: Path) -> list[str]:
    if file_path == EXAM_URLS_FILE:
        return read_exam_urls(file_path)
    if file_path == MANUAL_EXAM_FILE:
        return read_manual_exam_urls(file_path)
    if file_path == LEARNING_URLS_FILE:
        return read_learning_urls(file_path)
    return read_unique_lines(file_path)


def count_non_empty_lines(file_path: Path) -> int:
    return len(read_non_empty_lines(file_path))


def has_valid_credential() -> tuple[bool, bool]:
    metadata = load_credential_metadata()
    saved_at = parse_saved_at(metadata)
    if not metadata or saved_at is None:
        return False, True
    expires_at = parse_expires_at(metadata)
    if expires_at is not None:
        return True, is_credential_expired_at(expires_at)
    return True, is_credential_expired(saved_at)


def collect_project_state() -> ProjectState:
    has_credential, credential_expired = has_valid_credential()
    return ProjectState(
        has_credential=has_credential,
        credential_expired=credential_expired,
        learning_count=count_learning_urls(LEARNING_URLS_FILE),
        learning_failure_count=count_learning_failures(LEARNING_FAILURES_FILE),
        exam_count=count_exam_urls(EXAM_URLS_FILE),
        manual_exam_count=count_manual_exam_urls(MANUAL_EXAM_FILE),
    )


def recommend_next_step(
    *,
    has_credential: bool,
    learning_count: int,
    exam_count: int,
    manual_exam_count: int,
) -> str:
    if not has_credential:
        return "切换账号 / 更新登录凭证"
    if exam_count > 0:
        return "AI 自动考试"
    if manual_exam_count > 0:
        return "人工考试"
    if learning_count == 0:
        return "手动选择课程 / 录入课程或考试链接"
    return "仅挂课"
