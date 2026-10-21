"""Clear selected link queues with a complete backup and stale-view protection."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core.storage import serialized, write_bytes_atomic, write_json_atomic


def link_queue_paths(include_exams: bool = False) -> dict[str, Path]:
    from core import config

    paths = {"课程待办": config.LEARNING_URLS_FILE, "失败 / 复查": config.LEARNING_FAILURES_FILE}
    if include_exams:
        paths.update({"AI 考试": config.EXAM_URLS_FILE, "考试复查": config.MANUAL_EXAM_FILE})
    return paths


@serialized
def snapshot_link_queues(include_exams: bool = False) -> dict[Path, bytes | None]:
    return {path: path.read_bytes() if path.exists() else None for path in link_queue_paths(include_exams).values()}


def queue_record_count(content: bytes | None) -> int | None:
    if content is None or not content.strip():
        return 0
    try:
        data = json.loads(content)
    except (ValueError, UnicodeError):
        return None
    return len(data) if isinstance(data, list) else None


@dataclass(frozen=True)
class ClearQueuesResult:
    counts: dict[str, int | None]
    backup_dir: Path | None


@serialized
def clear_link_queues(
    include_exams: bool = False,
    *,
    expected_snapshot: dict[Path, bytes | None] | None = None,
) -> ClearQueuesResult:
    from core.config import DATA_DIR

    paths = link_queue_paths(include_exams)
    snapshot = snapshot_link_queues(include_exams)
    if expected_snapshot is not None and snapshot != expected_snapshot:
        raise RuntimeError("待办在确认期间发生变化，请重新查看后再清空")
    counts = {label: queue_record_count(snapshot[path]) for label, path in paths.items()}
    if all(count == 0 for count in counts.values()):
        return ClearQueuesResult(counts, None)
    backup = DATA_DIR / "backups" / f"clear-links-{datetime.now():%Y%m%d_%H%M%S_%f}"
    backup.mkdir(parents=True, exist_ok=False)
    # 所有原文件及清单成功落盘后才能清空，连损坏 JSON 也原样保存。
    for path, content in snapshot.items():
        if content is not None:
            write_bytes_atomic(backup / path.name, content)
    write_json_atomic(backup / "manifest.json", {
        "scope": "all_pending" if include_exams else "learning",
        "counts": counts,
        "files": [str(path) for path, content in snapshot.items() if content is not None],
    })
    cleared = []
    try:
        for path, content in snapshot.items():
            if content is not None:
                write_bytes_atomic(path, b"[]\n")
                cleared.append(path)
    except OSError as exc:
        rollback_errors = []
        for path in cleared:
            try:
                write_bytes_atomic(path, snapshot[path])
            except OSError:
                rollback_errors.append(path.name)
        status = "部分文件未能恢复" if rollback_errors else "已恢复已清空的文件"
        raise RuntimeError(f"清空未完成，{status}；原始备份：{backup}") from exc
    return ClearQueuesResult(counts, backup)
