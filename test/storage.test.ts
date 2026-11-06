import test from "node:test";
import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { Repository } from "../src/storage/repository.ts";
import {
  normalize,
  entry,
  resource,
  parseLinkInput,
  CASE_POOL,
  platformHost,
} from "../src/domain/identity.ts";
import { previewUrl } from "../src/platform/references.ts";
import { acquireInstance } from "../src/application/instance.ts";
import { temporary, COURSE, EXAM, SUBJECT } from "./helpers.ts";

test("pasted prose retains valid URLs and validates each complete source independently", () => {
  const sourced = `${COURSE}?type=6&required=1&sourceId=${entry(SUBJECT, "a").businessId}`;
  const input = `课程链接：【${COURSE}】，考试：${EXAM}；重复（${COURSE}）。\n主题来源：<${sourced.replaceAll("&", "&amp;")}>\n无效来源：${COURSE}?sourceId=broken 外站：https://evil.test/a`;
  const result = parseLinkInput(input);
  assert.deepEqual(result.urls, [COURSE, EXAM, normalize(sourced)]);
  assert.equal(result.rejected, 2);
  assert.deepEqual(parseLinkInput("通知里没有链接"), { urls: [], rejected: 0 });
  assert.deepEqual(parseLinkInput(`链接：${COURSE}，${EXAM}`).urls, [
    COURSE,
    EXAM,
  ]);
});

test("case-pool entry aliases share identity without extending credential or download hosts", () => {
  assert.equal(
    normalize("http://ctexpert.cn/expert-assist-h5/casePool/?code=discarded"),
    CASE_POOL,
  );
  assert.equal(entry(CASE_POOL, "a").kind, "entry");
  assert.equal(platformHost("www.ctexpert.cn"), false);
  assert.throws(() => previewUrl(CASE_POOL));
  for (const url of [
    "https://evil.ctexpert.cn/expert-assist-web/casePool",
    "https://www.ctexpert.cn/expert-assist-web/casePool/other",
    "https://www.ctexpert.cn/login",
    "https://www.ctexpert.cn/expert-assist-web/casePool#/study/course/detail/11111111-1111-4111-8111-111111111111",
    "https://u:p@www.ctexpert.cn/expert-assist-web/casePool",
  ])
    assert.throws(() => normalize(url));
});

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

test("query and path course sources share identity without merging other subjects or bare courses", async () => {
  const temp = await temporary(),
    repo = new Repository(temp.dir);
  try {
    const subject = entry(SUBJECT, "a").businessId;
    const query = `${COURSE}?type=6&required=1&sourceId=${subject}`;
    const path = `${COURSE}/6/1/${subject}`;
    assert.equal(normalize(query), path);
    assert.equal(repo.upsert(query, "a").id, repo.upsert(path, "a").id);
    assert.equal(repo.upsert(query, "a").url, path);
    assert.notEqual(repo.upsert(query, "a").id, repo.upsert(COURSE, "a").id);
    assert.notEqual(
      entry(query, "a").id,
      entry(query.replace(subject, entry(EXAM, "a").businessId), "a").id,
    );
    assert.equal(
      normalize(
        `${COURSE}?TYPE=6&isRequired=1&sourceId=${subject.toUpperCase()}`,
      ),
      path,
    );
    const qr = `https://kc.zhixueyun.com/app/wechat/#/qrScan?businessType=1&businessId=${entry(COURSE, "a").businessId}&type=6&require=1&sourceId=${subject}`;
    assert.equal(normalize(qr), path);
    for (const invalid of [
      `${COURSE}?sourceId=${subject}`,
      `${COURSE}?type=6&required=1`,
      `${COURSE}?type=6&required=2&sourceId=${subject}`,
      `${query}&sourceId=${entry(EXAM, "a").businessId}`,
      `${path}?required=0`,
      `${path}?type=7`,
    ])
      assert.throws(() => normalize(invalid));
    const nested = `https://www.mylearning.cn/#/paas-container?paasurl=${encodeURIComponent(`https://kc.zhixueyun.com/#/train?classId=${subject}`)}`;
    assert.equal(normalize(nested), resource("class", subject));
  } finally {
    repo.close();
    await temp.close();
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
    r.enqueue(a, "learn");
    r.review(a, "subject_residual");
    r.complete(b, { finishStatus: 2 });
    assert.equal(r.task(a.id).status, "COMPLETED");
    assert.equal(r.task(a.id).details.reason, undefined);
    assert.equal(r.queue("a", "learn").length, 0);
    assert.equal(r.queue("a", "failure").length, 0);
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
    repo.review(task, "partial_course_failure", {
      message: "上次播放失败",
      sections: [{ section: "s", message: "stalled" }],
    });
    repo.complete(task, { source: "server" });
    assert.deepEqual(repo.task(task.id).details, {
      evidence: { source: "server" },
    });
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
