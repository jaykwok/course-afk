from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from core.config import MANUAL_EXAM_FILE
from core.exam_queue import normalize_model_config, unique_model_configs
from core.file_ops import del_file


@dataclass(frozen=True)
class ManualExamEntry:
    url: str
    reason: str | None
    reason_text: str | None
    remaining_attempts: int | None
    threshold: int | None
    ai_failed_model_configs: list[dict[str, object]]


def _normalize_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_entry(raw_entry) -> ManualExamEntry | None:
    if not isinstance(raw_entry, dict):
        return None

    url = str(raw_entry.get("url", "")).strip()
    if not url:
        return None

    return ManualExamEntry(
        url=url,
        reason=_normalize_text(raw_entry.get("reason")),
        reason_text=_normalize_text(raw_entry.get("reason_text")),
        remaining_attempts=_normalize_int(raw_entry.get("remaining_attempts")),
        threshold=_normalize_int(raw_entry.get("threshold")),
        ai_failed_model_configs=unique_model_configs(
            raw_entry.get("ai_failed_model_configs", [])
        ),
    )


def _merge_entries(existing: ManualExamEntry, incoming: ManualExamEntry) -> ManualExamEntry:
    return ManualExamEntry(
        url=existing.url,
        reason=incoming.reason or existing.reason,
        reason_text=incoming.reason_text or existing.reason_text,
        remaining_attempts=(
            incoming.remaining_attempts
            if incoming.remaining_attempts is not None
            else existing.remaining_attempts
        ),
        threshold=incoming.threshold if incoming.threshold is not None else existing.threshold,
        ai_failed_model_configs=unique_model_configs(
            existing.ai_failed_model_configs + incoming.ai_failed_model_configs
        ),
    )


def _normalize_entries(raw_entries) -> list[ManualExamEntry]:
    if not isinstance(raw_entries, list):
        raise ValueError("人工考试队列必须是 JSON 数组")

    entries_by_url: dict[str, ManualExamEntry] = {}
    for raw_entry in raw_entries:
        entry = _normalize_entry(raw_entry)
        if entry is None:
            continue
        existing = entries_by_url.get(entry.url)
        entries_by_url[entry.url] = (
            _merge_entries(existing, entry) if existing is not None else entry
        )
    return list(entries_by_url.values())


def _serialize_entries(entries: list[ManualExamEntry]) -> list[dict[str, object]]:
    return [
        {
            "url": entry.url,
            "reason": entry.reason,
            "reason_text": entry.reason_text,
            "remaining_attempts": entry.remaining_attempts,
            "threshold": entry.threshold,
            "ai_failed_model_configs": entry.ai_failed_model_configs,
        }
        for entry in entries
    ]


def read_manual_exam_queue(file_path: Path = MANUAL_EXAM_FILE) -> list[ManualExamEntry]:
    try:
        content = file_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return []

    if not content:
        return []

    try:
        raw_entries = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"人工考试队列不是有效 JSON: {exc}") from exc
    return _normalize_entries(raw_entries)


def write_manual_exam_queue(
    entries: list[ManualExamEntry],
    *,
    file_path: Path = MANUAL_EXAM_FILE,
    keep_file: bool = True,
) -> None:
    normalized = _normalize_entries(_serialize_entries(entries))
    if not normalized and not keep_file:
        del_file(file_path)
        return

    file_path.write_text(
        json.dumps(_serialize_entries(normalized), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def append_manual_exam_entry(
    url: str,
    *,
    reason: str,
    reason_text: str,
    remaining_attempts: int | None = None,
    threshold: int | None = None,
    ai_failed_model_config: dict[str, object] | None = None,
    file_path: Path = MANUAL_EXAM_FILE,
) -> None:
    normalized_url = url.strip()
    if not normalized_url:
        return

    ai_failed_model_configs = []
    normalized_model_config = normalize_model_config(ai_failed_model_config)
    if normalized_model_config is not None:
        ai_failed_model_configs.append(normalized_model_config)

    entries = read_manual_exam_queue(file_path=file_path)
    incoming = ManualExamEntry(
        url=normalized_url,
        reason=reason,
        reason_text=reason_text,
        remaining_attempts=remaining_attempts,
        threshold=threshold,
        ai_failed_model_configs=ai_failed_model_configs,
    )
    entries_by_url = {entry.url: entry for entry in entries}
    existing = entries_by_url.get(normalized_url)
    if existing is None:
        entries.append(incoming)
    else:
        entries = [
            _merge_entries(entry, incoming) if entry.url == normalized_url else entry
            for entry in entries
        ]
    write_manual_exam_queue(entries, file_path=file_path)


def read_manual_exam_urls(file_path: Path = MANUAL_EXAM_FILE) -> list[str]:
    return [entry.url for entry in read_manual_exam_queue(file_path=file_path)]


def count_manual_exam_urls(file_path: Path = MANUAL_EXAM_FILE) -> int:
    return len(read_manual_exam_urls(file_path=file_path))
