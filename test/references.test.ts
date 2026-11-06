import test, { type TestContext } from "node:test";
import assert from "node:assert/strict";
import { readFile, writeFile, rm } from "node:fs/promises";
import { join } from "node:path";
import { references } from "../src/platform/references.ts";
import { Repository } from "../src/storage/repository.ts";
import type { DiscoveryContext } from "../src/platform/discovery.ts";
import { COURSE, temporary } from "./helpers.ts";

async function fixture(t: TestContext) {
  const temp = await temporary(),
    repo = new Repository(temp.dir),
    task = repo.upsert(COURSE, "a");
  const state = {
    failInfo: false,
    downloads: 0,
    guides: 0,
    opened: 0,
    closed: 0,
    guide: true,
  };
  let abort = new AbortController();
  const ctx = {
    repo,
    lease: { account: "a", signal: abort.signal },
    report() {},
    auth: {
      assert() {
        abort.signal.throwIfAborted();
      },
      async navigate() {
        state.opened++;
        return {
          async close() {
            state.closed++;
          },
          async waitForFunction() {},
          async evaluate(_fn: unknown, arg: any) {
            if (!arg?.url) return "synthetic";
            let value;
            if (arg.url.includes("/course-front/info/")) {
              if (state.failInfo)
                throw new Error("temporary course-info failure");
              value = {
                id: task.businessId,
                name: "课程",
                courseChapters: [
                  {
                    courseChapterSections: [
                      {
                        id: "section-a",
                        resourceId: "resource-a",
                        name: "课件 A",
                        fileType: "pdf",
                        guideStudyFlag: state.guide,
                      },
                      {
                        id: "section-b",
                        resourceId: "resource-b",
                        name: "课件 B",
                        fileType: "pdf",
                      },
                    ],
                  },
                ],
              };
            } else if (arg.url.includes("/preview?"))
              value = { url: "https://cdn.zhixueyun.com/file.pdf" };
            else if (arg.url.includes("/get-guide-study-info?")) {
              state.guides++;
              value = [{ name: "导学", content: "学习说明" }];
            } else throw new Error("Unexpected API");
            return { status: 200, value };
          },
        };
      },
    },
  } as unknown as DiscoveryContext;
  t.mock.method(globalThis, "fetch", async () => {
    state.downloads++;
    return new Response("%PDF-1.7\nfixture");
  });
  const output = join(temp.dir, "references", "a", task.businessId);
  return {
    temp,
    repo,
    task,
    ctx,
    state,
    output,
    cancel() {
      abort.abort(new Error("cancelled"));
    },
    reset() {
      abort = new AbortController();
      ctx.lease.signal = abort.signal;
    },
    run: () => references(task, ctx, temp.dir),
    async saved() {
      return JSON.parse(await readFile(join(output, "manifest.json"), "utf8"));
    },
    async close() {
      assert.equal(state.closed, state.opened);
      repo.close();
      await temp.close();
    },
  };
}

test("reference retries preserve successful indexes, reuse validated files and repair corrupt documents and missing guides", async (t) => {
  const f = await fixture(t);
  try {
    const first = await f.run();
    assert.equal(first.complete, true);
    assert.equal(first.files.length, 3);
    assert.equal(f.state.downloads, 2);
    f.state.failInfo = true;
    const failed = await f.run();
    assert.equal(failed.complete, false);
    assert.deepEqual(failed.files, first.files);
    for (const file of first.files)
      assert.ok(
        (await readFile(join(f.output, "资料索引.md"), "utf8")).includes(
          file.markdown || file.path,
        ),
      );
    f.state.failInfo = false;
    assert.equal((await f.run()).complete, true);
    assert.equal(f.state.downloads, 2);
    assert.equal(f.state.guides, 1);
    const document = first.files.find((f) => !f.kind)!;
    await writeFile(join(f.output, document.path), "%PDF-1.7\nbroken!");
    assert.equal((await f.run()).complete, true);
    assert.equal(f.state.downloads, 3);
    const guide = first.files.find((f) => f.kind === "guide")!;
    await rm(join(f.output, guide.markdown!));
    assert.equal((await f.run()).complete, true);
    assert.equal(f.state.downloads, 3);
    assert.equal(f.state.guides, 2);
    assert.equal((await f.saved()).files.length, 3);
  } finally {
    await f.close();
  }
});

test("cancelled reference batches persist completed resources and resume without downloading them again", async (t) => {
  const f = await fixture(t);
  try {
    f.state.guide = false;
    f.ctx.report = () => f.cancel();
    await assert.rejects(f.run(), /cancelled/);
    const partial = await f.saved();
    assert.equal(partial.complete, false);
    assert.equal(partial.files.length, 1);
    assert.ok(
      (await readFile(join(f.output, "资料索引.md"), "utf8")).includes(
        partial.files[0].path,
      ),
    );
    f.reset();
    f.ctx.report = () => {};
    const result = await f.run();
    assert.equal(result.complete, true);
    assert.equal(result.files.length, 2);
    assert.equal(f.state.downloads, 2);
  } finally {
    await f.close();
  }
});
