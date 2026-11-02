"""Relations are written only from explicit domain discoveries, never snapshots."""

from __future__ import annotations

import hashlib
import json

from core.tasks.identity import account_id, parse_entry
from core.tasks.records import merge_history
from core.tasks.repository import encode, now


def record_discovered_tasks(
    urls,
    *,
    source,
    parent_url=None,
    course_exam=False,
    completed_urls=(),
    repository=None,
    account=None,
):
    from core.workflow.host import activity_context, current_host

    context = activity_context.get()
    if repository is None:
        if context:
            repository = current_host().repository
        else:
            from core.config import LEARNING_URLS_FILE
            from core.tasks.store import repository_for

            repository = repository_for(LEARNING_URLS_FILE)
    account = account or (
        context["account"] if context and "account" in context else account_id()
    )
    if parent_url is None:
        if not context:
            return []
        parent = repository.task(context["task_id"])
        parent_url = parent["url"]
        account = parent["account"]
    parent_entry = parse_entry(parent_url, account)
    children = []
    with repository.connect() as db:
        parent_id = repository.upsert(parent_entry, db=db)
        for url in dict.fromkeys(urls):
            entry = parse_entry(url, account)
            child = repository.upsert(entry, db=db)
            required = entry.key.required != "0"
            repository.link(
                parent_id,
                child,
                required=required,
                db=db,
                source=source,
                evidence={"parent_url": parent_entry.url, "child_url": entry.url},
            )
            children.append(child)
            if course_exam:
                if (
                    source != "course_exam_api"
                    or parent_entry.key.kind != "course"
                    or entry.key.kind != "exam"
                ):
                    raise ValueError("交卷历史关联必须来自明确的课程考试接口映射")
                _copy_exam_protection(repository, db, parent_id, child)
            if url in completed_urls:
                outcome = {
                    "schema_version": 1,
                    "task_id": child,
                    "status": "COMPLETED",
                    "reason_codes": [],
                    "child_refs": [],
                    "evidence_refs": [source],
                    "retry_policy": "manual",
                    "stop_scope": "task",
                    "details": {"observed_complete": True},
                }
                db.execute(
                    "INSERT OR REPLACE INTO task_local_outcomes VALUES(?,?,?)",
                    (child, "observation:" + source, encode(outcome)),
                )
                outcome = repository._aggregate(child, db)
                db.execute(
                    "UPDATE tasks SET status=?,outcome=?,updated_at=? WHERE task_id=?",
                    (outcome["status"], encode(outcome), now(), child),
                )
        repository.refresh_ancestors(children, db=db)
    return children


def _copy_exam_protection(repository, db, parent, child):
    old = db.execute(
        "SELECT payload FROM exam_history WHERE task_id=?", (parent,)
    ).fetchone()
    current = db.execute(
        "SELECT payload FROM exam_history WHERE task_id=?", (child,)
    ).fetchone()
    if old:
        history = merge_history(
            json.loads(current[0]) if current else {}, json.loads(old[0])
        )
        db.execute(
            "INSERT OR REPLACE INTO exam_history VALUES(?,?)", (child, encode(history))
        )
    for intent in list(
        db.execute(
            "SELECT * FROM action_intents WHERE task_id=? AND operation='submit' AND state='unverified'",
            (parent,),
        )
    ):
        action_id = hashlib.sha256((intent["action_id"] + child).encode()).hexdigest()
        baseline = json.loads(intent["baseline"])
        baseline["_association"] = {
            "source": "course_exam_api",
            "parent": parent,
            "original_action_id": intent["action_id"],
        }
        db.execute(
            "INSERT OR IGNORE INTO action_intents VALUES(?,?,?,'unverified',?,NULL,?)",
            (action_id, child, "submit", encode(baseline), intent["created_at"]),
        )
