from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from core.config import LEARNING_FAILURES_FILE, LEARNING_URLS_FILE
from core.file_ops import del_file, is_compliant_url_regex, normalize_text, write_text_atomic
from core.links import unique_urls


@dataclass(frozen=True)
class LearningQueueEntry:
    url: str


@dataclass(frozen=True)
class LearningFailureEntry:
    url: str
    reason: str
    reason_text: str
    detail: dict[str, object]


def _normalize_detail(value) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _normalize_queue_entries(raw_entries) -> list[LearningQueueEntry]:
    if not isinstance(raw_entries, list):
        raise ValueError("课程链接队列必须是 JSON 数组")

    entries_by_url: dict[str, LearningQueueEntry] = {}
    for raw_entry in raw_entries:
        if isinstance(raw_entry, dict):
            url = normalize_text(raw_entry.get("url"))
        else:
            url = ""
        if url:
            entries_by_url[url] = LearningQueueEntry(url=url)
    return list(entries_by_url.values())


def _serialize_queue_entries(entries: list[LearningQueueEntry]) -> list[dict[str, object]]:
    return [{"url": entry.url} for entry in entries]


def _normalize_failure_entry(raw_entry) -> LearningFailureEntry | None:
    if not isinstance(raw_entry, dict):
        return None

    url = normalize_text(raw_entry.get("url"))
    reason = normalize_text(raw_entry.get("reason"))
    reason_text = normalize_text(raw_entry.get("reason_text"))
    if not url or not reason:
        return None

    return LearningFailureEntry(
        url=url,
        reason=reason,
        reason_text=reason_text,
        detail=_normalize_detail(raw_entry.get("detail")),
    )


def _normalize_failure_entries(raw_entries) -> list[LearningFailureEntry]:
    if not isinstance(raw_entries, list):
        raise ValueError("挂课失败队列必须是 JSON 数组")

    entries_by_url: dict[str, LearningFailureEntry] = {}
    for raw_entry in raw_entries:
        entry = _normalize_failure_entry(raw_entry)
        if entry is not None:
            entries_by_url[entry.url] = entry
    return list(entries_by_url.values())


def _serialize_failure_entries(
    entries: list[LearningFailureEntry],
) -> list[dict[str, object]]:
    return [
        {
            "url": entry.url,
            "reason": entry.reason,
            "reason_text": entry.reason_text,
            "detail": entry.detail,
        }
        for entry in entries
    ]


def read_learning_queue(file_path: Path = LEARNING_URLS_FILE) -> list[LearningQueueEntry]:
    try:
        content = file_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return []

    if not content:
        return []

    try:
        raw_entries = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"课程链接队列不是有效 JSON: {exc}") from exc
    return _normalize_queue_entries(raw_entries)


def write_learning_queue(
    entries: list[LearningQueueEntry],
    *,
    file_path: Path = LEARNING_URLS_FILE,
    keep_file: bool = True,
) -> None:
    normalized = _normalize_queue_entries(_serialize_queue_entries(entries))
    if not normalized and not keep_file:
        del_file(file_path)
        return

    write_text_atomic(
        file_path,
        json.dumps(_serialize_queue_entries(normalized), ensure_ascii=False, indent=2),
    )


def append_learning_url(url: str, *, file_path: Path = LEARNING_URLS_FILE) -> bool:
    normalized_url = normalize_text(url)
    if not normalized_url:
        return False

    entries = read_learning_queue(file_path=file_path)
    if normalized_url in {entry.url for entry in entries}:
        return False

    entries.append(LearningQueueEntry(url=normalized_url))
    write_learning_queue(entries, file_path=file_path)
    return True


def append_learning_urls(
    urls: list[str],
    *,
    file_path: Path = LEARNING_URLS_FILE,
) -> list[str]:
    entries = read_learning_queue(file_path=file_path)
    existing = {entry.url for entry in entries}
    added: list[str] = []
    for url in unique_urls(urls):
        if url in existing:
            continue
        entries.append(LearningQueueEntry(url=url))
        existing.add(url)
        added.append(url)

    if added:
        write_learning_queue(entries, file_path=file_path)
    return added


def read_learning_urls(file_path: Path = LEARNING_URLS_FILE) -> list[str]:
    return [entry.url for entry in read_learning_queue(file_path=file_path)]


def count_learning_urls(file_path: Path = LEARNING_URLS_FILE) -> int:
    return len(read_learning_urls(file_path=file_path))


def write_learning_urls(
    urls: list[str],
    *,
    file_path: Path = LEARNING_URLS_FILE,
    keep_file: bool = True,
) -> None:
    entries = [LearningQueueEntry(url=url) for url in unique_urls(urls)]
    write_learning_queue(entries, file_path=file_path, keep_file=keep_file)


def read_learning_failures(
    file_path: Path = LEARNING_FAILURES_FILE,
) -> list[LearningFailureEntry]:
    try:
        content = file_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return []

    if not content:
        return []

    try:
        raw_entries = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"挂课失败队列不是有效 JSON: {exc}") from exc
    return _normalize_failure_entries(raw_entries)


def write_learning_failures(
    entries: list[LearningFailureEntry],
    *,
    file_path: Path = LEARNING_FAILURES_FILE,
    keep_file: bool = True,
) -> None:
    normalized = _normalize_failure_entries(_serialize_failure_entries(entries))
    if not normalized and not keep_file:
        del_file(file_path)
        return

    write_text_atomic(
        file_path,
        json.dumps(_serialize_failure_entries(normalized), ensure_ascii=False, indent=2),
    )


