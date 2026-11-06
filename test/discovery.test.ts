import test from "node:test";
import assert from "node:assert/strict";
import { Repository } from "../src/storage/repository.ts";
import {
  traverse,
  subjectManifest,
  extractLinks,
} from "../src/platform/discovery.ts";
import { temporary, COURSE, EXAM, SUBJECT } from "./helpers.ts";
test("recursive traversal shares cycles and aliases, preserves partial discoveries", async () => {
  const temp = await temporary(),
    repo = new Repository(temp.dir),
    signal = new AbortController();
  const second = "https://cms.mylearning.cn/topic/second.html",
    root = "https://cms.mylearning.cn/topic/root.html";
  let reads = 0;
  try {
    const ctx = {
      repo,
      lease: {
        account: "a",
        generation: 1,
        fingerprint: "test",
        signal: signal.signal,
      },
      report: () => {},
    };
    const result = await traverse(
      [root],
      async (task) => {
        reads++;
        if (task.url === root)
          return { links: [second, COURSE], complete: true };
        return {
          links: [root, EXAM],
          complete: false,
          message: "分页缺少最后一页",
        };
      },
      ctx,
    );
    assert.equal(reads, 2);
    assert.equal(result.complete, false);
    assert.equal(repo.queue("a", "learn").length, 1);
    assert.equal(repo.queue("a", "exam").length, 1);
    assert.equal(repo.queue("a", "failure").length, 2);
    assert.equal(repo.queue("b", "learn").length, 0);
  } finally {
    repo.close();
    await temp.close();
  }
});
test("limits preserve pending containers; cancellation propagates without discarding leaf work", async () => {
  const temp = await temporary(),
    repo = new Repository(temp.dir),
    abort = new AbortController();
  try {
    const root = "https://cms.mylearning.cn/topic/a.html",
      child = "https://cms.mylearning.cn/topic/b.html";
    const ctx = {
      repo,
      lease: {
        account: "a",
        generation: 1,
        fingerprint: "x",
        signal: abort.signal,
      },
      report: () => {},
    };
    await traverse(
      [root],
      async () => ({ links: [COURSE, child], complete: true }),
      ctx,
      { entries: 1, depth: 1 },
    );
    assert.equal(repo.queue("a", "learn").length, 1);
    assert.ok(repo.queue("a", "discover").some((t) => t.url === child));
    abort.abort(new Error("cancelled"));
    await assert.rejects(
      traverse([SUBJECT], async () => ({ links: [], complete: true }), ctx),
      /cancelled/,
    );
  } finally {
    repo.close();
    await temp.close();
  }
});
test("subject uses resource IDs and required source identity; unknown sections stay incomplete", async () => {
  const t = await temporary(),
    r = new Repository(t.dir);
  try {
    const task = r.upsert(SUBJECT, "a");
    const m = subjectManifest(
      [
        {
          courseChapterSections: [
            {
              id: "wrong",
              sectionType: 10,
              attachmentId: COURSE.split("/").at(-1),
              required: 1,
            },
            { sectionType: 9, resourceId: EXAM.split("/").at(-1) },
            { sectionType: 77 },
          ],
        },
      ],
      task,
    );
    assert.equal(m.complete, false);
    assert.match(m.links[0], /\/6\/1\/333/);
    assert.ok(m.links.includes(EXAM));
    assert.deepEqual(
      extractLinks(
        [`onclick="window.open('${SUBJECT}')"`, COURSE],
        SUBJECT,
      ).sort(),
      [SUBJECT, COURSE].sort(),
    );
  } finally {
    r.close();
    await t.close();
  }
});
