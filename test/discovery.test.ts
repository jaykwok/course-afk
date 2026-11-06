import test from "node:test";
import assert from "node:assert/strict";
import { Repository } from "../src/storage/repository.ts";
import {
  traverse,
  subjectManifest,
  extractLinks,
  discover,
} from "../src/platform/discovery.ts";
import { temporary, COURSE, EXAM, SUBJECT } from "./helpers.ts";
import { entry, CASE_POOL } from "../src/domain/identity.ts";
import { runInNewContext } from "node:vm";
import { learn } from "../src/platform/learning.ts";
import type { DiscoveryContext } from "../src/platform/discovery.ts";
import { configuration } from "../src/config.ts";

test("topic and case-pool adapters share recursive traversal, aliases, cycles and partial-result recovery", async () => {
  const temp = await temporary(),
    repo = new Repository(temp.dir);
  const root = "https://cms.mylearning.cn/topic/root.html",
    nested = "https://cms.mylearning.cn/topic/nested.html";
  try {
    for (const incomplete of [false, true]) {
      const account = String(incomplete),
        opened: string[] = [],
        pages: number[] = [];
      const ctx = {
        repo,
        lease: { account, signal: new AbortController().signal },
        report() {},
        auth: {
          assert() {},
          async navigate(_scope: unknown, url: string) {
            opened.push(url);
            return {
              url,
              async close() {},
              async queryAll() {
                return [];
              },
              async waitForFunction() {},
              async evaluate(fn: Function, arg: any) {
                if (url === root)
                  return [
                    `<a href="${CASE_POOL}">案例库</a><a href="http://ctexpert.cn/expert-assist-h5/casePool">移动案例库</a>`,
                  ];
                if (url === nested) return [SUBJECT, CASE_POOL];
                if (url === SUBJECT)
                  return {
                    status: 200,
                    value: [
                      {
                        courseChapterSections: [
                          {
                            sectionType: 10,
                            attachmentId: entry(COURSE, account).businessId,
                            required: 1,
                          },
                          {
                            sectionType: 9,
                            resourceId: entry(EXAM, account).businessId,
                          },
                        ],
                      },
                    ],
                  };
                assert.equal(url, CASE_POOL);
                assert.equal(arg, undefined);
                return runInNewContext(`(${fn.toString()})()`, {
                  AbortSignal,
                  location: new URL(CASE_POOL),
                  localStorage: { getItem: () => "synthetic-token" },
                  fetch: async (path: string, options: any) => {
                    assert.equal(options.headers.token, "synthetic-token");
                    if (path.includes("/tag/listTree"))
                      return {
                        ok: true,
                        json: async () => ({
                          code: 200,
                          data: [
                            { name: "案例资源", childrenList: [{ id: "tag" }] },
                          ],
                        }),
                      };
                    const page = JSON.parse(options.body).pageNum;
                    pages.push(page);
                    return {
                      ok: true,
                      json: async () => ({
                        code: incomplete && page === 2 ? 500 : 200,
                        data: {
                          total: 2,
                          records: [
                            {
                              id: String(page),
                              url: page === 1 ? nested : COURSE,
                            },
                          ],
                        },
                      }),
                    };
                  },
                });
              },
            };
          },
        },
      } as unknown as DiscoveryContext;
      const result = await discover([root], ctx);
      assert.equal(result.complete, !incomplete);
      assert.deepEqual(pages, [1, 2]);
      assert.deepEqual(opened, [root, CASE_POOL, nested, SUBJECT]);
      assert.equal(repo.queue(account, "learn").length, incomplete ? 1 : 2);
      assert.equal(repo.queue(account, "exam").length, 1);
      assert.equal(
        repo.task(repo.upsert(CASE_POOL, account).id).details.discoveryComplete,
        !incomplete,
      );
      const activity = subjectManifest(
        [
          {
            courseChapterSections: [
              { id: "url", resourceId: "r", sectionType: 3, url: CASE_POOL },
            ],
          },
        ],
        repo.upsert(SUBJECT, account),
      );
      assert.deepEqual(activity.links, [CASE_POOL]);
    }
  } finally {
    repo.close();
    await temp.close();
  }
});

