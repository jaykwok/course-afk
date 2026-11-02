"""Dry-run first, backup first, one transaction for all legacy business data."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil

from core.tasks.identity import parse_entry
from core.tasks.repository import TaskRepository, encode, now

FILES = ("课程链接.json", "挂课失败链接.json", "考试链接.json", "人工考试链接.json")


def _history_source(path):
    if not path.exists():
        return {"version": 2, "entries": {}}, "missing"
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8-sig"))
    if (
        not isinstance(value, dict)
        or value.get("version") not in {1, 2}
        or not isinstance(value.get("entries"), dict)
    ):
        raise ValueError("考试历史格式损坏，停止迁移以保护记录")
    for entry in value["entries"].values():
        if not isinstance(entry, dict) or not isinstance(
            entry.get("failed_models", []), list
        ):
            raise ValueError("考试历史条目损坏，停止迁移")
        if value["version"] == 1:
            previous = entry.pop("submission", None)
            if previous:
                entry["submissions"] = {
                    (previous.get("model_config") or {}).get("account")
                    or "unbound": previous
                }
        if not isinstance(entry.get("submissions", {}), dict):
            raise ValueError("考试历史提交字段损坏，停止迁移")
    value["version"] = 2
    return value, hashlib.sha256(raw).hexdigest()


def import_history(
    repo, path, *, account="unbound", db=None, expected_fingerprint=None
):
    if db is None:
        with repo.connect() as connection:
            return import_history(
                repo,
                path,
                account=account,
                db=connection,
                expected_fingerprint=expected_fingerprint,
            )
    source = str(path.resolve())
    if db.execute("SELECT 1 FROM imports WHERE source=?", (source,)).fetchone():
        return
    data, fingerprint = _history_source(path)
    if expected_fingerprint is not None and fingerprint != expected_fingerprint:
        raise RuntimeError("迁移期间考试历史发生变化，导入事务已回滚")
    for url, entry in data["entries"].items():
        key = parse_entry(url, account)
        task_id = repo.upsert(key, db=db)
        previous = db.execute(
            "SELECT payload FROM exam_history WHERE task_id=?", (task_id,)
        ).fetchone()
        merged = json.loads(previous[0]) if previous else {}
        merged["requires_manual_submit"] = bool(
            merged.get("requires_manual_submit") or entry.get("requires_manual_submit")
        )
        merged["failed_models"] = list(
            {
                encode(c): c
                for c in merged.get("failed_models", [])
                + entry.get("failed_models", [])
            }.values()
        )
        submissions = merged.setdefault("submissions", {})
        for owner, submission in entry.get("submissions", {}).items():
            existing = submissions.get(owner)
            if not existing or existing == submission:
                submissions[owner] = submission
            else:
                # Preserve every ambiguous legacy attempt; no latest-wins loss.
                merged.setdefault("legacy_submissions", []).append(
                    {"account": owner, **submission}
                )
            if submission.get("state") == "unverified":
                action_id = hashlib.sha256(
                    (source + url + owner + encode(submission)).encode()
                ).hexdigest()
                db.execute(
                    "INSERT OR IGNORE INTO action_intents VALUES(?,?,?,'unverified',?,NULL,?)",
                    (action_id, task_id, "submit", encode(submission), now()),
                )
        db.execute(
            "INSERT OR REPLACE INTO exam_history VALUES(?,?)", (task_id, encode(merged))
        )
    db.execute("INSERT INTO imports VALUES(?,?,?)", (source, fingerprint, now()))


def dry_run(links: Path, *, account="unbound") -> dict:
    from core.tasks.store import _read_source

    entries = {}
    identities = set()
    for name in FILES:
        rows, fingerprint = _read_source(links / name)
        invalid = [r for r in rows if not isinstance(r, dict) or not r.get("url")]
        if invalid:
            raise ValueError(f"{name} 含 {len(invalid)} 个无法迁移的条目；源文件保留")
        entries[name] = {"count": len(rows), "fingerprint": fingerprint}
        identities.update(parse_entry(r["url"], account).key.task_id for r in rows)
    history, history_fingerprint = _history_source(links / "exam-history.json")
    return {
        "files": entries,
        "unique_tasks": len(identities),
        "alias_merges": sum(v["count"] for v in entries.values()) - len(identities),
        "account": account,
        "unbound": account == "unbound",
        "history_entries": len(history["entries"]),
        "history_fingerprint": history_fingerprint,
        "pending_submissions": sum(
            s.get("state") == "unverified"
            for e in history["entries"].values()
            for s in e.get("submissions", {}).values()
        ),
    }


def migrate(links: Path, repo: TaskRepository, *, account="unbound") -> dict:
    from core.tasks.store import import_queue

    report = dry_run(links, account=account)
    backup = repo.path.parent / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup.mkdir(parents=True)
    for name in (*FILES, "exam-history.json"):
        source = links / name
        if source.exists():
            shutil.copy2(source, backup / name)
    repo.backup(backup / "state.sqlite")
    with repo.connect() as db:
        if db.execute("SELECT 1 FROM runs WHERE dispatch='DISPATCHED'").fetchone():
            raise RuntimeError("存在活动执行，不能切换存储")
        for name in FILES:
            import_queue(
                links / name,
                account=account,
                db=db,
                repository=repo,
                expected_fingerprint=report["files"][name]["fingerprint"],
            )
        import_history(
            repo,
            links / "exam-history.json",
            account=account,
            db=db,
            expected_fingerprint=report["history_fingerprint"],
        )
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("迁移关系校验失败")
    return {**report, "backup": str(backup)}


def initialize_store():
    from core.config import DATA_DIR, LINKS_DIR

    repo = TaskRepository(DATA_DIR / "workflows" / "state.sqlite")
    sources = [
        str((LINKS_DIR / name).resolve()) for name in (*FILES, "exam-history.json")
    ]
    with repo.connect() as db:
        count = db.execute(
            "SELECT count(*) FROM imports WHERE source IN (?,?,?,?,?)", sources
        ).fetchone()[0]
    if count < len(sources):
        migrate(LINKS_DIR, repo)
    from core.tasks.repair import repair_state

    result = repair_state(repo, LINKS_DIR, apply=True)
    if result.get("missing_source_snapshots"):
        import logging

        logging.warning(
            "部分迁移原始快照缺失，无法证明失败模型历史已全部补齐；请查看 workflow_state.py repair-check，补回原备份后运行 repair。"
        )
    return result


def bind_unbound(repo: TaskRepository, account: str) -> int:
    """Explicit operator action under application_lock; never inferred at login."""
    from dataclasses import replace
    from core.tasks.identity import TaskKey, Entry

    if not account or account == "unbound":
        raise ValueError("需要当前可验证的账号标识")
    with repo.connect() as db:
        if db.execute("SELECT 1 FROM runs WHERE dispatch!='FINISHED'").fetchone():
            raise RuntimeError("存在未结束执行，请先核实执行状态")
        rows = list(db.execute("SELECT * FROM tasks WHERE account='unbound'"))
        mapping = {}
        keys = {
            row["task_id"]: replace(
                TaskKey(**json.loads(row["identity"])), account=account
            )
            for row in rows
        }
        # Composite child identities contain their parent's ID. Re-key the
        # parent first, otherwise later section/issue resolution misses them.
        for old_id, key in keys.items():
            if key.kind not in {"issue", "section", "url_review"}:
                mapping[old_id] = key.task_id
        for old_id, key in list(keys.items()):
            if key.kind in {"issue", "section", "url_review"}:
                parent, separator, suffix = key.business_id.partition(":")
                if parent in mapping and separator:
                    keys[old_id] = replace(
                        key, business_id=mapping[parent] + ":" + suffix
                    )
            mapping[old_id] = keys[old_id].task_id
        for row in rows:
            key = keys[row["task_id"]]
            existed = db.execute(
                "SELECT 1 FROM tasks WHERE task_id=?", (key.task_id,)
            ).fetchone()
            task_id = repo.upsert(Entry(key, row["url"], (row["url"],)), db=db)
            if not existed:
                db.execute(
                    "UPDATE tasks SET status=?,outcome=? WHERE task_id=?",
                    (row["status"], row["outcome"], task_id),
                )
            for alias in list(
                db.execute(
                    "SELECT alias FROM task_aliases WHERE task_id=?", (row["task_id"],)
                )
            ):
                db.execute(
                    "INSERT OR IGNORE INTO task_aliases VALUES(?,?,?)",
                    (account, alias[0], task_id),
                )
            for item in list(
                db.execute(
                    "SELECT * FROM queue_items WHERE task_id=?", (row["task_id"],)
                )
            ):
                db.execute(
                    "INSERT OR IGNORE INTO queue_items VALUES(?,?,?,?,?)",
                    (
                        item["collection"],
                        task_id,
                        item["item_key"],
                        item["payload"],
                        item["position"],
                    ),
                )
            db.execute("DELETE FROM queue_items WHERE task_id=?", (row["task_id"],))
            db.execute(
                "UPDATE action_intents SET task_id=? WHERE task_id=?",
                (task_id, row["task_id"]),
            )
            history = db.execute(
                "SELECT payload FROM exam_history WHERE task_id=?", (row["task_id"],)
            ).fetchone()
            if history:
                previous = db.execute(
                    "SELECT payload FROM exam_history WHERE task_id=?", (task_id,)
                ).fetchone()
                if previous:
                    raise ValueError(
                        "目标账号已有同一考试历史，请逐项合并，绑定事务已回滚"
                    )
                db.execute(
                    "INSERT INTO exam_history VALUES(?,?)", (task_id, history[0])
                )
                db.execute(
                    "DELETE FROM exam_history WHERE task_id=?", (row["task_id"],)
                )
        for edge in list(db.execute("SELECT * FROM task_edges")):
            parent, child = (
                mapping.get(edge["parent"], edge["parent"]),
                mapping.get(edge["child"], edge["child"]),
            )
            if parent != edge["parent"] or child != edge["child"]:
                evidence = list(
                    db.execute(
                        "SELECT * FROM task_edge_evidence WHERE parent=? AND child=?",
                        (edge["parent"], edge["child"]),
                    )
                )
                for proof in evidence:
                    repo.link(
                        parent,
                        child,
                        required=edge["required"],
                        db=db,
                        source=proof["source"],
                        evidence=json.loads(proof["evidence"]),
                    )
                if not evidence:
                    repo.link(parent, child, required=edge["required"], db=db)
                db.execute(
                    "DELETE FROM task_edge_evidence WHERE parent=? AND child=?",
                    (edge["parent"], edge["child"]),
                )
                db.execute(
                    "DELETE FROM task_edges WHERE parent=? AND child=?",
                    (edge["parent"], edge["child"]),
                )
        for row in rows:
            for dismissal in list(
                db.execute(
                    "SELECT * FROM task_dismissals WHERE task_id=?", (row["task_id"],)
                )
            ):
                db.execute(
                    "INSERT OR IGNORE INTO task_dismissals VALUES(?,?,?)",
                    (mapping[row["task_id"]], dismissal["scope"], dismissal["at"]),
                )
            db.execute("DELETE FROM task_dismissals WHERE task_id=?", (row["task_id"],))
            local = db.execute(
                "SELECT * FROM task_local_outcomes WHERE task_id=?", (row["task_id"],)
            ).fetchone()
            if local:
                payload = json.loads(local["payload"])
                payload["task_id"] = mapping[row["task_id"]]
                db.execute(
                    "INSERT OR IGNORE INTO task_local_outcomes VALUES(?,?,?)",
                    (mapping[row["task_id"]], local["run_id"], encode(payload)),
                )
                db.execute(
                    "DELETE FROM task_local_outcomes WHERE task_id=?", (row["task_id"],)
                )
            if not db.execute(
                "SELECT 1 FROM runs WHERE task_id=?", (row["task_id"],)
            ).fetchone():
                db.execute(
                    "DELETE FROM task_aliases WHERE task_id=?", (row["task_id"],)
                )
                db.execute("DELETE FROM tasks WHERE task_id=?", (row["task_id"],))
        return len(rows)
