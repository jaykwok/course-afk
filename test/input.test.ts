import test from "node:test";
import assert from "node:assert/strict";
import { application, COURSE, EXAM, SUBJECT } from "./helpers.ts";
import { token } from "../src/utils.ts";
import type { Queue } from "../src/storage/repository.ts";
import { atomicJson } from "../src/storage/files.ts";
import { accountId } from "../src/domain/identity.ts";
import { readdir } from "node:fs/promises";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";

test("empty link forms do not start an operation or request login", async (t) => {
  const f = await application();
  try {
    const lease = t.mock.method(f.app.auth, "lease", async () => {
      throw new Error("must not authenticate");
    });
    for (const kind of ["links", "references"]) {
      assert.throws(
        () => f.app.command(token(), "operation", { kind, input_text: "  " }),
        /请先填写/,
      );
      assert.equal(f.app.operationId, null);
      assert.equal(f.app.view.prompt, null);
    }
    assert.equal(lease.mock.callCount(), 0);
  } finally {
    await f.close();
  }
});

test("queue clearing applies the submitted form scope without another prompt or login", async (t) => {
  const queues: Queue[] = ["learn", "failure", "discover", "exam", "review"];
  for (const [scope, removed] of [
    ["exams", ["exam", "review"]],
    ["failures", ["failure"]],
    ["courses", ["learn", "failure", "discover"]],
    ["all", queues],
  ] as const) {
    const f = await application();
    try {
      const lease = t.mock.method(f.app.auth, "lease", async () => {
        throw new Error("Local operation requested login");
      });
      const prompt = t.mock.method(f.app, "prompt", async () => {
        throw new Error("Must not ask another question");
      });
      for (const account of ["a", "b"])
        for (const queue of queues)
          f.repo.enqueue(
            f.repo.upsert(
              queue === "exam" || queue === "review"
                ? EXAM
                : queue === "discover"
                  ? SUBJECT
                  : queue === "failure"
                    ? COURSE.replaceAll("11111111", "aaaaaaaa")
                    : COURSE,
              account,
            ),
            queue,
          );
      const exam = f.repo.upsert(EXAM, "a");
      const intent = f.repo.intent(exam, "submit", { recordId: "before" });
      f.repo.saveHistory(exam, { failedModels: ["existing-model"] });
      f.repo.db
        .prepare("UPDATE tasks SET manual_submit=1 WHERE id=?")
        .run(exam.id);
      for (const invalid of [undefined, "everything"])
        assert.throws(
          () =>
            f.app.command(token(), "operation", {
              kind: "clear_queues",
              account: "a",
              scope: invalid,
            }),
          /清空范围无效/,
        );
      assert.equal(f.app.operationId, null);
      f.app.command(token(), "operation", {
        kind: "clear_queues",
        account: "a",
        scope,
      });
      await f.app.settled();
      assert.equal(lease.mock.callCount(), 0);
      assert.equal(prompt.mock.callCount(), 0);
      assert.match(f.app.view.notification.message, /已清空/);
      for (const queue of queues) {
        assert.equal(
          f.repo.queue("a", queue).length,
          (removed as readonly string[]).includes(queue) ? 0 : 1,
        );
        assert.equal(f.repo.queue("b", queue).length, 1);
      }
      assert.equal(f.repo.pending(exam, "submit")[0].id, intent);
      assert.deepEqual(f.repo.history(exam).failedModels, ["existing-model"]);
      assert.equal(f.repo.task(exam.id).manualSubmit, true);
    } finally {
      await f.close();
    }
  }
});

test("clearing course failures removes retryable and blocked work while keeping healthy queues and a backup", async (t) => {
  const f = await application();
  try {
    t.mock.method(f.app.auth, "lease", async () => {
      throw new Error("Local operation requested login");
    });
    const ready = f.repo.upsert(COURSE, "a"),
      pending = f.repo.upsert(SUBJECT, "a"),
      exam = f.repo.upsert(EXAM, "a");
    f.repo.enqueue(ready, "learn");
    f.repo.enqueue(pending, "discover");
    f.repo.enqueue(exam, "exam");
    for (const [url, recovery] of [
      [COURSE.replaceAll("11111111", "aaaaaaaa"), "retry"],
      ["https://cms.mylearning.cn/topic/blocked.html", "blocked"],
    ])
      f.repo.review(f.repo.upsert(url, "a"), "unavailable", {
        message: "暂时无法学习",
        recovery,
      });
    assert.deepEqual(f.app.localQueues().accounts[0].counts, {
      learn: 1,
      discover: 1,
      failure: 2,
      exam: 1,
      review: 0,
    });
    assert.equal(f.app.operationId, null);
    f.app.command(token(), "operation", {
      kind: "clear_queues",
      account: "a",
      scope: "failures",
    });
    await f.app.settled();
    assert.equal(f.repo.queue("a", "failure").length, 0);
    assert.deepEqual(
      f.repo.queue("a", "learn").map((task) => task.id),
      [ready.id],
    );
    assert.deepEqual(
      f.repo.queue("a", "discover").map((task) => task.id),
      [pending.id],
    );
    assert.deepEqual(
      f.repo.queue("a", "exam").map((task) => task.id),
      [exam.id],
    );
    const backups = await readdir(join(f.app.config.dataDir, "backups"));
    const saved = new DatabaseSync(
      join(f.app.config.dataDir, "backups", backups[0]),
      { readOnly: true },
    );
    try {
      assert.equal(
        saved
          .prepare("SELECT count(*) n FROM queues WHERE queue='failure'")
          .get()?.n,
        2,
      );
    } finally {
      saved.close();
    }
  } finally {
    await f.close();
  }
});

