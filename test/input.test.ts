import test from "node:test";
import assert from "node:assert/strict";
import { application, COURSE, EXAM } from "./helpers.ts";
import { token } from "../src/utils.ts";
import type { Queue } from "../src/storage/repository.ts";

test("queue clearing respects exam/course scope, cancellation, accounts and submission records", async (t) => {
  const queues: Queue[] = ["learn", "failure", "discover", "exam", "review"];
  for (const [kind, confirmed, removed] of [
    ["clear_exams", false, []],
    ["clear_exams", true, ["exam", "review"]],
    ["clear_courses", true, ["learn", "failure", "discover"]],
    ["clear_all", true, queues],
  ] as const) {
    const f = await application();
    try {
      t.mock.method(f.app.auth, "lease", async (scope: any) => ({
        account: "a",
        generation: 0,
        fingerprint: "fixture",
        signal: scope.signal,
      }));
      t.mock.method(f.app.auth, "assert", () => {});
      t.mock.method(
        f.app,
        "prompt",
        async (_type: string, fields: { message: string }) => {
          if (kind === "clear_exams")
            assert.match(fields.message, /待考科目和考试复查/);
          return confirmed;
        },
      );
      for (const account of ["a", "b"])
        for (const queue of queues)
          f.repo.enqueue(
            f.repo.upsert(
              queue === "exam" || queue === "review" ? EXAM : COURSE,
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
      f.app.command(token(), "operation", { kind });
      await f.app.settled();
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