test("subject learning refreshes nested activity progress without erasing incomplete child discovery", async () => {
  const temp = await temporary(),
    repo = new Repository(temp.dir);
  try {
    for (const incompleteChild of [false, true]) {
      const account = String(incompleteChild),
        task = repo.upsert(SUBJECT, account);
      let reads = 0,
        reloads = 0;
      const child = SUBJECT.replace(
        "33333333-3333-4333-8333-333333333333",
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      );
      const ctx = {
        repo,
        lease: {
          account,
          generation: 0,
          fingerprint: "x",
          signal: new AbortController().signal,
        },
        report: () => {},
        confirm: async () => false,
        auth: {
          assert() {},
          async navigate(_scope: unknown, url: string) {
            return {
              async close() {},
              async waitForFunction() {},
              async reload() {
                reloads++;
              },
              async evaluate(_fn: unknown, arg: any) {
                if (!arg?.url) return []; // No unfinished DOM items; refresh server evidence.
                if (url === child)
                  return {
                    status: 200,
                    value: [{ courseChapterSections: [{ sectionType: 77 }] }],
                  };
                reads++;
                return {
                  status: 200,
                  value: [
                    {
                      courseChapterSections: [
                        {
                          id: "s",
                          resourceId: "r",
                          sectionType: 3,
                          url: incompleteChild ? child : COURSE,
                          progress: {
                            courseId: task.businessId,
                            sectionId: "s",
                            resourceId: "r",
                            finishStatus: reads > 1 ? 2 : 0,
                          },
                        },
                      ],
                    },
                  ],
                };
              },
            };
          },
        },
      } as unknown as DiscoveryContext;
      await learn(task, ctx, configuration(undefined, temp.dir), () => {});
      assert.equal(reads, 2);
      assert.equal(reloads, 1);
      assert.equal(repo.task(task.id).status === "COMPLETED", !incompleteChild);
      assert.equal(
        repo.task(task.id).details.discoveryComplete,
        !incompleteChild,
      );
      assert.equal(repo.task(task.id).details.residual, false);
      if (incompleteChild)
        assert.equal(repo.task(task.id).details.reason, "discovery_incomplete");
      if (!incompleteChild)
        assert.equal(
          repo.queue(account, "learn").some((t) => t.id === task.id),
          false,
        );
    }
  } finally {
    repo.close();
    await temp.close();
  }
});

test("topic HTML extracts only containers and preserves relative course sources", () => {
  const root =
    "https://cms.mylearning.cn/safe/topic/resource/2020/lyljjh/pc.html";
  const classes = [
    "75c755b8-a236-45e3-b599-fdb54db0ccea",
    "5779989b-619c-4b50-8e0a-64799735c18b",
  ].map((id) => `https://kc.zhixueyun.com/#/train-new/class-detail/${id}`);
  const topics = ["2024/yqx2024", "2024/qd", "2025/zycp"].map(
    (p) => `https://cms.mylearning.cn/safe/topic/resource/${p}/pc.html`,
  );
  const html = `<html><head><script src="/safe/topic/common/header.js"></script>
    <script src="/safe/topic/common/footer.js"></script><script src="/safe/topic/common/login.js"></script>
    <script>if (isMobile) location.href = 'm.html'; const color = '#081a53';</script>
    <!-- <a href="https://cms.mylearning.cn/topic/retired.html">已移除内容</a> --></head>
    <body>${classes.map((url) => `<div onclick="window.open('${url}')">培训班</div>`).join("")}
    ${topics.map((url) => `<a href="${url}">专题</a>`).join("")}</body></html>`;
  assert.deepEqual(
    extractLinks([html], root).sort(),
    [...classes, ...topics].sort(),
  );
  const query = `${COURSE}?type=6&required=1&sourceId=${entry(SUBJECT, "a").businessId}`;
  assert.deepEqual(
    extractLinks(
      [`<a href="${query.replaceAll("&", "&amp;")}">课程</a>`],
      root,
    ),
    [entry(query, "a").url],
  );
  assert.deepEqual(
    extractLinks(
      [`<a href="${query.slice(query.indexOf("#"))}">课程</a>`],
      root,
    ),
    [entry(query, "a").url],
  );
  assert.deepEqual(
    extractLinks(
      [
        "/safe/topic/common/app.js",
        "<html></html>",
        "https://outside.test/topic/a.html",
      ],
      root,
    ),
    [],
  );
});