def record_learning_failure(
    url: str,
    *,
    reason: str,
    reason_text: str,
    detail: dict[str, object] | None = None,
    file_path: Path = LEARNING_FAILURES_FILE,
) -> None:
    normalized_url = normalize_text(url)
    normalized_reason = normalize_text(reason)
    if not normalized_url or not normalized_reason:
        return

    incoming = LearningFailureEntry(
        url=normalized_url,
        reason=normalized_reason,
        reason_text=normalize_text(reason_text),
        detail=_normalize_detail(detail),
    )
    entries = read_learning_failures(file_path=file_path)
    existing = {entry.url: entry for entry in entries}
    if incoming.url not in existing:
        entries.append(incoming)
    else:
        entries = [
            incoming if entry.url == incoming.url else entry
            for entry in entries
        ]
    write_learning_failures(entries, file_path=file_path)


def remove_learning_failure(
    url: str,
    *,
    file_path: Path = LEARNING_FAILURES_FILE,
    keep_file: bool = True,
) -> None:
    normalized_url = normalize_text(url)
    if not normalized_url:
        return

    entries = [
        entry
        for entry in read_learning_failures(file_path=file_path)
        if entry.url != normalized_url
    ]
    write_learning_failures(entries, file_path=file_path, keep_file=keep_file)


def count_learning_failures(file_path: Path = LEARNING_FAILURES_FILE) -> int:
    return len(read_learning_failures(file_path=file_path))


def prune_invalid_learning_failures(
    *,
    file_path: Path = LEARNING_FAILURES_FILE,
) -> list[str]:
    """
    清理失败文档中的脏数据：不合规 URL（如测试占位 .../detail/a）。

    返回被移除的 URL 列表。
    """
    entries = read_learning_failures(file_path=file_path)
    if not entries:
        return []

    kept: list[LearningFailureEntry] = []
    removed: list[str] = []
    for entry in entries:
        if is_compliant_url_regex(entry.url):
            kept.append(entry)
        else:
            removed.append(entry.url)

    if not removed:
        return []

    write_learning_failures(kept, file_path=file_path, keep_file=True)
    return removed


# 可重新入队学习队列的失败原因（无权限/不合规/待复查/需人工等不自动重试）
RETRIABLE_LEARNING_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        "retryable_error",
        "sync_timeout",
        "partial_course_failure",
        "concurrent_study_limit",
    }
)

# 展示用：reason → 中文说明（TUI 汇总分组）
LEARNING_FAILURE_REASON_LABELS: dict[str, str] = {
    "retryable_error": "可重试错误",
    "sync_timeout": "进度同步超时",
    "partial_course_failure": "部分章节失败",
    "concurrent_study_limit": "并发学习限流",
    "no_permission": "无权限",
    "resource_gone": "资源不存在",
    "resource_delisted": "资源已下架",
    "non_compliant_url": "不合规链接",
    "unknown_learning_type": "未知类型",
    "url_type_pending": "URL 待复查",
    "survey_manual_required": "调研需人工",
    "h5_manual_required": "H5需人工",
    "other_learning_type": "其它类型",
}

# 需人工处理（不自动重试）
MANUAL_LEARNING_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        "survey_manual_required",
        "h5_manual_required",
        "other_learning_type",
        "unknown_learning_type",
        "url_type_pending",
    }
)

# 不可访问已清理（不自动重试）
ACCESS_DENIED_LEARNING_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        "no_permission",
        "resource_gone",
        "resource_delisted",
        "non_compliant_url",
    }
)


def group_learning_failures_by_reason(
    file_path: Path = LEARNING_FAILURES_FILE,
) -> list[tuple[str, int, str]]:
    """
    按 reason 分组统计失败条目。

    返回 [(reason, count, label), ...]，按数量降序、reason 升序。
    """
    counts: dict[str, int] = {}
    for entry in read_learning_failures(file_path=file_path):
        counts[entry.reason] = counts.get(entry.reason, 0) + 1
    rows = [
        (
            reason,
            count,
            LEARNING_FAILURE_REASON_LABELS.get(reason, reason),
        )
        for reason, count in counts.items()
    ]
    rows.sort(key=lambda item: (-item[1], item[0]))
    return rows


def requeue_retryable_learning_failures(
    *,
    failures_file: Path = LEARNING_FAILURES_FILE,
    learning_file: Path = LEARNING_URLS_FILE,
    reasons: frozenset[str] | None = None,
) -> list[str]:
    """
    将可重试失败链接重新写入学习队列，并从失败队列移除。

    默认仅 retryable_error。返回从失败队列移出并尝试入队的 URL（去重保序）。
    """
    allowed = reasons if reasons is not None else RETRIABLE_LEARNING_FAILURE_REASONS
    failures = read_learning_failures(file_path=failures_file)
    if not failures:
        return []

    requeue_urls: list[str] = []
    remaining: list[LearningFailureEntry] = []
    for entry in failures:
        if entry.reason in allowed and entry.url:
            requeue_urls.append(entry.url)
        else:
            remaining.append(entry)

    unique_requeue = unique_urls(requeue_urls)
    if not unique_requeue:
        return []

    append_learning_urls(unique_requeue, file_path=learning_file)
    write_learning_failures(remaining, file_path=failures_file, keep_file=True)
    return unique_requeue
