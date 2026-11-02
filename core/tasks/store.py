"""SQLite queue and history storage with readable JSON output snapshots."""

from __future__ import annotations

import json
from pathlib import Path

from core.storage import write_json_atomic
from core.tasks.identity import account_id, parse_entry, TaskKey, Entry
from core.tasks.repository import TaskRepository, encode, now


def repository_for(path: Path) -> TaskRepository:
    from core.config import LINKS_DIR, DATA_DIR

    parent = Path(path).resolve().parent
    directory = (
        DATA_DIR / "workflows"
        if parent == LINKS_DIR.resolve()
        else parent / ".workflows"
    )
    return TaskRepository(directory / "state.sqlite")


def _replace(repo, db, collection, rows, account):
    previous = list(
        db.execute(
            "SELECT task_id,item_key,q.payload,t.identity FROM queue_items q JOIN tasks t USING(task_id) WHERE collection=? AND t.account=?",
            (collection, account),
        )
    )
    existing = {
        (r["task_id"], r["item_key"]): json.loads(r["payload"]) for r in previous
    }
    changed = set()
    db.execute(
        "DELETE FROM queue_items WHERE collection=? AND task_id IN (SELECT task_id FROM tasks WHERE account=?)",
        (collection, account),
    )
    retained_issues = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not row.get("url"):
            continue
        entry = parse_entry(row["url"], account)
        task_id = repo.upsert(entry, db=db)
        scope = (
            "exam" if collection in {"考试链接.json", "人工考试链接.json"} else "learn"
        )
        repo.select(task_id, scope, db=db)
        payload = {**row, "url": entry.url}
        # Independent issues coexist, including multiple sections of one type.
        detail = row.get("detail") or {}
        issue = ":".join(
            str(detail[k]) if detail.get(k) is not None else ""
            for k in ("section_id", "section_index", "resource_id", "section_type")
        )
        item_key = (
            str(row.get("reason") or "") + ":" + issue if "reason" in row else "queue"
        )
        from core.tasks.records import merge_queue, merge_history

        old = db.execute(
            "SELECT payload FROM queue_items WHERE collection=? AND task_id=? AND item_key=?",
            (collection, task_id, item_key),
        ).fetchone()
        # Merge duplicate aliases inside a queue write and preserve sticky protection.
        previous_payload = existing.get((task_id, item_key), {})
        payload = merge_queue(
            merge_queue(previous_payload, json.loads(old[0]) if old else {}), payload
        )
        db.execute(
            "INSERT OR REPLACE INTO queue_items VALUES(?,?,?,?,?)",
            (collection, task_id, item_key, encode(payload), index),
        )
        if payload.get("requires_manual_submit") or payload.get(
            "ai_failed_model_configs"
        ):
            old_history = db.execute(
                "SELECT payload FROM exam_history WHERE task_id=?", (task_id,)
            ).fetchone()
            history = merge_history(
                json.loads(old_history[0]) if old_history else {},
                {
                    "failed_models": payload.get("ai_failed_model_configs", []),
                    "requires_manual_submit": payload.get(
                        "requires_manual_submit", False
                    ),
                },
            )
            db.execute(
                "INSERT OR REPLACE INTO exam_history VALUES(?,?)",
                (task_id, encode(history)),
            )
        if row.get("reason"):
            issue_kind = (
                "url_review" if row["reason"] == "url_type_pending" else "issue"
            )
            issue_key = TaskKey(
                entry.key.tenant, account, issue_kind, task_id + ":" + item_key
            )
            issue_id = repo.upsert(Entry(issue_key, entry.url, ()), db=db)
            retained_issues.add(issue_id)
            repo.link(
                task_id,
                issue_id,
                db=db,
                source="issue",
                evidence={"collection": collection, "item_key": item_key},
            )
            changed.add(issue_id)
            db.execute(
                "UPDATE tasks SET status='NEEDS_REVIEW',outcome=?,updated_at=? WHERE task_id=?",
                (encode(payload), now(), issue_id),
            )
            db.execute(
                "UPDATE tasks SET status='NEEDS_REVIEW',updated_at=? WHERE task_id=? AND status!='RUNNING'",
                (now(), task_id),
            )
        if (
            entry.key.kind == "course"
            and entry.key.source_type == "6"
            and entry.key.source_id
        ):
            from core.config import ZHIXUEYUN_SUBJECT_PREFIX

            source = parse_entry(
                ZHIXUEYUN_SUBJECT_PREFIX + entry.key.source_id, account
            )
            parent = repo.upsert(source, db=db)
            repo.link(
                parent,
                task_id,
                required=entry.key.required == "1",
                db=db,
                source="course_source",
                evidence={"url": entry.url},
            )
    for previous_item in previous:
        if previous_item["item_key"] == "queue":
            continue
        identity = json.loads(previous_item["identity"])
        issue = TaskKey(
            identity["tenant"],
            account,
            "url_review"
            if previous_item["item_key"].startswith("url_type_pending:")
            else "issue",
            previous_item["task_id"] + ":" + previous_item["item_key"],
        ).task_id
        if issue not in retained_issues:
            changed.add(issue)
            db.execute(
                "UPDATE tasks SET status='COMPLETED',updated_at=? WHERE task_id=?",
                (now(), issue),
            )
    repo.refresh_ancestors(changed, db=db)
    # A queue removal never supplies completion evidence. Outcomes own status.
    return previous


def read_queue(path: Path) -> list:
    path = Path(path).resolve()
    repo = repository_for(path)
    with repo.connect() as db:
        return [
            json.loads(row[0])
            for row in db.execute(
                "SELECT q.payload FROM queue_items q JOIN tasks t USING(task_id) WHERE collection=? AND t.account=? ORDER BY position,q.rowid",
                (path.name, account_id()),
            )
        ]


def write_queue(path: Path, rows: list, *, keep_file=True):
    path = Path(path).resolve()
    repo = repository_for(path)
    with repo.connect() as db:
        _replace(repo, db, path.name, rows, account_id())
    # Export after commit: interrupted export cannot roll back committed state.
    if rows or keep_file:
        write_json_atomic(path, read_queue(path))
    else:
        path.unlink(missing_ok=True)


def read_history(path: Path) -> dict:
    repo = repository_for(path)
    with repo.connect() as db:
        return {
            "version": 2,
            "entries": {
                r["url"]: json.loads(r["payload"])
                for r in db.execute(
                    "SELECT t.url,h.payload FROM exam_history h JOIN tasks t USING(task_id) WHERE account=?",
                    (account_id(),),
                )
            },
        }


def write_history(path: Path, data: dict):
    repo = repository_for(path)
    with repo.connect() as db:
        for url, payload in data["entries"].items():
            task_id = repo.upsert(parse_entry(url, account_id()), db=db)
            db.execute(
                "INSERT OR REPLACE INTO exam_history VALUES(?,?)",
                (task_id, encode(payload)),
            )
    write_json_atomic(Path(path).with_name("exam-history.json"), read_history(path))
