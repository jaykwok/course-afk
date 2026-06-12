from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from core.config import LEARNING_FAILURES_FILE, LEARNING_URLS_FILE
from core.file_ops import del_file


@dataclass(frozen=True)
class LearningQueueEntry:
    url: str


@dataclass(frozen=True)
class LearningFailureEntry:
    url: str
    reason: str
    reason_text: str
    detail: dict[str, object]


def _normalize_text(value) -> str:
    return str(value or "").strip()


def _normalize_detail(value) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _unique_clean_strings(values) -> list[str]:
    return list(
        dict.fromkeys(
            str(value).strip()
            for value in values
            if str(value).strip()
        )
    )


def _normalize_queue_entries(raw_entries) -> list[LearningQueueEntry]:
    if not isinstance(raw_entries, list):
        raise ValueError("课程链接队列必须是 JSON 数组")

    entries_by_url: dict[str, LearningQueueEntry] = {}
    for raw_entry in raw_entries:
        if isinstance(raw_entry, dict):
            url = _normalize_text(raw_entry.get("url"))
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

    url = _normalize_text(raw_entry.get("url"))
    reason = _normalize_text(raw_entry.get("reason"))
    reason_text = _normalize_text(raw_entry.get("reason_text"))
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

    file_path.write_text(
        json.dumps(_serialize_queue_entries(normalized), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def append_learning_url(url: str, *, file_path: Path = LEARNING_URLS_FILE) -> bool:
    normalized_url = _normalize_text(url)
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
    for url in _unique_clean_strings(urls):
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
    entries = [LearningQueueEntry(url=url) for url in _unique_clean_strings(urls)]
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

    file_path.write_text(
        json.dumps(_serialize_failure_entries(normalized), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def record_learning_failure(
    url: str,
    *,
    reason: str,
    reason_text: str,
    detail: dict[str, object] | None = None,
    file_path: Path = LEARNING_FAILURES_FILE,
) -> None:
    normalized_url = _normalize_text(url)
    normalized_reason = _normalize_text(reason)
    if not normalized_url or not normalized_reason:
        return

    incoming = LearningFailureEntry(
        url=normalized_url,
        reason=normalized_reason,
        reason_text=_normalize_text(reason_text),
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
    normalized_url = _normalize_text(url)
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
