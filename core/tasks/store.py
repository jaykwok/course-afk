"""Compatibility projections over TaskRepository, never a second JSON queue.

JSON files are imported once (or explicitly), then exported as readable
snapshots. Queue membership and history are always read from SQLite.
"""

from __future__ import annotations

import hashlib
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


def _read_source(path: Path):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return [], "missing"
    if not raw.strip():
        return [], hashlib.sha256(raw).hexdigest()
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, list):
        raise ValueError(f"队列必须是 JSON 数组: {path.name}")
    return value, hashlib.sha256(raw).hexdigest()


def import_queue(
    path: Path,
    *,
    account="unbound",
    force=False,
    db=None,
    repository=None,
    expected_fingerprint=None,
):
    path = Path(path).resolve()
    repo = repository or repository_for(path)
    if db is None:
        with repo.connect() as connection:
            return import_queue(
                path,
                account=account,
                force=force,
                db=connection,
                repository=repo,
                expected_fingerprint=expected_fingerprint,
            )
    source = str(path)
    previous = db.execute(
        "SELECT fingerprint FROM imports WHERE source=?", (source,)
    ).fetchone()
    if previous and not force:
        return
    rows, fingerprint = _read_source(path)
    if expected_fingerprint is not None and fingerprint != expected_fingerprint:
        raise RuntimeError("迁移期间源文件发生变化，导入事务已回滚")
    if any(
        not isinstance(row, dict)
        or not isinstance(row.get("url"), str)
        or not row["url"].strip()
        for row in rows
    ):
        raise ValueError(f"队列含无法识别的条目，源文件保留: {path.name}")
    _replace(repo, db, path.name, rows, account, merge=True)
    db.execute(
        "INSERT OR REPLACE INTO imports VALUES(?,?,?)", (source, fingerprint, now())
    )


def _replace(repo, db, collection, rows, account, *, merge=False):
    previous = list(
        db.execute(
            "SELECT task_id,item_key,t.identity FROM queue_items q JOIN tasks t USING(task_id) WHERE collection=? AND t.account=?",
            (collection, account),
        )
    )
    if not merge:
        db.execute(
            "DELETE FROM queue_items WHERE collection=? AND task_id IN (SELECT task_id FROM tasks WHERE account=?)",
            (collection, account),
        )
    from core.workflow.host import activity_context

    context = activity_context.get()
    retained_issues = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not row.get("url"):
            continue
        entry = parse_entry(row["url"], account)
        task_id = repo.upsert(entry, db=db)
        payload = {**row, "url": entry.url}
        if row.get("requires_manual_submit"):
            previous_history = db.execute(
                "SELECT payload FROM exam_history WHERE task_id=?", (task_id,)
            ).fetchone()
            history = json.loads(previous_history[0]) if previous_history else {}
            history["requires_manual_submit"] = True
            db.execute(
                "INSERT OR REPLACE INTO exam_history VALUES(?,?)",
                (task_id, encode(history)),
            )
        # Independent issues coexist, including multiple sections of one type.
        detail = row.get("detail") or {}
        issue = ":".join(
            str(detail.get(k) or "")
            for k in ("section_id", "section_index", "resource_id", "section_type")
        )
        item_key = (
            str(row.get("reason") or "") + ":" + issue if "reason" in row else "queue"
        )
        db.execute(
            "INSERT OR REPLACE INTO queue_items VALUES(?,?,?,?,?)",
            (collection, task_id, item_key, encode(payload), index),
        )
        if context and task_id != context["task_id"]:
            repo.link(context["task_id"], task_id, db=db)
            if entry.key.kind == "exam":
                parent = db.execute(
                    "SELECT kind FROM tasks WHERE task_id=?", (context["task_id"],)
                ).fetchone()
                # Only API-resolved exam IDs create this association. A course
                # route alone never invents the standalone exam's identity.
                if parent and parent["kind"] == "course":
                    old = db.execute(
                        "SELECT payload FROM exam_history WHERE task_id=?",
                        (context["task_id"],),
                    ).fetchone()
                    current = db.execute(
                        "SELECT payload FROM exam_history WHERE task_id=?", (task_id,)
                    ).fetchone()
                    if old:
                        previous = json.loads(old[0])
                        history = json.loads(current[0]) if current else {}
                        history["failed_models"] = list(
                            {
                                encode(c): c
                                for c in history.get("failed_models", [])
                                + previous.get("failed_models", [])
                            }.values()
                        )
                        history["requires_manual_submit"] = bool(
                            history.get("requires_manual_submit")
                            or previous.get("requires_manual_submit")
                        )
                        for owner, submission in previous.get(
                            "submissions", {}
                        ).items():
                            if owner not in history.setdefault("submissions", {}):
                                history["submissions"][owner] = submission
                            elif history["submissions"][owner] != submission:
                                history.setdefault("legacy_submissions", []).append(
                                    {"account": owner, **submission}
                                )
                        db.execute(
                            "INSERT OR REPLACE INTO exam_history VALUES(?,?)",
                            (task_id, encode(history)),
                        )
                    db.execute(
                        "UPDATE action_intents SET task_id=? WHERE task_id=? AND operation='submit'",
                        (task_id, context["task_id"]),
                    )
        if row.get("reason"):
            issue_key = TaskKey(
                entry.key.tenant, account, "issue", task_id + ":" + item_key
            )
            issue_id = repo.upsert(Entry(issue_key, entry.url, ()), db=db)
            retained_issues.add(issue_id)
            repo.link(task_id, issue_id, db=db)
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
            repo.link(parent, task_id, required=entry.key.required == "1", db=db)
    if not merge:
        for previous_item in previous:
            if previous_item["item_key"] == "queue":
                continue
            identity = json.loads(previous_item["identity"])
            issue = TaskKey(
                identity["tenant"],
                account,
                "issue",
                previous_item["task_id"] + ":" + previous_item["item_key"],
            ).task_id
            if issue not in retained_issues:
                db.execute(
                    "UPDATE tasks SET status='COMPLETED',updated_at=? WHERE task_id=?",
                    (now(), issue),
                )
    # A queue removal never supplies completion evidence. Outcomes own status.
    return previous


def read_queue(path: Path) -> list:
    path = Path(path).resolve()
    repo = repository_for(path)
    import_queue(path, repository=repo)
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
    import_queue(path, repository=repo)
    with repo.connect() as db:
        _replace(repo, db, path.name, rows, account_id())
    # Export after commit: interrupted export cannot roll back committed state.
    if rows or keep_file:
        write_json_atomic(path, read_queue(path))
    else:
        path.unlink(missing_ok=True)


def read_history(path: Path) -> dict:
    repo = repository_for(path)
    from core.tasks.migration import import_history

    import_history(repo, Path(path).with_name("exam-history.json"))
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
