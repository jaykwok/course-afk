"""Backed-up, additive repair of v1 snapshots and unproven relationships.

No server actions and no DBOS table changes. Unknown history is retained and
forces manual submission rather than silently weakening protection.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path

from core.tasks.identity import parse_entry
from core.tasks.records import merge_history, merge_queue
from core.tasks.repository import encode, now


def _archive(db, key, kind, payload):
    db.execute(
        "INSERT OR IGNORE INTO state_repairs VALUES(?,?,?,?)",
        (key, kind, encode(payload), now()),
    )


def _backup_sources(repo, links, db):
    sources = []
    for item in db.execute("SELECT * FROM imports"):
        source = Path(item["source"])
        if (
            source.name not in {"考试链接.json", "人工考试链接.json"}
            or item["fingerprint"] == "missing"
        ):
            continue
        candidates = [source]
        for root in (repo.path.parent / "backups", links.parent / "backups"):
            if root.exists():
                candidates.extend(root.rglob(source.name))
        for candidate in candidates:
            if not candidate.is_file():
                continue
            raw = candidate.read_bytes()
            if hashlib.sha256(raw).hexdigest() != item["fingerprint"]:
                continue
            rows = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(rows, list):
                raise ValueError("迁移原始快照不是队列数组")
            sources.append((dict(item), rows))
            break
        else:
            sources.append((dict(item), None))
    return sources


def repair_state(repo, links: Path, *, apply=False, recheck=False):
    with repo.connect() as db:
        if (
            not recheck
            and db.execute("SELECT 1 FROM schema_migrations WHERE version=2").fetchone()
        ):
            return {"already_repaired": True}
        sources = _backup_sources(repo, links, db)
        legacy_edges = [
            dict(r)
            for r in db.execute(
                "SELECT e.* FROM task_edges e WHERE NOT EXISTS(SELECT 1 FROM task_edge_evidence p WHERE p.parent=e.parent AND p.child=e.child)"
            )
        ]
    report = {
        "legacy_edges": len(legacy_edges),
        "source_snapshots": sum(rows is not None for _, rows in sources),
        "missing_source_snapshots": sum(rows is None for _, rows in sources),
        "quarantined_edges": 0,
        "quarantined_intents": 0,
        "restored_exam_records": 0,
    }
    if not apply:
        return report
    backup = (
        repo.path.parent
        / "backups"
        / ("repair-v2-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        / "state.sqlite"
    )
    repo.backup(backup)
    with repo.connect() as db:
        # Recheck under the write transaction, allowing idempotent interrupted repair.
        if (
            not recheck
            and db.execute("SELECT 1 FROM schema_migrations WHERE version=2").fetchone()
        ):
            return {"already_repaired": True}
        for edge in legacy_edges:
            parent = dict(
                db.execute(
                    "SELECT * FROM tasks WHERE task_id=?", (edge["parent"],)
                ).fetchone()
            )
            child = dict(
                db.execute(
                    "SELECT * FROM tasks WHERE task_id=?", (edge["child"],)
                ).fetchone()
            )
            p, c = json.loads(parent["identity"]), json.loads(child["identity"])
            structural = p["account"] == c["account"] and (
                c["kind"] in {"issue", "url_review", "section"}
                and c["business_id"].startswith(parent["task_id"] + ":")
                or c["kind"] == "course"
                and p["kind"] == "subject"
                and c["source_type"] == "6"
                and c["source_id"] == p["business_id"]
            )
            if structural:
                db.execute(
                    "INSERT INTO task_edge_evidence VALUES(?,?,?,?)",
                    (
                        edge["parent"],
                        edge["child"],
                        "structural_identity",
                        encode({"identity": c}),
                    ),
                )
                continue
            key = "legacy-edge:" + edge["parent"] + ":" + edge["child"]
            _archive(db, key, "unproven_edge", edge)
            db.execute(
                "DELETE FROM task_edges WHERE parent=? AND child=?",
                (edge["parent"], edge["child"]),
            )
            report["quarantined_edges"] += 1
            # A removed legacy edge may have been legitimate; its missing
            # provenance cannot certify the parent's work. Require a fresh
            # authorized verification without recreating cleared queue entries.
            previous_outcome = (
                json.loads(parent["outcome"]) if parent["outcome"] else {}
            )
            review = {
                **previous_outcome,
                "schema_version": 1,
                "task_id": parent["task_id"],
                "status": "NEEDS_REVIEW",
                "reason_codes": ["legacy_relationships_unverified"],
                "child_refs": [],
                "evidence_refs": [],
                "retry_policy": "manual",
                "stop_scope": "task",
                "details": {
                    **previous_outcome.get("details", {}),
                    "local_work_complete": False,
                },
            }
            run = db.execute(
                "SELECT run_id FROM runs WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
                (parent["task_id"],),
            ).fetchone()
            db.execute(
                "UPDATE tasks SET status=?,outcome=? WHERE task_id=?",
                (review["status"], encode(review), parent["task_id"]),
            )
            db.execute(
                "INSERT OR REPLACE INTO task_local_outcomes VALUES(?,?,?)",
                (parent["task_id"], run[0] if run else "legacy", encode(review)),
            )
            if p["kind"] == "course" and c["kind"] == "exam":
                for original in list(
                    db.execute(
                        "SELECT * FROM action_intents WHERE task_id=? AND operation='submit'",
                        (edge["parent"],),
                    )
                ):
                    copied_id = hashlib.sha256(
                        (original["action_id"] + edge["child"]).encode()
                    ).hexdigest()
                    copied = db.execute(
                        "SELECT * FROM action_intents WHERE action_id=? AND task_id=?",
                        (copied_id, edge["child"]),
                    ).fetchone()
                    if copied:
                        _archive(
                            db,
                            "legacy-intent:" + copied_id,
                            "unproven_copied_intent",
                            dict(copied),
                        )
                        db.execute(
                            "UPDATE action_intents SET state='quarantined' WHERE action_id=?",
                            (copied_id,),
                        )
                        report["quarantined_intents"] += 1
                history = db.execute(
                    "SELECT payload FROM exam_history WHERE task_id=?", (edge["child"],)
                ).fetchone()
                payload = json.loads(history[0]) if history else {}
                _archive(db, key + ":history", "uncertain_history", payload)
                payload["requires_manual_submit"] = True
                payload.setdefault("association_review", {})[edge["parent"]] = (
                    "legacy_mapping_unverified"
                )
                db.execute(
                    "INSERT OR REPLACE INTO exam_history VALUES(?,?)",
                    (edge["child"], encode(payload)),
                )
        for source, rows in sources:
            if rows is None:
                _archive(
                    db,
                    "missing-source:" + source["source"],
                    "missing_original_snapshot",
                    source,
                )
                continue
            for raw in rows:
                if not isinstance(raw, dict) or not raw.get("url"):
                    raise ValueError("原始队列条目损坏，修复事务已回滚")
                protections = {
                    "failed_models": raw.get("ai_failed_model_configs", []),
                    "requires_manual_submit": bool(raw.get("requires_manual_submit")),
                }
                if (
                    not protections["failed_models"]
                    and not protections["requires_manual_submit"]
                ):
                    continue
                entry = parse_entry(raw["url"], "unbound")
                # A queue file did not record owner in v1. Match its surviving
                # identity if unique; otherwise preserve under unbound for review.
                candidates = [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM tasks WHERE url=? AND kind=?",
                        (entry.url, entry.key.kind),
                    )
                ]
                target = (
                    candidates[0]["task_id"]
                    if len(candidates) == 1
                    else repo.upsert(entry, db=db)
                )
                old = db.execute(
                    "SELECT payload FROM exam_history WHERE task_id=?", (target,)
                ).fetchone()
                merged = merge_history(json.loads(old[0]) if old else {}, protections)
                if not old or encode(merged) != old[0]:
                    report["restored_exam_records"] += 1
                db.execute(
                    "INSERT OR REPLACE INTO exam_history VALUES(?,?)",
                    (target, encode(merged)),
                )
                for item in list(
                    db.execute(
                        "SELECT * FROM queue_items WHERE task_id=? AND collection=?",
                        (target, Path(source["source"]).name),
                    )
                ):
                    payload = merge_queue(
                        json.loads(item["payload"]), {**raw, "url": entry.url}
                    )
                    db.execute(
                        "UPDATE queue_items SET payload=? WHERE collection=? AND task_id=? AND item_key=?",
                        (encode(payload), item["collection"], target, item["item_key"]),
                    )
        # Seed local phase results so valid legacy parents can converge after
        # child transitions. Unknown discovery completeness remains reviewable.
        for task in list(db.execute("SELECT * FROM tasks WHERE outcome IS NOT NULL")):
            if db.execute(
                "SELECT 1 FROM task_local_outcomes WHERE task_id=?", (task["task_id"],)
            ).fetchone():
                continue
            outcome = json.loads(task["outcome"])
            if not isinstance(outcome, dict) or "status" not in outcome:
                continue
            run = db.execute(
                "SELECT run_id,kind FROM runs WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
                (task["task_id"],),
            ).fetchone()
            if (
                run
                and run["kind"] == "discover"
                and "discovery_complete" not in outcome.get("details", {})
            ):
                outcome = {
                    **outcome,
                    "status": "NEEDS_REVIEW",
                    "reason_codes": list(
                        dict.fromkeys(
                            outcome.get("reason_codes", []) + ["discovery_unverified"]
                        )
                    ),
                    "details": {
                        **outcome.get("details", {}),
                        "discovery_complete": False,
                    },
                }
                db.execute(
                    "UPDATE tasks SET status=?,outcome=? WHERE task_id=?",
                    (outcome["status"], encode(outcome), task["task_id"]),
                )
            db.execute(
                "INSERT INTO task_local_outcomes VALUES(?,?,?)",
                (task["task_id"], run["run_id"] if run else "legacy", encode(outcome)),
            )
        repo.refresh_ancestors(
            [r[0] for r in db.execute("SELECT task_id FROM tasks")], db=db
        )
        db.execute("INSERT OR REPLACE INTO schema_migrations VALUES(2,?)", (now(),))
        db.execute(
            "INSERT OR REPLACE INTO state_repairs VALUES(?,?,?,?)",
            ("v2-summary", "repair_summary", encode(report), now()),
        )
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("修复关系校验失败，事务已回滚")
    return {**report, "backup": str(backup)}
