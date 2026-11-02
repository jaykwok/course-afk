"""SQLite business transactions. Never reads or edits DBOS internal tables."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
import uuid

from core.tasks.identity import Entry


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class TaskRepository:
    _initialized = set()
    _schema_lock = threading.RLock()

    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._schema_lock:
            if self.path in self._initialized and self.path.exists():
                return
            self._initialize()
            self._initialized.add(self.path)

    def _initialize(self):
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks(
                    task_id TEXT PRIMARY KEY, identity TEXT NOT NULL UNIQUE, account TEXT NOT NULL,
                    kind TEXT NOT NULL, url TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'READY',
                    outcome TEXT, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS task_aliases(
                    account TEXT NOT NULL, alias TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES tasks,
                    PRIMARY KEY(account,alias,task_id));
                CREATE TABLE IF NOT EXISTS task_edges(
                    parent TEXT NOT NULL REFERENCES tasks, child TEXT NOT NULL REFERENCES tasks,
                    required INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(parent,child));
                CREATE TABLE IF NOT EXISTS runs(
                    run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks, kind TEXT NOT NULL,
                    version TEXT NOT NULL, payload TEXT NOT NULL, dispatch TEXT NOT NULL,
                    outcome TEXT, created_at TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs(task_id,kind)
                    WHERE dispatch IN ('PLANNED','DISPATCHED','INTERRUPTED','RESULT_SAVED');
                CREATE TABLE IF NOT EXISTS action_intents(
                    action_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks,
                    operation TEXT NOT NULL, state TEXT NOT NULL, baseline TEXT NOT NULL,
                    receipt TEXT, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS phases(
                    run_id TEXT NOT NULL REFERENCES runs, phase TEXT NOT NULL, started_at TEXT NOT NULL,
                    budget REAL NOT NULL, consumed REAL NOT NULL DEFAULT 0, PRIMARY KEY(run_id,phase));
                CREATE TABLE IF NOT EXISTS queue_items(
                    collection TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES tasks,
                    item_key TEXT NOT NULL, payload TEXT NOT NULL, position INTEGER NOT NULL,
                    PRIMARY KEY(collection,task_id,item_key));
                CREATE TABLE IF NOT EXISTS exam_history(
                    task_id TEXT PRIMARY KEY REFERENCES tasks, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS task_edge_evidence(
                    parent TEXT NOT NULL REFERENCES tasks, child TEXT NOT NULL REFERENCES tasks,
                    source TEXT NOT NULL, evidence TEXT NOT NULL, PRIMARY KEY(parent,child,source));
                CREATE TABLE IF NOT EXISTS task_local_outcomes(
                    task_id TEXT PRIMARY KEY REFERENCES tasks, run_id TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS task_dismissals(
                    task_id TEXT NOT NULL REFERENCES tasks, scope TEXT NOT NULL, at TEXT NOT NULL,
                    PRIMARY KEY(task_id,scope));
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def upsert(self, entry: Entry, *, db=None) -> str:
        if db is None:
            with self.connect() as connection:
                return self.upsert(entry, db=connection)
        key = entry.key
        db.execute(
            "INSERT OR IGNORE INTO tasks(task_id,identity,account,kind,url,updated_at) VALUES(?,?,?,?,?,?)",
            (key.task_id, encode(asdict(key)), key.account, key.kind, entry.url, now()),
        )
        db.executemany(
            "INSERT OR IGNORE INTO task_aliases VALUES(?,?,?)",
            [(key.account, alias, key.task_id) for alias in entry.aliases],
        )
        return key.task_id

    def task(self, task_id: str) -> dict:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            return dict(row)

    def children(self, task_id: str) -> list[dict]:
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT t.*,e.required FROM task_edges e JOIN tasks t ON t.task_id=e.child WHERE e.parent=?",
                    (task_id,),
                )
            ]

    def learning_reviews(self, account: str) -> list[str]:
        with self.connect() as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT p.url FROM task_edges e JOIN tasks p ON p.task_id=e.parent JOIN tasks c ON c.task_id=e.child WHERE p.account=? AND c.kind='url_review' AND c.status NOT IN ('COMPLETED','DISMISSED') AND NOT EXISTS(SELECT 1 FROM task_dismissals d WHERE d.task_id=p.task_id AND d.scope='learn')",
                    (account,),
                )
            ]

    def link(
        self,
        parent: str,
        child: str,
        *,
        required=True,
        db=None,
        source="explicit",
        evidence=None,
    ):
        if parent == child:
            return
        if db is None:
            with self.connect() as connection:
                return self.link(
                    parent,
                    child,
                    required=required,
                    db=connection,
                    source=source,
                    evidence=evidence,
                )
        if db.execute(
            "WITH RECURSIVE descendants(id) AS (SELECT child FROM task_edges WHERE parent=? UNION SELECT e.child FROM task_edges e JOIN descendants d ON e.parent=d.id) SELECT 1 FROM descendants WHERE id=?",
            (child, parent),
        ).fetchone():
            raise ValueError("任务关系不能形成循环")
        owners = list(
            db.execute(
                "SELECT account FROM tasks WHERE task_id IN (?,?)", (parent, child)
            )
        )
        if len(owners) != 2 or owners[0][0] != owners[1][0]:
            raise ValueError("父子任务必须属于同一账号")
        db.execute(
            "INSERT INTO task_edges VALUES(?,?,?) ON CONFLICT(parent,child) DO UPDATE SET required=MAX(required,excluded.required)",
            (parent, child, int(required)),
        )
        db.execute(
            "INSERT OR REPLACE INTO task_edge_evidence VALUES(?,?,?,?)",
            (parent, child, source, encode(evidence or {})),
        )

    def plan(self, task_id: str, kind: str, options: dict, version: str) -> dict:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM runs WHERE task_id=? AND kind=? AND dispatch IN ('PLANNED','DISPATCHED','INTERRUPTED','RESULT_SAVED')",
                (task_id, kind),
            ).fetchone()
            if row:
                return dict(row)
            run_id = str(uuid.uuid4())
            task = db.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            payload = {
                "schema_version": 1,
                "run_id": run_id,
                "task_id": task_id,
                "account": task["account"],
                "kind": kind,
                "options": options,
            }
            previous = db.execute(
                "SELECT run_id FROM runs WHERE task_id=? AND kind=? AND dispatch='INCOMPATIBLE' ORDER BY created_at DESC LIMIT 1",
                (task_id, kind),
            ).fetchone()
            if previous:
                payload["previous_run_id"] = previous[0]
            db.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,?,NULL,?)",
                (run_id, task_id, kind, version, encode(payload), "PLANNED", now()),
            )
            return dict(
                db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            )

    def run(self, run_id: str) -> dict:
        with self.connect() as db:
            return dict(
                db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            )

    def dispatch(self, run_id: str, state: str):
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET dispatch=? WHERE run_id=? AND outcome IS NULL",
                (state, run_id),
            )
            if state == "INTERRUPTED":
                db.execute(
                    "UPDATE tasks SET status='INTERRUPTED',updated_at=? WHERE task_id IN (SELECT task_id FROM runs WHERE run_id=? AND outcome IS NULL)",
                    (now(), run_id),
                )

    def save_outcome(self, run_id: str, outcome: dict, *, finalized=False):
        with self.connect() as db:
            previous = db.execute(
                "SELECT run_id FROM task_local_outcomes WHERE task_id=?",
                (outcome["task_id"],),
            ).fetchone()
            if not finalized or previous is None or previous[0] != run_id:
                db.execute(
                    "INSERT OR REPLACE INTO task_local_outcomes VALUES(?,?,?)",
                    (outcome["task_id"], run_id, encode(outcome)),
                )
            outcome = self._aggregate(outcome["task_id"], db)
            value = encode(outcome)
            db.execute(
                "UPDATE tasks SET status=?,outcome=?,updated_at=? WHERE task_id=?",
                (outcome["status"], value, now(), outcome["task_id"]),
            )
            db.execute(
                "UPDATE runs SET dispatch=?,outcome=? WHERE run_id=?",
                ("FINISHED" if finalized else "RESULT_SAVED", value, run_id),
            )
            run_kind = db.execute(
                "SELECT kind FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            if (
                outcome["status"] == "COMPLETED"
                or outcome.get("stop_scope") != "batch"
                and (
                    outcome["status"] in {"WAITING_CHILDREN", "BLOCKED", "NEEDS_REVIEW"}
                    or run_kind == "exam"
                    and outcome["status"] == "WAITING_SYNC"
                )
            ):
                collection = (
                    "考试链接.json"
                    if run_kind == "exam"
                    else "课程链接.json"
                    if run_kind == "learn"
                    else None
                )
                if collection:
                    db.execute(
                        "DELETE FROM queue_items WHERE task_id=? AND collection=?",
                        (outcome["task_id"], collection),
                    )
            self.refresh_ancestors({outcome["task_id"]}, db=db)
        return outcome

    def _aggregate(self, task_id, db):
        row = db.execute(
            "SELECT payload FROM task_local_outcomes WHERE task_id=?", (task_id,)
        ).fetchone()
        if not row:
            return None
        own = json.loads(row[0])
        # An incomplete discovery or independent manual work must not be
        # completed merely because its currently known children have finished.
        local_complete = own["status"] == "COMPLETED" or (
            own["status"] == "WAITING_CHILDREN"
            and own.get("details", {}).get(
                "local_work_complete", not own.get("reason_codes")
            )
            and own.get("details", {}).get("discovery_complete", True)
        )
        if not local_complete:
            return own
        children = [
            r[0]
            for r in db.execute(
                "SELECT t.task_id FROM task_edges e JOIN tasks t ON t.task_id=e.child WHERE e.parent=? AND e.required=1 AND t.status!='COMPLETED'",
                (task_id,),
            )
        ]
        return {
            **own,
            "status": "WAITING_CHILDREN" if children else "COMPLETED",
            "child_refs": children,
            "reason_codes": own.get("reason_codes", []) if children else [],
        }

    def refresh_ancestors(self, changed, *, db=None):
        if db is None:
            with self.connect() as connection:
                return self.refresh_ancestors(changed, db=connection)
        affected = set()
        frontier = list(changed)
        while frontier:
            for parent in db.execute(
                "SELECT parent FROM task_edges WHERE child=?", (frontier.pop(),)
            ):
                if parent[0] not in affected:
                    affected.add(parent[0])
                    frontier.append(parent[0])
        # Aggregate children before parents, including diamonds/multiple roots.
        dependencies = {
            task: {
                r[0]
                for r in db.execute(
                    "SELECT child FROM task_edges WHERE parent=?", (task,)
                )
            }
            & affected
            for task in affected
        }
        ready = [task for task, children in dependencies.items() if not children]
        while ready:
            parent = ready.pop()
            row = db.execute(
                "SELECT status,outcome FROM tasks WHERE task_id=?", (parent,)
            ).fetchone()
            if row["status"] not in {"RUNNING", "DISMISSED"}:
                outcome = self._aggregate(parent, db)
                if outcome is not None and (
                    row["status"] != outcome["status"]
                    or row["outcome"] != encode(outcome)
                ):
                    db.execute(
                        "UPDATE tasks SET status=?,outcome=?,updated_at=? WHERE task_id=?",
                        (outcome["status"], encode(outcome), now(), parent),
                    )
            dependencies.pop(parent)
            for ancestor, children in dependencies.items():
                if parent in children:
                    children.remove(parent)
                    if not children:
                        ready.append(ancestor)

    def dismiss(self, task_ids, scope, *, db):
        for task_id in task_ids:
            db.execute(
                "INSERT OR REPLACE INTO task_dismissals VALUES(?,?,?)",
                (task_id, scope, now()),
            )
            run_kinds = ("learn", "discover") if scope == "learn" else ("exam",)
            for kind in run_kinds:
                db.execute(
                    "UPDATE runs SET dispatch='DISMISSED' WHERE task_id=? AND kind=? AND dispatch IN ('PLANNED','INTERRUPTED','DISPATCHED','RESULT_SAVED')",
                    (task_id, kind),
                )
            db.execute(
                "UPDATE tasks SET status='DISMISSED',updated_at=? WHERE task_id=? AND status!='COMPLETED'",
                (now(), task_id),
            )
            # Removing an issue withdraws that obligation; it supplies no
            # completion evidence. Explicit rediscovery restores required=1.
            db.execute(
                "UPDATE task_edges SET required=0 WHERE child=? AND EXISTS(SELECT 1 FROM tasks t WHERE t.task_id=child AND t.kind IN ('issue','url_review'))",
                (task_id,),
            )

    def select(self, task_id, scope, *, db):
        db.execute(
            "DELETE FROM task_dismissals WHERE task_id=? AND scope=?", (task_id, scope)
        )
        db.execute(
            "UPDATE tasks SET status='READY',updated_at=? WHERE task_id=? AND status='DISMISSED'",
            (now(), task_id),
        )

    def running(self, task_id: str):
        with self.connect() as db:
            db.execute(
                "UPDATE tasks SET status='RUNNING',updated_at=? WHERE task_id=?",
                (now(), task_id),
            )

    def incompatible(self, run_id: str, outcome: dict):
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET dispatch='INCOMPATIBLE',outcome=? WHERE run_id=?",
                (encode(outcome), run_id),
            )
            db.execute(
                "UPDATE tasks SET status='NEEDS_REVIEW',outcome=?,updated_at=? WHERE task_id=?",
                (encode(outcome), now(), outcome["task_id"]),
            )
        return outcome

    def intent(self, task_id: str, operation: str, baseline: dict) -> dict:
        with self.connect() as db:
            unresolved = db.execute(
                "SELECT * FROM action_intents WHERE task_id=? AND operation=? AND state='unverified' ORDER BY created_at",
                (task_id, operation),
            ).fetchall()
            if unresolved:
                return dict(unresolved[0])
            action_id = str(uuid.uuid4())
            db.execute(
                "INSERT INTO action_intents VALUES(?,?,?,'unverified',?,NULL,?)",
                (action_id, task_id, operation, encode(baseline), now()),
            )
            return dict(
                db.execute(
                    "SELECT * FROM action_intents WHERE action_id=?", (action_id,)
                ).fetchone()
            )

    def pending_intents(self, task_id: str, operation: str) -> list[dict]:
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM action_intents WHERE task_id=? AND operation=? AND state='unverified'",
                    (task_id, operation),
                )
            ]

    def confirm_intent(self, action_id: str, state: str, receipt: dict):
        with self.connect() as db:
            db.execute(
                "UPDATE action_intents SET state=?,receipt=? WHERE action_id=?",
                (state, encode(receipt), action_id),
            )

    def backup(self, target: Path):
        target.parent.mkdir(parents=True, exist_ok=True)
        destination = sqlite3.connect(target)
        try:
            with self.connect() as source:
                source.backup(destination)
        finally:
            destination.close()

    def export(self) -> dict:
        with self.connect() as db:
            return {
                table: [dict(r) for r in db.execute(f"SELECT * FROM {table}")]
                for table in (
                    "tasks",
                    "task_aliases",
                    "task_edges",
                    "runs",
                    "action_intents",
                    "phases",
                    "queue_items",
                    "exam_history",
                    "task_edge_evidence",
                    "task_local_outcomes",
                    "task_dismissals",
                )
            }
