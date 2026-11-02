"""Clear selected link queues with a complete backup and stale-view protection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core.storage import serialized, write_bytes_atomic, write_json_atomic


def link_queue_paths(include_exams: bool = False) -> dict[str, Path]:
    from core import config

    paths = {
        "课程待办": config.LEARNING_URLS_FILE,
        "失败 / 复查": config.LEARNING_FAILURES_FILE,
    }
    if include_exams:
        paths.update(
            {"AI 考试": config.EXAM_URLS_FILE, "考试复查": config.MANUAL_EXAM_FILE}
        )
    return paths


@serialized
def snapshot_link_queues(include_exams: bool = False) -> dict[Path, bytes | None]:
    from core.tasks.store import read_queue

    result = {}
    for path in link_queue_paths(include_exams).values():
        raw = path.read_bytes() if path.exists() else None
        try:
            rows = read_queue(path)
            result[path] = (
                raw
                if raw is not None and json.loads(raw) == rows
                else (
                    json.dumps(rows, ensure_ascii=False).encode()
                    if rows or raw is not None
                    else None
                )
            )
        except (ValueError, UnicodeError):
            result[path] = raw
    return result


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
    counts = {
        label: queue_record_count(snapshot[path]) for label, path in paths.items()
    }
    from core.tasks.store import repository_for
    from core.tasks.identity import account_id

    repositories = {
        repository_for(path).path: repository_for(path) for path in paths.values()
    }
    reviews = {}
    for key, repo in repositories.items():
        with repo.connect() as db:
            reviews[key] = list(
                db.execute(
                    "SELECT p.task_id AS parent,c.task_id AS child FROM task_edges e JOIN tasks p ON p.task_id=e.parent JOIN tasks c ON c.task_id=e.child WHERE p.account=? AND c.kind='url_review' AND c.status NOT IN ('COMPLETED','DISMISSED')",
                    (account_id(),),
                )
            )
    if all(count == 0 for count in counts.values()) and not any(reviews.values()):
        return ClearQueuesResult(counts, None)
    backup = DATA_DIR / "backups" / f"clear-links-{datetime.now():%Y%m%d_%H%M%S_%f}"
    backup.mkdir(parents=True, exist_ok=False)
    # 所有原文件及清单成功落盘后才能清空，连损坏 JSON 也原样保存。
    for path, content in snapshot.items():
        if content is not None:
            write_bytes_atomic(backup / path.name, content)
    write_json_atomic(
        backup / "manifest.json",
        {
            "scope": "all_pending" if include_exams else "learning",
            "counts": counts,
            "files": [
                str(path) for path, content in snapshot.items() if content is not None
            ],
        },
    )
    cleared = []
    try:
        from contextlib import ExitStack

        for index, repository in enumerate(repositories.values()):
            repository.backup(backup / f"state-{index}.sqlite")
        with ExitStack() as stack:
            connections = {
                path: stack.enter_context(repo.connect())
                for path, repo in repositories.items()
            }
            for path, content in snapshot.items():
                db = connections[repository_for(path).path]
                repo = repositories[repository_for(path).path]
                removed = [
                    row[0]
                    for row in db.execute(
                        "SELECT DISTINCT q.task_id FROM queue_items q JOIN tasks t USING(task_id) WHERE collection=? AND account=?",
                        (path.name, account_id()),
                    )
                ]
                scope = (
                    "exam"
                    if path.name in {"考试链接.json", "人工考试链接.json"}
                    else "learn"
                )
                repo.dismiss(removed, scope, db=db)
                if scope == "learn":
                    issues = [
                        row[0]
                        for task_id in removed
                        for row in db.execute(
                            "SELECT c.task_id FROM task_edges e JOIN tasks c ON c.task_id=e.child WHERE e.parent=? AND c.kind IN ('issue','url_review')",
                            (task_id,),
                        )
                    ]
                    repo.dismiss(issues, "learn", db=db)
                db.execute(
                    "DELETE FROM queue_items WHERE collection=? AND task_id IN (SELECT task_id FROM tasks WHERE account=?)",
                    (path.name, account_id()),
                )
                if content is not None:
                    write_bytes_atomic(path, b"[]\n")
                    cleared.append(path)
            for key, items in reviews.items():
                repo = repositories[key]
                repo.dismiss(
                    {r["parent"] for r in items} | {r["child"] for r in items},
                    "learn",
                    db=connections[key],
                )
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
