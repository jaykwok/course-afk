import test from "node:test";
import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { Repository } from "../src/storage/repository.ts";
import { normalize, entry, resource } from "../src/domain/identity.ts";
import { acquireInstance } from "../src/application/instance.ts";
import { temporary, COURSE, EXAM, SUBJECT } from "./helpers.ts";

test("identity keeps tenant, account and source; QR aliases resolve", () => {
  assert.equal(
    normalize(
      `https://kc.zhixueyun.com/app/wechat/#/qrScan?businessType=1&businessId=${COURSE.split("/").at(-1)}`,
    ),
    COURSE,
  );
  assert.notEqual(entry(COURSE, "a").id, entry(COURSE, "b").id);
  assert.notEqual(
    entry(COURSE, "a").id,
    entry(
      resource(
        "course",
        COURSE.split("/").at(-1)!,
        SUBJECT.split("/").at(-1)!,
        "1",
      ),
      "a",
    ).id,
  );
  for (const url of [
    "https://kc.zhixueyun.com.evil.test/x",
    "file:///a",
    "https://user:pass@kc.zhixueyun.com/",
  ])
    assert.throws(() => normalize(url));
});
test("SQLite preserves queues, history, uncertain intent and interrupts crashed runs", async () => {
  const t = await temporary();
  let repo = new Repository(t.dir);
  try {
    const a = repo.upsert(EXAM, "a"),
      b = repo.upsert(EXAM, "b");
    repo.enqueue(a, "exam");
    repo.enqueue(b, "exam");
    repo.begin(a, "exam");
    const intent = repo.intent(a, "submit", { recordId: "before" });
    repo.saveHistory(a, { failedModels: ["model1"] });
    assert.throws(() => repo.intent(a, "submit", {}));
    repo.close();
    repo = new Repository(t.dir);
    assert.equal(repo.task(a.id).manualSubmit, true);
    assert.equal(repo.task(a.id).status, "NEEDS_REVIEW");
    assert.equal(repo.pending(a, "submit")[0].id, intent);
    assert.deepEqual(repo.history(a).failedModels, ["model1"]);
    assert.equal(repo.queue("b", "exam").length, 1);
    await repo.clear("a", true);
    assert.equal(repo.queue("a", "exam").length, 0);
    assert.equal(repo.queue("b", "exam").length, 1);
    assert.equal(repo.pending(a, "submit").length, 1);
    assert.equal(repo.task(a.id).manualSubmit, true);
    repo.confirm(intent, { recordId: "new" });
    assert.equal(repo.pending(a, "submit").length, 0);
  } finally {
    repo.close();
    await t.close();
  }
});
test("transactions roll back; graph rejects cycles and cross-account dependencies", async () => {
  const t = await temporary(),
    r = new Repository(t.dir);
  try {
    const a = r.upsert(SUBJECT, "a"),
      b = r.upsert(COURSE, "a"),
      c = r.upsert(COURSE, "b");
    assert.throws(() =>
      r.transaction(() => {
        r.enqueue(b, "learn");
        throw new Error("crash");
      }),
    );
    assert.equal(r.queue("a", "learn").length, 0);
    assert.equal(r.link(a, b), true);
    assert.equal(r.link(b, a), false);
    assert.throws(() => r.link(a, c));
    r.outcome(a.id, "WAITING_CHILDREN", {
      discoveryComplete: true,
      residual: false,
    });
    r.complete(b, { finishStatus: 2 });
    assert.equal(r.task(a.id).status, "COMPLETED");
    const path = await r.snapshot(),
      backup = new DatabaseSync(path, { readOnly: true });
    assert.equal(backup.prepare("SELECT count(*) n FROM tasks").get()?.n, 3);
    backup.close();
  } finally {
    r.close();
    await t.close();
  }
});
test("OS instance ownership excludes concurrent startup and releases on close", async () => {
  const t = await temporary();
  try {
    const a = await acquireInstance(t.dir);
    assert.equal(a.owned, true);
    if (!a.owned) return;
    await a.publish("http://127.0.0.1:12345", "key");
    const b = await acquireInstance(t.dir);
    assert.equal(b.owned, false);
    if (!b.owned) assert.equal(b.instance.key, "key");
    await a.close();
    const c = await acquireInstance(t.dir);
    assert.equal(c.owned, true);
    if (c.owned) await c.close();
  } finally {
    await t.close();
  }
});
test("reference runs leave learning completion and queues unchanged", async () => {
  const temp = await temporary(),
    repo = new Repository(temp.dir);
  try {
    const task = repo.upsert(COURSE, "a");
    repo.complete(task, { source: "server" });
    const run = repo.begin(repo.task(task.id), "references");
    assert.equal(repo.task(task.id).status, "COMPLETED");
    repo.end(run, "FAILED", { reason: "download failed" });
    assert.equal(repo.task(task.id).status, "COMPLETED");
    assert.equal(repo.queue("a", "learn").length, 0);
  } finally {
    repo.close();
    await temp.close();
  }
});
