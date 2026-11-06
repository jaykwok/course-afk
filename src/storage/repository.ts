import { DatabaseSync, backup } from "node:sqlite";
import { existsSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { entry, type Entry } from "../domain/identity.ts";
import { token } from "../utils.ts";

export type Queue = "learn" | "exam" | "review" | "failure" | "discover";
export type Status =
  | "READY"
  | "RUNNING"
  | "COMPLETED"
  | "WAITING_CHILDREN"
  | "WAITING_SYNC"
  | "NEEDS_REVIEW"
  | "FAILED"
  | "INTERRUPTED";
export interface Task extends Entry {
  status: Status;
  details: Record<string, any>;
  manualSubmit: boolean;
}
export interface QueueItem extends Task {
  queue: Queue;
  reason: string;
}
const APPLICATION_ID = 1128351307;
const SCHEMA = 1;
const sql = `
CREATE TABLE tasks(id TEXT PRIMARY KEY, account TEXT NOT NULL, kind TEXT NOT NULL, url TEXT NOT NULL,
 identity TEXT NOT NULL, status TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}', manual_submit INTEGER NOT NULL DEFAULT 0);
CREATE TABLE queues(task_id TEXT NOT NULL REFERENCES tasks(id), queue TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', position INTEGER NOT NULL,
 PRIMARY KEY(task_id,queue,reason));
CREATE TABLE edges(parent TEXT NOT NULL REFERENCES tasks(id), child TEXT NOT NULL REFERENCES tasks(id), required INTEGER NOT NULL, PRIMARY KEY(parent,child));
CREATE TABLE runs(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), kind TEXT NOT NULL, status TEXT NOT NULL, outcome TEXT, created_at TEXT NOT NULL);
CREATE UNIQUE INDEX active_run ON runs(task_id,kind) WHERE status='RUNNING';
CREATE TABLE intents(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), operation TEXT NOT NULL, state TEXT NOT NULL, baseline TEXT NOT NULL, receipt TEXT, created_at TEXT NOT NULL);
CREATE UNIQUE INDEX pending_intent ON intents(task_id,operation) WHERE state='PENDING';
CREATE TABLE exam_history(task_id TEXT PRIMARY KEY REFERENCES tasks(id), payload TEXT NOT NULL);
`;

export class Repository {
  readonly db: DatabaseSync;
  readonly directory: string;
  constructor(directory: string) {
    this.directory = directory;
    mkdirSync(directory, { recursive: true });
    const file = join(directory, "workbench.sqlite");
    const existing = existsSync(file);
    // Inspect an existing database read-only before enabling WAL or making any writes.
    if (existing) {
      const probe = new DatabaseSync(file, { readOnly: true });
      try {
        if (
          Number(
            probe.prepare("PRAGMA application_id").get()?.application_id,
          ) !== APPLICATION_ID ||
          Number(probe.prepare("PRAGMA user_version").get()?.user_version) !==
            SCHEMA
        )
          throw new Error("工作台数据库版本不匹配");
      } finally {
        probe.close();
      }
    }
    this.db = new DatabaseSync(file);
    this.db.exec(
      "PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000; PRAGMA journal_mode=WAL;",
    );
    if (!existing)
      this.transaction(() => {
        this.db.exec(sql);
        this.db.exec(
          `PRAGMA application_id=${APPLICATION_ID}; PRAGMA user_version=${SCHEMA}`,
        );
      });
    this.transaction(() => {
      this.db.exec(
        "UPDATE runs SET status='INTERRUPTED' WHERE status='RUNNING'; UPDATE tasks SET status='INTERRUPTED' WHERE status='RUNNING';",
      );
      // A crash cannot convert an uncertain submission into a fresh attempt.
      this.db.exec(
        "UPDATE tasks SET manual_submit=1,status='NEEDS_REVIEW' WHERE id IN (SELECT task_id FROM intents WHERE operation='submit' AND state='PENDING')",
      );
    });
  }
  transaction<T>(fn: () => T): T {
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const result = fn();
      this.db.exec("COMMIT");
      return result;
    } catch (e) {
      this.db.exec("ROLLBACK");
      throw e;
    }
  }
  close() {
    this.db.close();
  }
  upsert(url: string, account: string): Task {
    if (!account || account === "unbound") throw new Error("请先核验登录账号");
    const e = entry(url, account);
    this.db
      .prepare(
        "INSERT INTO tasks(id,account,kind,url,identity,status) VALUES(?,?,?,?,?,'READY') ON CONFLICT(id) DO NOTHING",
      )
      .run(e.id, account, e.kind, e.url, JSON.stringify(e));
    return this.task(e.id);
  }
  task(id: string): Task {
    const row = this.db.prepare("SELECT * FROM tasks WHERE id=?").get(id);
    if (!row) throw new Error("任务不存在");
    return {
      ...JSON.parse(String(row.identity)),
      status: row.status as Status,
      details: JSON.parse(String(row.details)),
      manualSubmit: !!row.manual_submit,
    };
  }
  queue(account: string, queue: Queue): QueueItem[] {
    return this.db
      .prepare(
        "SELECT t.id,q.reason FROM queues q JOIN tasks t ON t.id=q.task_id WHERE t.account=? AND q.queue=? ORDER BY q.position,q.rowid",
      )
      .all(account, queue)
      .map((r) => ({
        ...this.task(String(r.id)),
        queue,
        reason: String(r.reason),
      }));
  }
  enqueue(task: Task, queue: Queue, reason = "") {
    this.db
      .prepare("INSERT OR IGNORE INTO queues VALUES(?,?,?,?)")
      .run(task.id, queue, reason, Date.now());
  }
  remove(id: string, queue: Queue) {
    this.db
      .prepare("DELETE FROM queues WHERE task_id=? AND queue=?")
      .run(id, queue);
  }
  outcome(id: string, status: Status, details: Record<string, any> = {}) {
    this.db
      .prepare("UPDATE tasks SET status=?,details=? WHERE id=?")
      .run(
        status,
        JSON.stringify({ ...this.task(id).details, ...details }),
        id,
      );
  }
  review(
    task: Task,
    reason: string,
    details: Record<string, any> = {},
    manual = false,
  ) {
    this.transaction(() => {
      this.outcome(task.id, "NEEDS_REVIEW", { ...details, reason });
      if (manual)
        this.db
          .prepare("UPDATE tasks SET manual_submit=1 WHERE id=?")
          .run(task.id);
      this.enqueue(task, task.kind === "exam" ? "review" : "failure", reason);
      if (task.kind === "exam") this.remove(task.id, "exam");
    });
  }
  complete(task: Task, evidence: Record<string, unknown>) {
    this.transaction(() => {
      this.outcome(task.id, "COMPLETED", { evidence });
      this.db.prepare("DELETE FROM queues WHERE task_id=?").run(task.id);
    });
    this.refreshParents(task.id);
  }
  link(parent: Task, child: Task, required = true) {
    if (parent.account !== child.account) throw new Error("父子任务账号不一致");
    if (parent.id === child.id) return false;
    const cycle = this.db
      .prepare(
        "WITH RECURSIVE descendants(id) AS (SELECT child FROM edges WHERE parent=? UNION SELECT e.child FROM edges e JOIN descendants d ON e.parent=d.id) SELECT 1 FROM descendants WHERE id=?",
      )
      .get(child.id, parent.id);
    if (cycle) return false;
    this.db
      .prepare(
        "INSERT INTO edges VALUES(?,?,?) ON CONFLICT(parent,child) DO UPDATE SET required=max(required,excluded.required)",
      )
      .run(parent.id, child.id, required ? 1 : 0);
    return true;
  }
  refreshParents(id: string, seen = new Set<string>()) {
    if (seen.has(id)) return;
    seen.add(id);
    for (const row of this.db
      .prepare("SELECT parent FROM edges WHERE child=?")
      .all(id)) {
      const parent = this.task(String(row.parent));
      const unfinished = this.db
        .prepare(
          "SELECT 1 FROM edges e JOIN tasks t ON t.id=e.child WHERE e.parent=? AND e.required=1 AND t.status!='COMPLETED' LIMIT 1",
        )
        .get(parent.id);
      if (parent.details.discoveryComplete && !parent.details.residual)
        this.outcome(parent.id, unfinished ? "WAITING_CHILDREN" : "COMPLETED");
      this.refreshParents(parent.id, seen);
    }
  }
  begin(task: Task, kind: string) {
    const id = token();
    this.transaction(() => {
      this.db
        .prepare("INSERT INTO runs VALUES(?,?,?,'RUNNING',NULL,?)")
        .run(id, task.id, kind, new Date().toISOString());
      if (kind !== "references") this.outcome(task.id, "RUNNING");
    });
    return id;
  }
  end(run: string, status: Status, detail: unknown) {
    this.db
      .prepare("UPDATE runs SET status=?,outcome=? WHERE id=?")
      .run(status, JSON.stringify(detail), run);
  }
  pending(task: Task, operation: string): { id: string; baseline: any }[] {
    return this.db
      .prepare(
        "SELECT id,baseline FROM intents WHERE task_id=? AND operation=? AND state='PENDING'",
      )
      .all(task.id, operation)
      .map((r) => ({
        id: String(r.id),
        baseline: JSON.parse(String(r.baseline)),
      }));
  }
  intent(task: Task, operation: string, baseline: unknown) {
    if (this.pending(task, operation).length)
      throw new Error("上次操作结果尚未确认，请在平台核对");
    const id = token();
    this.db
      .prepare("INSERT INTO intents VALUES(?,?,?,'PENDING',?,NULL,?)")
      .run(
        id,
        task.id,
        operation,
        JSON.stringify(baseline),
        new Date().toISOString(),
      );
    return id;
  }
  confirm(id: string, receipt: unknown) {
    this.db
      .prepare(
        "UPDATE intents SET state='VERIFIED',receipt=? WHERE id=? AND state='PENDING'",
      )
      .run(JSON.stringify(receipt), id);
  }
  history(task: Task): Record<string, any> {
    const r = this.db
      .prepare("SELECT payload FROM exam_history WHERE task_id=?")
      .get(task.id);
    return r ? JSON.parse(String(r.payload)) : {};
  }
  saveHistory(task: Task, value: Record<string, any>) {
    const old = this.history(task);
    const merged = {
      ...old,
      ...value,
      failedModels: [
        ...new Set([
          ...(old.failedModels || []),
          ...(value.failedModels || []),
        ]),
      ],
    };
    this.db
      .prepare(
        "INSERT INTO exam_history VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET payload=excluded.payload",
      )
      .run(task.id, JSON.stringify(merged));
  }
  async snapshot(label = "backup") {
    const dir = join(this.directory, "backups");
    mkdirSync(dir, { recursive: true });
    const path = join(
      dir,
      `${Date.now()}-${label}-${token().slice(0, 8)}.sqlite`,
    );
    await backup(this.db, path);
    return path;
  }
  async clear(account: string, includeExams: boolean) {
    await this.snapshot("queues");
    this.db
      .prepare(
        `DELETE FROM queues WHERE task_id IN (SELECT id FROM tasks WHERE account=?) ${includeExams ? "" : "AND queue IN ('learn','failure','discover')"}`,
      )
      .run(account);
    // Exam history and uncertain intents survive queue clearing.
  }
  export() {
    return Object.fromEntries(
      ["tasks", "queues", "edges", "runs", "intents", "exam_history"].map(
        (t) => [t, this.db.prepare(`SELECT * FROM ${t}`).all()],
      ),
    );
  }
}