test("link input, manual selection and reference download accept prose and report invalid URLs without dropping valid ones", async (t) => {
  const f = await application();
  const opened: string[] = [];
  try {
    t.mock.method(f.app.auth, "lease", async (scope: any) => ({
      account: "a",
      generation: 0,
      fingerprint: "fixture",
      signal: scope.signal,
    }));
    t.mock.method(f.app.auth, "assert", () => {});
    t.mock.method(f.app.auth, "navigate", async (_scope: any, url: string) => {
      opened.push(url);
      return {
        url,
        async bringToFront() {},
        async close() {},
        async waitForFunction() {},
        async evaluate() {
          throw new Error("fixture course unavailable");
        },
      } as any;
    });
    t.mock.method(f.app, "prompt", async () => true);
    for (const kind of ["links", "select", "references"] as const) {
      const second =
        kind === "references"
          ? COURSE.replace(
              "11111111-1111-4111-8111-111111111111",
              "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            )
          : EXAM;
      const input_text = `课程链接：${COURSE}；另一个链接：${second}；无效：https://outside.test/a`;
      f.app.command(token(), "operation", { kind, input_text });
      await f.app.settled();
      assert.ok(f.app.logs.some((l) => l.message.includes("已忽略 1 条")));
      assert.ok(!f.app.logs.some((l) => /未找到可用/.test(l.message)));
      if (kind === "links") {
        assert.equal(f.repo.queue("a", "learn").length, 1);
        assert.equal(f.repo.queue("a", "exam").length, 1);
        assert.deepEqual(f.app.view.summary.rows, [
          ["已发现任务", 2],
          ["待复查入口", 0],
          ["待继续解析", 0],
          ["已解析入口", 0],
          ["已跳过占位或内容页", 0],
          ["延伸专题（未展开）", 0],
          ["已忽略无效链接", 1],
        ]);
      } else {
        assert.ok(opened.includes(COURSE));
        assert.ok(opened.includes(second));
        if (kind === "references")
          assert.equal(f.app.view.summary.title, "资料下载结果");
      }
      opened.length = 0;
    }
    f.chrome.assertDone();
  } finally {
    await f.close();
  }
});

test("expired or absent credentials never gate local clearing, and selecting all accounts produces a recoverable backup", async (t) => {
  for (const expired of [false, true]) {
    const f = await application();
    try {
      const account = accountId("center:local");
      if (expired)
        await atomicJson(f.app.auth.path, {
          version: 2,
          account: "center:local",
          name: "Local",
          expires: "2000-01-01T00:00:00Z",
          cookies: [
            { name: "session", value: "synthetic", domain: ".mylearning.cn" },
          ],
        });
      for (const owner of [account, "other"])
        f.repo.enqueue(f.repo.upsert(COURSE, owner), "learn");
      const lease = t.mock.method(f.app.auth, "lease", async () => {
        throw new Error("must not authenticate");
      });
      f.app.command(token(), "operation", {
        kind: "clear_queues",
        scope: "all",
        account: null,
      });
      await f.app.settled();
      assert.equal(lease.mock.callCount(), 0);
      assert.equal(f.repo.queue(account, "learn").length, 0);
      assert.equal(f.repo.queue("other", "learn").length, 0);
      const backups = await readdir(join(f.app.config.dataDir, "backups"));
      const saved = new DatabaseSync(
        join(f.app.config.dataDir, "backups", backups[0]),
        { readOnly: true },
      );
      try {
        assert.equal(
          saved.prepare("SELECT count(*) n FROM queues").get()?.n,
          2,
        );
      } finally {
        saved.close();
      }
      f.chrome.assertDone();
    } finally {
      await f.close();
    }
  }
});
