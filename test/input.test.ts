import test from "node:test";
import assert from "node:assert/strict";
import { application, COURSE, EXAM } from "./helpers.ts";
import { token } from "../src/utils.ts";

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
