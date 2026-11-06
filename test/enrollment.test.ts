import test from "node:test";
import assert from "node:assert/strict";
import { Repository } from "../src/storage/repository.ts";
import { discover, type DiscoveryContext } from "../src/platform/discovery.ts";
import { entry, normalize, resource } from "../src/domain/identity.ts";
import { temporary, COURSE } from "./helpers.ts";

const classId = "75c755b8-a236-45e3-b599-fdb54db0ccea";
const classUrl = resource("class", classId);
const paas = `https://kc.zhixueyun.com/#/paas-container?paasurl=${encodeURIComponent(`website/1645614858578771970/default?screen=desktop&type=4&isPreview=0&classId=${classId}`)}`;
const root = "https://cms.mylearning.cn/topic/project/pc.html";

async function fixture(
  options: {
    status?: number;
    after?: number;
    fields?: Record<string, unknown>;
    lostResponse?: boolean;
    wrongClass?: boolean;
    info?: Record<string, unknown>;
    detailCode?: number;
  } = {},
) {
  const temp = await temporary(),
    repo = new Repository(temp.dir);
  let status = options.status ?? 0,
    posts = 0,
    chapterReads = 0,
    confirmations = 0;
  const ctx = {
    repo,
    lease: {
      account: "a",
      generation: 0,
      fingerprint: "f",
      signal: new AbortController().signal,
    },
    report() {},
    async confirm() {
      confirmations++;
      throw new Error("Unexpected enrollment confirmation");
    },
    auth: {
      assert() {},
      async navigate(_scope: unknown, url: string) {
        return {
          url,
          async close() {},
          async queryAll() {
            return [];
          },
          async waitForFunction() {},
          async evaluate(
            _fn: unknown,
            arg?: { url: string; method: string; body: unknown; extra: any },
          ) {
            if (!arg)
              return {
                url,
                values: [paas],
                title: "专题",
                text: "",
                ready: true,
                status: 200,
                article: false,
                busy: false,
                interactive: false,
              };
            const path = new URL(arg.url).pathname;
            let value: unknown;
            if (path.endsWith("/registrationProgress"))
              value = {
                code: 0,
                data: {
                  classInfoId: options.wrongClass ? "wrong" : classId,
                  registerStatus: status,
                  classInfoVo: {
                    payType: 0,
                    isRegister: 2,
                    isRegisterReason: 2,
                    signupFile: 0,
                    ...options.fields,
                  },
                },
              };
            else if (path.includes("/class-info/safe/front/basic/")) {
              value = {
                ...(options.detailCode !== undefined
                  ? { code: options.detailCode }
                  : {}),
                id: classId,
                registerStartTime: null,
                registerEndTime: null,
                currentTime: Date.now(),
                ...options.info,
              };
            } else if (path.endsWith("/register")) {
              assert.equal(arg.method, "POST");
              assert.deepEqual(arg.body, { classId });
              assert.equal(arg.extra.permitenable, "false");
              posts++;
              status = options.after ?? 5;
              if (options.lostResponse) throw new Error("response lost");
              value = { code: 0 };
            } else {
              assert.equal(
                status,
                5,
                "chapters must only be read after verified enrollment",
              );
              chapterReads++;
              value = {
                code: 0,
                data: path.includes("/chapter/paas")
                  ? [{ id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" }]
                  : [
                      {
                        id: "activity",
                        businessType: 1,
                        businessId: entry(COURSE, "a").businessId,
                      },
                    ],
              };
            }
            return { status: 200, value };
          },
        };
      },
    },
  } as unknown as DiscoveryContext;
  return {
    repo,
    ctx,
    stats: () => ({ posts, chapterReads, confirmations }),
    close: async () => {
      repo.close();
      await temp.close();
    },
  };
}

test("PAAS and recursively linked classes enroll automatically once, then collect courses", async () => {
  assert.equal(normalize(paas), classUrl);
  for (const url of [paas, root]) {
    const f = await fixture({ fields: { isRegister: 2, isRegisterReason: 1 } });
    try {
      const result = await discover([url], f.ctx);
      assert.equal(result.complete, true);
      assert.equal(result.found[0].url, COURSE);
      assert.equal(f.stats().posts, 1);
      assert.equal(f.stats().confirmations, 0);
      assert.equal(
        f.repo.pending(f.repo.upsert(classUrl, "a"), "enroll").length,
        0,
      );
      await discover([url], f.ctx);
      assert.equal(f.stats().posts, 1);
    } finally {
      await f.close();
    }
  }
});

test("already enrolled classes reconcile an old intent without submitting again", async () => {
  const f = await fixture({ status: 5 });
  try {
    const task = f.repo.upsert(classUrl, "a");
    f.repo.intent(task, "enroll", { registerStatus: 0 });
    assert.equal((await discover([paas], f.ctx)).complete, true);
    assert.equal(f.stats().posts, 0);
    assert.equal(f.repo.pending(task, "enroll").length, 0);
  } finally {
    await f.close();
  }
});

test("pending, rejected, paid and additional-form enrollment states remain explicit and do not submit", async () => {
  for (const [options, message] of [
    [{ status: 1 }, /待审核/],
    [{ status: 3 }, /待审核/],
    [{ status: 2 }, /未通过/],
    [{ status: 4 }, /待缴费/],
    [{ fields: { payType: 1 } }, /资料或付费/],
    [{ fields: { signupFile: 1 } }, /资料或付费/],
    [{ fields: { isRegister: 1, isRegisterReason: 1 } }, /资料或付费/],
    [{ wrongClass: true }, /状态不完整/],
  ] as const) {
    const f = await fixture(options);
    try {
      const result = await discover([paas], f.ctx);
      assert.equal(result.complete, false);
      assert.match(result.issues[0].message, message);
      assert.deepEqual(f.stats(), {
        posts: 0,
        chapterReads: 0,
        confirmations: 0,
      });
    } finally {
      await f.close();
    }
  }
});

test("lost enrollment responses are reconciled by status and never blindly resubmitted", async () => {
  for (const after of [0, 5]) {
    const f = await fixture({ lostResponse: true, after });
    try {
      assert.equal((await discover([paas], f.ctx)).complete, false);
      const result = await discover([paas], f.ctx);
      assert.equal(result.complete, after === 5);
      assert.equal(f.stats().posts, 1);
      if (after === 0) {
        assert.equal(f.stats().chapterReads, 0);
        assert.match(result.issues[0].message, /上次报名/);
      }
    } finally {
      await f.close();
    }
  }
});

test("accepted applications awaiting review are not mistaken for successful enrollment", async () => {
  const f = await fixture({ after: 1 });
  try {
    for (let n = 0; n < 2; n++)
      assert.match((await discover([paas], f.ctx)).issues[0].message, /待审核/);
    assert.equal(f.stats().posts, 1);
    assert.equal(f.stats().chapterReads, 0);
  } finally {
    await f.close();
  }
});

test("expired and future signup windows explain the block before any enrollment write", async (t) => {
  t.mock.timers.enable({
    apis: ["Date"],
    now: new Date("2026-11-06T14:00:00Z"),
  });
  for (const [options, reason, message] of [
    [
      { info: { registerEndTime: "2026-03-31T23:59:00" } },
      "enrollment_closed",
      /报名已结束.*2026-03-31 23:59/,
    ],
    [
      { info: { registerStartTime: "2026-11-06T23:00:00" } },
      "enrollment_not_open",
      /报名尚未开始.*2026-11-06 23:00/,
    ],
    [
      { info: { registerEndTime: "2026-11-06T13:00:00Z" } },
      "enrollment_closed",
      /报名已结束.*2026-11-06 21:00/,
    ],
    [
      { info: { registerEndTime: "invalid" } },
      "enrollment_blocked",
      /时间格式/,
    ],
    [{ info: { id: "wrong" } }, "enrollment_blocked", /期限尚未确认/],
    [{ detailCode: 500 }, "enrollment_blocked", /期限尚未确认/],
    [{ info: { currentTime: null } }, "enrollment_blocked", /当前时间尚未确认/],
    [
      {
        info: {
          currentTime: Date.parse("2026-11-07T00:00:00Z"),
          registerEndTime: Date.parse("2026-11-06T23:00:00Z"),
        },
      },
      "enrollment_closed",
      /报名已结束/,
    ],
  ] as const) {
    for (const pending of [false, true]) {
      const f = await fixture(options);
      try {
        const task = f.repo.upsert(classUrl, "a");
        if (pending) f.repo.intent(task, "enroll", { registerStatus: 0 });
        const result = await discover([paas], f.ctx);
        assert.equal(result.complete, false);
        assert.match(result.issues[0].message, message);
        assert.equal(f.repo.task(task.id).details.reason, reason);
        assert.deepEqual(f.stats(), {
          posts: 0,
          chapterReads: 0,
          confirmations: 0,
        });
      } finally {
        await f.close();
      }
    }
  }
});

test("open signup windows enroll while enrolled students can read classes after the deadline", async (t) => {
  t.mock.timers.enable({
    apis: ["Date"],
    now: new Date("2026-11-06T14:00:00Z"),
  });
  for (const options of [
    {
      info: {
        currentTime: Date.parse("2026-03-01T00:00:00Z"),
        registerEndTime: Date.parse("2026-03-31T15:59:00Z"),
      },
    },
    {
      info: {
        registerStartTime: "2026-11-06T21:59:59",
        registerEndTime: "2026-11-06T22:00:01",
      },
    },
    { status: 5, info: { registerEndTime: "2026-03-31T23:59:00" } },
  ]) {
    const f = await fixture(options);
    try {
      const result = await discover([paas], f.ctx);
      assert.equal(result.complete, true);
      assert.equal(result.found[0].url, COURSE);
      assert.equal(f.stats().posts, options.status === 5 ? 0 : 1);
    } finally {
      await f.close();
    }
  }
});
