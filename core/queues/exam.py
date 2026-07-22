from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from core.config import EXAM_URLS_FILE
from core.file_ops import del_file, write_text_atomic
from core.links import unique_urls


@dataclass(frozen=True)
class ExamQueueEntry:
    url: str
    ai_failed_model_configs: list[dict[str, object]]


def _normalize_model_config(raw_config) -> dict[str, object] | None:
    if not isinstance(raw_config, dict):
        return None

    model = str(raw_config.get("model", "")).strip()
    if not model:
        return None

    raw_request_type = raw_config.get("request_type")
    request_type = (
        str(raw_request_type).strip().lower()
        if raw_request_type is not None and str(raw_request_type).strip()
        else None
    )
    raw_reasoning_effort = raw_config.get("reasoning_effort")
    reasoning_effort = (
        str(raw_reasoning_effort).strip().lower()
        if raw_reasoning_effort is not None and str(raw_reasoning_effort).strip()
        else None
    )
    return {
        "model": model,
        "request_type": request_type,
        "web_search": bool(raw_config.get("web_search", False)),
        "thinking": bool(raw_config.get("thinking", False)),
        "reasoning_effort": reasoning_effort,
    }


def _model_config_key(config: dict[str, object]) -> tuple[object, ...]:
    return (
        config["model"],
        config["request_type"],
        config["web_search"],
        config["thinking"],
        config["reasoning_effort"],
    )


def _unique_model_configs(raw_configs) -> list[dict[str, object]]:
    if not isinstance(raw_configs, list):
        return []

    configs_by_key: dict[tuple[object, ...], dict[str, object]] = {}
    for raw_config in raw_configs:
        config = _normalize_model_config(raw_config)
        if config is None:
            continue
        configs_by_key[_model_config_key(config)] = config
    return list(configs_by_key.values())


def normalize_model_config(raw_config) -> dict[str, object] | None:
    return _normalize_model_config(raw_config)


def unique_model_configs(raw_configs) -> list[dict[str, object]]:
    return _unique_model_configs(raw_configs)


def _normalize_entries(raw_entries) -> list[ExamQueueEntry]:
    if not isinstance(raw_entries, list):
        raise ValueError("考试链接队列必须是 JSON 数组")

    entries_by_url: dict[str, ExamQueueEntry] = {}
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            continue
        url = str(raw_entry.get("url", "")).strip()
        if not url:
            continue
        failed_model_configs = _unique_model_configs(
            raw_entry.get("ai_failed_model_configs", [])
        )
        existing = entries_by_url.get(url)
        if existing is not None:
            failed_model_configs = _unique_model_configs(
                existing.ai_failed_model_configs + failed_model_configs
            )
        entries_by_url[url] = ExamQueueEntry(
            url=url,
            ai_failed_model_configs=failed_model_configs,
        )
    return list(entries_by_url.values())


def _serialize_entries(entries: list[ExamQueueEntry]) -> list[dict[str, object]]:
    return [
        {
            "url": entry.url,
            "ai_failed_model_configs": entry.ai_failed_model_configs,
        }
        for entry in entries
    ]


def read_exam_queue(file_path: Path = EXAM_URLS_FILE) -> list[ExamQueueEntry]:
    try:
        content = file_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return []

    if not content:
        return []

    try:
        raw_entries = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"考试链接队列不是有效 JSON: {exc}") from exc
    return _normalize_entries(raw_entries)


def write_exam_queue(
    entries: list[ExamQueueEntry],
    *,
    file_path: Path = EXAM_URLS_FILE,
    keep_file: bool = True,
) -> None:
    normalized = _normalize_entries(_serialize_entries(entries))
    if not normalized and not keep_file:
        del_file(file_path)
        return

    write_text_atomic(
        file_path,
        json.dumps(_serialize_entries(normalized), ensure_ascii=False, indent=2),
    )


def append_exam_url(url: str, *, file_path: Path = EXAM_URLS_FILE) -> None:
    append_exam_urls([url], file_path=file_path)


def append_exam_urls(
    urls: list[str],
    *,
    file_path: Path = EXAM_URLS_FILE,
) -> list[str]:
    normalized_urls = unique_urls(urls)
    if not normalized_urls:
        return []

    entries = read_exam_queue(file_path=file_path)
    existing_urls = {entry.url for entry in entries}
    added: list[str] = []
    for normalized_url in normalized_urls:
        if normalized_url in existing_urls:
            continue
        entries.append(
            ExamQueueEntry(url=normalized_url, ai_failed_model_configs=[])
        )
        existing_urls.add(normalized_url)
        added.append(normalized_url)

    if added:
        write_exam_queue(entries, file_path=file_path)
    return added


def read_exam_urls(file_path: Path = EXAM_URLS_FILE) -> list[str]:
    return [entry.url for entry in read_exam_queue(file_path=file_path)]


def count_exam_urls(file_path: Path = EXAM_URLS_FILE) -> int:
    return len(read_exam_urls(file_path=file_path))


def write_exam_urls(
    urls: list[str],
    *,
    file_path: Path = EXAM_URLS_FILE,
    keep_file: bool = True,
) -> None:
    existing_by_url = {entry.url: entry for entry in read_exam_queue(file_path=file_path)}
    entries = [
        ExamQueueEntry(
            url=url,
            ai_failed_model_configs=existing_by_url.get(
                url,
                ExamQueueEntry(url=url, ai_failed_model_configs=[]),
            ).ai_failed_model_configs,
        )
        for url in unique_urls(urls)
    ]
    write_exam_queue(entries, file_path=file_path, keep_file=keep_file)


def has_ai_failed_model_config(
    url: str,
    model_config: dict[str, object],
    *,
    file_path: Path = EXAM_URLS_FILE,
) -> bool:
    normalized_url = url.strip()
    normalized_config = _normalize_model_config(model_config)
    if not normalized_url or normalized_config is None:
        return False

    for entry in read_exam_queue(file_path=file_path):
        if entry.url == normalized_url:
            return any(
                _model_config_key(config) == _model_config_key(normalized_config)
                for config in entry.ai_failed_model_configs
            )
    return False


def record_ai_failed_model_config(
    url: str,
    model_config: dict[str, object],
    *,
    file_path: Path = EXAM_URLS_FILE,
) -> None:
    normalized_url = url.strip()
    normalized_config = _normalize_model_config(model_config)
    if not normalized_url or normalized_config is None:
        return

    entries = read_exam_queue(file_path=file_path)
    entries_by_url = {entry.url: entry for entry in entries}
    existing = entries_by_url.get(normalized_url)
    if existing is None:
        entries.append(
            ExamQueueEntry(
                url=normalized_url,
                ai_failed_model_configs=[normalized_config],
            )
        )
    elif not has_ai_failed_model_config(
        normalized_url,
        normalized_config,
        file_path=file_path,
    ):
        entries = [
            ExamQueueEntry(
                url=entry.url,
                ai_failed_model_configs=(
                    entry.ai_failed_model_configs + [normalized_config]
                    if entry.url == normalized_url
                    else entry.ai_failed_model_configs
                ),
            )
            for entry in entries
        ]
    else:
        return

    write_exam_queue(entries, file_path=file_path)
