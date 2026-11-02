from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from core.config import EXAM_URLS_FILE
from core.tasks.identity import canonical_url
from core.file_ops import del_file
from core.links import unique_urls
from core.storage import serialized, write_text_atomic
from core.queues.history import failed_configs, record_failures


@dataclass(frozen=True)
class ExamQueueEntry:
    url: str
    ai_failed_model_configs: list[dict[str, object]]
    requires_manual_submit: bool = False


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
        "base_url": str(raw_config.get("base_url") or "").strip().rstrip("/") or None,
        "provider": raw_config.get("provider"),
        "output_mode": raw_config.get("output_mode"),
        "prompt_version": raw_config.get("prompt_version"),
        "temperature": raw_config.get("temperature"),
        "account": raw_config.get("account"),
    }


def _model_config_key(config: dict[str, object]) -> str:
    return json.dumps(config, sort_keys=True, ensure_ascii=False)


def _unique_model_configs(raw_configs) -> list[dict[str, object]]:
    if not isinstance(raw_configs, list):
        return []

    configs_by_key: dict[str, dict[str, object]] = {}
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
        url = canonical_url(str(raw_entry.get("url", "")).strip())
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
            requires_manual_submit=bool(raw_entry.get("requires_manual_submit")) or bool(existing and existing.requires_manual_submit),
        )
    return list(entries_by_url.values())


def _serialize_entries(entries: list[ExamQueueEntry]) -> list[dict[str, object]]:
    return [
        {
            "url": entry.url,
            "ai_failed_model_configs": entry.ai_failed_model_configs,
            **({"requires_manual_submit": True} if entry.requires_manual_submit else {}),
        }
        for entry in entries
    ]


def read_exam_queue(file_path: Path = EXAM_URLS_FILE) -> list[ExamQueueEntry]:
    from core.tasks.store import read_queue
    return _normalize_entries(read_queue(file_path))


@serialized
def write_exam_queue(
    entries: list[ExamQueueEntry],
    *,
    file_path: Path = EXAM_URLS_FILE,
    keep_file: bool = True,
) -> None:
    normalized = _normalize_entries(_serialize_entries(entries))
    # Archive both retained and removed entries before replacing the queue.
    for entry in read_exam_queue(file_path) + normalized:
        record_failures(entry.url, entry.ai_failed_model_configs, file_path)
        if entry.requires_manual_submit:
            from core.queues.history import protect_manual_submission
            protect_manual_submission(entry.url,file_path)
    from core.tasks.store import write_queue
    write_queue(file_path, _serialize_entries(normalized), keep_file=keep_file)


def append_exam_url(url: str, *, file_path: Path = EXAM_URLS_FILE) -> None:
    append_exam_urls([url], file_path=file_path)


@serialized
def append_exam_urls(
    urls: list[str],
    *,
    file_path: Path = EXAM_URLS_FILE,
    requires_manual_submit: bool = False,
) -> list[str]:
    normalized_urls = unique_urls([canonical_url(u) for u in urls])
    if not normalized_urls:
        return []

    entries = read_exam_queue(file_path=file_path)
    existing_urls = {entry.url for entry in entries}
    added: list[str] = []
    changed = False
    for normalized_url in normalized_urls:
        if normalized_url in existing_urls:
            if requires_manual_submit:
                for index, entry in enumerate(entries):
                    if entry.url == normalized_url and not entry.requires_manual_submit:
                        entries[index] = replace(entry, requires_manual_submit=True)
                        changed = True
                        break
            continue
        entries.append(
            ExamQueueEntry(url=normalized_url, ai_failed_model_configs=_unique_model_configs(failed_configs(normalized_url, file_path)), requires_manual_submit=requires_manual_submit)
        )
        existing_urls.add(normalized_url)
        added.append(normalized_url)

    if added or changed:
        write_exam_queue(entries, file_path=file_path)
    return added


@serialized
def remove_exam_url(url: str, *, file_path: Path = EXAM_URLS_FILE) -> bool:
    """从 AI 考试队列移除单个链接，保留队列文件。"""
    normalized_url = canonical_url(url or "")
    if not normalized_url:
        return False

    entries = read_exam_queue(file_path=file_path)
    retained = [entry for entry in entries if entry.url != normalized_url]
    if len(retained) == len(entries):
        return False
    write_exam_queue(retained, file_path=file_path)
    return True


def read_exam_urls(file_path: Path = EXAM_URLS_FILE) -> list[str]:
    return [entry.url for entry in read_exam_queue(file_path=file_path)]


def requires_manual_submission(url: str, *, file_path: Path = EXAM_URLS_FILE) -> bool:
    from core.queues.history import manual_submission_required
    return manual_submission_required(url,file_path) or any(entry.url == canonical_url(url) and entry.requires_manual_submit for entry in read_exam_queue(file_path))


def count_exam_urls(file_path: Path = EXAM_URLS_FILE) -> int:
    return len(read_exam_urls(file_path=file_path))


@serialized
def write_exam_urls(
    urls: list[str],
    *,
    file_path: Path = EXAM_URLS_FILE,
    keep_file: bool = True,
) -> None:
    existing_by_url = {entry.url: entry for entry in read_exam_queue(file_path=file_path)}
    entries = [
        existing_by_url.get(url, ExamQueueEntry(url, []))
        for url in unique_urls(urls)
    ]
    write_exam_queue(entries, file_path=file_path, keep_file=keep_file)


def has_ai_failed_model_config(
    url: str,
    model_config: dict[str, object],
    *,
    file_path: Path = EXAM_URLS_FILE,
) -> bool:
    normalized_url = canonical_url(url)
    normalized_config = _normalize_model_config(model_config)
    if not normalized_url or normalized_config is None:
        return False

    history = _unique_model_configs(failed_configs(normalized_url, file_path))
    if any(_model_config_key(config) == _model_config_key(normalized_config) for config in history):
        return True
    for entry in read_exam_queue(file_path=file_path):
        if entry.url == normalized_url:
            return any(
                _model_config_key(config) == _model_config_key(normalized_config)
                for config in entry.ai_failed_model_configs
            )
    return False


@serialized
def record_ai_failed_model_config(
    url: str,
    model_config: dict[str, object],
    *,
    file_path: Path = EXAM_URLS_FILE,
) -> None:
    normalized_url = canonical_url(url)
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
            replace(entry,
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
