import test from "node:test";
import assert from "node:assert/strict";
import { Repository } from "../src/storage/repository.ts";
import { discover, type DiscoveryContext } from "../src/platform/discovery.ts";
import { entry, normalize, resource } from "../src/domain/identity.ts";
import { temporary, COURSE } from "./helpers.ts";

const classId = "75c755b8-a236-45e3-b599-fdb54db0ccea";
const classUrl = resource("class", classId);
const classCourse = `${COURSE}/5/1/${classId}`;
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
    chapters?: { id: string; name: string }[];
    activities?: (url: URL) => { status: number; value: unknown };
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
              if (path.endsWith("/chapter-activity-list") && options.activities)
                return options.activities(new URL(arg.url));
              value = path.endsWith("/chapter")
                ? options.chapters || [
                    { id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" },
                  ]
                : {
                    items: [
                      {
                        id: "activity",
                        businessType: 8,
                        businessId: entry(COURSE, "a").businessId,
                        required: 1,
                      },
                    ],
                    recordCount: 1,
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
      assert.equal(result.found[0].url, classCourse);
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

test("class discovery preserves courses and the exact chapter failure instead of hiding the cause", async () => {
  const chapters = [
    { id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", name: "第一章" },
    { id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", name: "第二章" },
    { id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc", name: "第三章" },
  ];
  const f = await fixture({
    status: 5,
    chapters,
    activities(url) {
      const id = url.searchParams.get("chapterId");
      if (id === chapters[0].id)
        return {
          status: 200,
          value: {
            items: [
              {
                id: "course",
                businessType: 8,
                businessId: entry(COURSE, "a").businessId,
                required: 1,
              },
            ],
            recordCount: 1,
          },
        };
      if (id === chapters[1].id)
        return {
          status: 422,
          value: { errorCode: 17, message: "章节不可访问" },
        };
      return {
        status: 200,
        value: {
          items: [{ id: "broken-link", businessValue: "not a url" }],
          recordCount: 1,
        },
      };
    },
  });
  try {
    const result = await discover([classUrl], f.ctx);
    assert.equal(result.complete, false);
    assert.deepEqual(
      result.found.map((task) => task.url),
      [classCourse],
    );
    assert.equal(result.issues.length, 1);
    assert.match(
      result.issues[0].message,
      /第二章.*第 1 页.*422.*chapter-activity-list.*17.*章节不可访问/,
    );
    assert.match(result.issues[0].message, /第三章.*broken-link.*Invalid URL/);
    const task = f.repo.upsert(classUrl, "a");
    assert.equal(task.details.message, result.issues[0].message);
    assert.equal(f.stats().posts, 0);
  } finally {
    await f.close();
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

const activity = (n: number) => ({
  id: `activity-${n}`,
  businessType: 8,
  businessId: `11111111-1111-4111-8111-${n.toString(16).padStart(12, "0")}`,
  required: n % 2,
});

test("class pagination follows the declared total across short pages and retains class source identity", async () => {
  const items = Array.from({ length: 26 }, (_, n) => activity(n));
  const pages = [
    items.slice(0, 5),
    items.slice(5, 15),
    items.slice(15, 25),
    items.slice(25),
  ];
  const requested: number[] = [];
  const f = await fixture({
    status: 5,
    activities(url) {
      assert.ok(url.pathname.endsWith("/chapter-activity-list"));
      assert.equal(url.searchParams.get("pageSize"), "10");
      const page = Number(url.searchParams.get("page"));
      requested.push(page);
      return {
        status: 200,
        value: { items: pages[page - 1], recordCount: 26 },
      };
    },
  });
  try {
    const result = await discover([classUrl], f.ctx);
    assert.equal(result.complete, true);
    assert.deepEqual(requested, [1, 2, 3, 4]);
    assert.equal(result.found.length, 26);
    assert.equal(f.repo.queue("a", "learn").length, 26);
    for (const [n, task] of result.found.entries()) {
      assert.equal(task.sourceId, classId);
      assert.equal(task.sourceType, "5");
      assert.equal(task.required, String(n % 2));
    }
  } finally {
    await f.close();
  }
});

test("incomplete or changing page counts preserve discovered courses and remain retryable", async () => {
  for (const mode of ["repeated", "empty", "changed", "missing-total"]) {
    let reads = 0;
    const f = await fixture({
      status: 5,
      activities(url) {
        const page = Number(url.searchParams.get("page"));
        reads++;
        return {
          status: 200,
          value: {
            items:
              page === 1 || mode === "repeated"
                ? [activity(0)]
                : mode === "changed"
                  ? [activity(1)]
                  : [],
            recordCount:
              mode === "missing-total"
                ? undefined
                : mode === "changed" && page > 1
                  ? 3
                  : 2,
          },
        };
      },
    });
    try {
      const result = await discover([classUrl], f.ctx);
      assert.equal(result.complete, false);
      assert.equal(result.found.length, mode === "changed" ? 2 : 1);
      assert.equal(result.issues[0].recovery, "retry");
      assert.match(result.issues[0].message, /活动总数|未返回新的活动/);
      assert.equal(reads, mode === "missing-total" ? 1 : 2);
    } finally {
      await f.close();
    }
  }
});

test("class exams are not courses and manual activities do not erase usable tasks", async () => {
  const f = await fixture({
    status: 5,
    activities() {
      return {
        status: 200,
        value: {
          items: [
            activity(0),
            { ...activity(1), businessType: 1 },
            { ...activity(2), businessType: 2 },
          ],
          recordCount: 3,
        },
      };
    },
  });
  try {
    const result = await discover([classUrl], f.ctx);
    assert.equal(result.complete, false);
    assert.deepEqual(
      result.found.map((task) => task.kind),
      ["course", "exam"],
    );
    assert.equal(f.repo.queue("a", "learn").length, 1);
    assert.equal(f.repo.queue("a", "exam").length, 1);
    assert.equal(result.issues[0].recovery, "blocked");
    assert.match(result.issues[0].message, /活动类型 2 需要在平台处理/);
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
      assert.equal(result.found[0].url, classCourse);
      assert.equal(f.stats().posts, options.status === 5 ? 0 : 1);
    } finally {
      await f.close();
    }
  }
});