test("URL activities require matching nested server progress and keep destination courses independent", async () => {
  const temp = await temporary(),
    repo = new Repository(temp.dir);
  try {
    const task = repo.upsert(SUBJECT, "a");
    const section = {
      id: "section-1",
      resourceId: "resource-1",
      sectionType: 3,
      url: COURSE,
      finishStatus: 2,
      progress: {
        courseId: task.businessId,
        sectionId: "section-1",
        resourceId: "resource-1",
        finishStatus: 0,
      },
    };
    const payload = (s: unknown) => [{ courseChapterSections: [s] }];
    const manifest = subjectManifest(payload(section), task);
    assert.equal(manifest.complete, true);
    assert.equal(manifest.residual, true);
    const ctx = {
      repo,
      lease: {
        account: "a",
        generation: 0,
        fingerprint: "x",
        signal: new AbortController().signal,
      },
      report: () => {},
    };
    await traverse([SUBJECT], async () => manifest, ctx);
    repo.complete(repo.upsert(COURSE, "a"), { finishStatus: 2 });
    assert.notEqual(repo.task(task.id).status, "COMPLETED");
    assert.ok(repo.queue("a", "learn").some((t) => t.id === task.id));
    const done = {
      ...section,
      finishStatus: 0,
      progress: { ...section.progress, finishStatus: 2 },
    };
    assert.equal(subjectManifest(payload(done), task).residual, false);
    for (const progress of [
      undefined,
      { ...done.progress, courseId: "wrong" },
      { ...done.progress, sectionId: "wrong" },
      { ...done.progress, resourceId: "wrong" },
    ])
      assert.equal(
        subjectManifest(payload({ ...done, progress }), task).residual,
        true,
      );
    // Completed URL activities do not require completion of their target courses.
    const other = COURSE.replace(
      "11111111-1111-4111-8111-111111111111",
      "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    );
    await traverse(
      [SUBJECT],
      async () => subjectManifest(payload({ ...done, url: other }), task),
      ctx,
    );
    assert.equal(repo.task(task.id).status, "COMPLETED");
    assert.equal(
      repo.queue("a", "learn").some((t) => t.id === task.id),
      false,
    );
    assert.ok(repo.queue("a", "learn").some((t) => t.url === other));
    const document = subjectManifest(
      payload({ ...done, url: "https://cms.mylearning.cn/files/a.pdf" }),
      task,
    );
    assert.equal(document.complete, true);
    assert.deepEqual(document.links, []);
  } finally {
    repo.close();
    await temp.close();
  }
});

test("an optional URL target cannot downgrade a required chapter or hide incomplete descendant discovery", async () => {
  const temp = await temporary(),
    repo = new Repository(temp.dir);
  try {
    const task = repo.upsert(SUBJECT, "a");
    const course = `${COURSE}/6/1/${task.businessId}`;
    const direct = {
      sectionType: 10,
      attachmentId: entry(COURSE, "a").businessId,
      required: 1,
    };
    const activity = {
      id: "s",
      resourceId: "r",
      sectionType: 3,
      url: course,
      progress: {
        courseId: task.businessId,
        sectionId: "s",
        resourceId: "r",
        finishStatus: 2,
      },
    };
    for (const sections of [
      [direct, activity],
      [activity, direct],
    ]) {
      const m = subjectManifest([{ courseChapterSections: sections }], task);
      assert.equal(m.optionalLinks?.includes(course), false);
    }
    const child = "https://cms.mylearning.cn/topic/incomplete.html";
    const ctx = {
      repo,
      lease: {
        account: "a",
        generation: 0,
        fingerprint: "x",
        signal: new AbortController().signal,
      },
      report: () => {},
    };
    await traverse(
      [SUBJECT],
      async (t) =>
        t.kind === "subject"
          ? subjectManifest(
              [{ courseChapterSections: [{ ...activity, url: child }] }],
              task,
            )
          : { links: [COURSE], complete: false },
      ctx,
    );
    repo.complete(repo.upsert(COURSE, "a"), { finishStatus: 2 });
    repo.reconcile(task.id);
    assert.equal(repo.task(task.id).details.discoveryComplete, false);
    assert.notEqual(repo.task(task.id).status, "COMPLETED");
  } finally {
    repo.close();
    await temp.close();
  }
});
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
