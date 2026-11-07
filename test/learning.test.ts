import test from "node:test";
import assert from "node:assert/strict";
import { learn } from "../src/platform/learning.ts";
import {
  playbackSnapshot,
  learningProgress,
  type Playback,
  type LearningProgress,
} from "../src/platform/playback.ts";
import type { DiscoveryContext } from "../src/platform/discovery.ts";
import { configuration } from "../src/config.ts";
import { Repository } from "../src/storage/repository.ts";
import { COURSE, temporary } from "./helpers.ts";

const turn = () => new Promise<void>((resolve) => setImmediate(resolve));
async function fixture() {
  const temp = await temporary(),
    repo = new Repository(temp.dir);
  const task = repo.upsert(COURSE, "learning-test");
  const abort = new AbortController();
  const playback: Playback = {
    focused: true,
    media: {
      time: 40,
      duration: 120,
      paused: false,
      ended: false,
      readyState: 4,
      error: false,
    },
  };
  const section = {
    id: "section",
    resourceId: "resource",
    sectionType: 6,
    name: "测试视频",
  };
  const reads: number[] = [];
  const updates: (LearningProgress | null)[] = [];
  let onUpdate = () => {};
  let closed = false;
  const ctx = {
    repo,
    lease: { account: task.account, signal: abort.signal },
    report() {},
    auth: {
      assert() {
        abort.signal.throwIfAborted();
      },
      async navigate() {
        return {
          async bringToFront() {},
          async close() {
            closed = true;
          },
          async queryAll() {
            return [];
          },
          async query() {
            return null;
          },
          async waitForFunction() {},
          async waitForSelector() {
            return {
              async evaluate() {
                return true;
              },
            };
          },
          async evaluate(fn: unknown, arg: any) {
            if (fn === playbackSnapshot) return structuredClone(playback);
            if (arg?.url) {
              if (arg.url.includes("/info/"))
                return {
                  status: 200,
                  value: {
                    id: task.businessId,
                    courseChapters: [{ courseChapterSections: [section] }],
                  },
                };
              assert.ok(arg.url.endsWith("/course-section-progress"));
              reads.push(Date.now());
              return {
                status: 200,
                value: [
                  {
                    courseId: task.businessId,
                    sectionId: section.id,
                    resourceId: section.resourceId,
                    finishStatus: Date.now() >= 5000 ? 2 : 0,
                  },
                ],
              };
            }
            if (typeof arg === "string") return true;
            return { title: "课程", notices: [], challenge: false };
          },
        };
      },
    },
  } as unknown as DiscoveryContext;
  return {
    repo,
    task,
    abort,
    playback,
    reads,
    updates,
    get closed() {
      return closed;
    },
    run: () =>
      learn(
        task,
        ctx,
        { ...configuration(undefined, temp.dir), watchSlice: 5 },
        (value) => {
          updates.push(value);
          onUpdate();
        },
      ),
    nextUpdate: () =>
      new Promise<void>((resolve) => {
        onUpdate = resolve;
      }),
    close: async () => {
      repo.close();
      await temp.close();
    },
  };
}

test("learning samples playback each second without accelerating server polling or marking ended media complete", async (t) => {
  const f = await fixture();
  t.mock.timers.enable({ apis: ["Date"], now: 0 });
  const running = f.run();
  const tick = async () => {
    const update = f.nextUpdate();
    t.mock.timers.tick(1000);
    await update;
    await turn();
  };
  try {
    await turn();
    assert.equal(f.updates.at(-1)?.completed, 40);
    assert.deepEqual(f.reads, [0, 0]);
    f.playback.media!.time = 41;
    await tick();
    assert.equal(f.updates.at(-1)?.completed, 41);
    f.playback.media!.paused = true;
    await tick();
    assert.equal(f.updates.at(-1)?.completed, 41);
    assert.equal(f.updates.at(-1)?.state, "paused");
    f.playback.media!.time = 5;
    f.playback.media!.paused = false;
    await tick();
    assert.equal(f.updates.at(-1)?.completed, 5);
    f.playback.media!.time = 120;
    f.playback.media!.ended = true;
    await tick();
    assert.equal(f.updates.at(-1)?.state, "syncing");
    assert.notEqual(f.repo.task(f.task.id).status, "COMPLETED");
    assert.deepEqual(f.reads, [0, 0]);
    t.mock.timers.tick(1000);
    await running;
    assert.deepEqual(f.reads, [0, 0, 5000, 5000]);
    assert.equal(f.repo.task(f.task.id).status, "COMPLETED");
    assert.equal(f.updates.at(-1), null);
    assert.equal(f.closed, true);
  } finally {
    f.abort.abort();
    await running.catch(() => {});
    t.mock.timers.reset();
    await f.close();
  }
});

test("cancelling learning clears the displayed progress and stops sampling", async (t) => {
  const f = await fixture();
  t.mock.timers.enable({ apis: ["Date", "setTimeout"], now: 0 });
  try {
    const stopped = assert.rejects(f.run(), /abort|取消/i);
    await turn();
    assert.equal(f.updates.at(-1)?.completed, 40);
    f.abort.abort(new Error("取消任务"));
    await stopped;
    assert.equal(f.updates.at(-1), null);
    const count = f.updates.length;
    t.mock.timers.tick(20000);
    await turn();
    assert.equal(f.updates.length, count);
    assert.equal(f.closed, true);
    assert.notEqual(f.repo.task(f.task.id).status, "COMPLETED");
  } finally {
    t.mock.timers.reset();
    await f.close();
  }
});

test("document elapsed time and unknown media duration never become a fabricated completion percentage", () => {
  const reading = learningProgress("文档", false, 12.4, null);
  assert.equal(reading.completed, 12);
  assert.equal(reading.total, null);
  assert.equal(reading.state, "reading");
  for (const duration of [NaN, Infinity, 0]) {
    const buffering = learningProgress("视频", true, 800, {
      time: 0,
      duration,
      paused: true,
      ended: false,
      readyState: 0,
      error: false,
    });
    assert.equal(buffering.completed, 0);
    assert.equal(buffering.total, null);
    assert.equal(buffering.state, "buffering");
  }
});
