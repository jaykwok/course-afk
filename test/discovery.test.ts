import test from "node:test";
import assert from "node:assert/strict";
import { Repository } from "../src/storage/repository.ts";
import {
  traverse,
  subjectManifest,
  extractLinks,
  discover,
  catalogPageState,
  catalogManifest,
} from "../src/platform/discovery.ts";
import { temporary, COURSE, EXAM, SUBJECT } from "./helpers.ts";
import { entry, CASE_POOL, normalize } from "../src/domain/identity.ts";
import { runInNewContext } from "node:vm";
import { learn } from "../src/platform/learning.ts";
import type { DiscoveryContext } from "../src/platform/discovery.ts";
import { configuration } from "../src/config.ts";
import { HOME } from "../src/config.ts";
import { InputActionError, type Tab } from "nodriver-ts";
import { digest } from "../src/utils.ts";

const snapshot = (url: string, values: string[], extra = {}) => ({
  url,
  values,
  status: 200,
  article: false,
  busy: false,
  interactive: false,
  title: "专题",
  text: "",
  ready: true,
  ...extra,
});

test("topic boundaries stop cross-topic expansion and home links while retaining explicit extra roots", async () => {
  const temp = await temporary(),
    repo = new Repository(temp.dir);
  const root =
    "https://cms.mylearning.cn/safe/topic/resource/2030/project/pc.html";
  const child = root.replace("pc.html", "parts/lesson.html");
  const outside = root.replace("project/pc.html", "other/pc.html");
  try {
    for (const includeExtra of [false, true]) {
      const account = String(includeExtra),
        seen: string[] = [];
      const parent = repo.upsert(root, account),
        old = repo.upsert(outside, account);
      repo.link(parent, old);
      const result = await traverse(
        includeExtra ? [root, outside] : [root],
        async (task) => {
          seen.push(task.url);
          return {
            links:
              task.url === root
                ? [child, outside, HOME]
                : task.url === child
                  ? [COURSE, root]
                  : [EXAM],
            complete: true,
          };
        },
        {
          repo,
          lease: {
            account,
            generation: 0,
            fingerprint: "f",
            signal: new AbortController().signal,
          },
          report() {},
        },
      );
      assert.equal(result.complete, true);
      assert.deepEqual(result.related, includeExtra ? [] : [outside]);
      assert.deepEqual(
        seen,
        includeExtra ? [root, child, outside] : [root, child],
      );
      assert.equal(result.found.length, includeExtra ? 2 : 1);
      assert.equal(
        repo.db
          .prepare("SELECT count(*) n FROM edges WHERE parent=? AND child=?")
          .get(parent.id, old.id)?.n,
        includeExtra ? 1 : 0,
      );
      assert.equal(repo.queue(account, "discover").length, 0);
    }
  } finally {
    repo.close();
    await temp.close();
  }
});

test("redirects back to a scanned catalog or platform home cannot start another crawl", async () => {
  const temp = await temporary(),
    repo = new Repository(temp.dir);
  const root = "https://cms.mylearning.cn/topic/project/pc.html",
    alias = root.replace("pc", "old"),
    home = root.replace("pc", "home");
  let reads = 0;
  const opened: string[] = [];
  const ctx = {
    repo,
    lease: { account: "a", signal: new AbortController().signal },
    report() {},
    auth: {
      assert() {},
      async navigate(_scope: unknown, url: string) {
        opened.push(url);
        return {
          url: url === home ? HOME : root,
          async close() {},
          async queryAll() {
            return [];
          },
          async evaluate() {
            assert.equal(
              url,
              root,
              "redirected pages must not have their links extracted",
            );
            reads++;
            return snapshot(root, [COURSE, alias, home]);
          },
        };
      },
    },
  } as unknown as DiscoveryContext;
  try {
    const result = await discover([root], ctx);
    assert.equal(result.complete, true);
    assert.equal(result.found.length, 1);
    assert.equal(result.ignored, 2);
    assert.equal(reads, 1);
    assert.deepEqual(opened, [root, alias, home]);
  } finally {
    repo.close();
    await temp.close();
  }
});

test("generic page evidence distinguishes articles, missing resources and unresolved pages", () => {
  const url = "https://cms.mylearning.cn/topic/arbitrary/anything.html";
  const article = snapshot(url, [], {
    article: true,
    text: "一段正常内容介绍。".repeat(20),
  });
  assert.equal(catalogPageState(article), "article");
  for (const status of [404, 410])
    assert.equal(catalogPageState({ ...article, status }), "missing");
  for (const status of [401, 403, 429, 500, 503])
    assert.equal(catalogPageState({ ...article, status }), "unknown");
  for (const extra of [
    { ready: false },
    { busy: true },
    { interactive: true },
    { article: false },
    { text: "请先登录" },
  ])
    assert.equal(catalogPageState({ ...article, ...extra }), "unknown");
  assert.equal(
    catalogPageState(snapshot(url, [], { text: "sorry,file not found" })),
    "missing",
  );
  assert.equal(
    catalogPageState(snapshot(url, [], { text: "暂无内容，敬请期待…" })),
    "missing",
  );
  assert.equal(
    catalogPageState(snapshot(url.replace("anything", "404"), [])),
    "unknown",
  );
});

test("graph pruning removes only resolved empty branches and preserves resources, uncertainty and useful cycles", async () => {
  const temp = await temporary(),
    repo = new Repository(temp.dir);
  const root = "https://cms.mylearning.cn/topic/root.html";
  const urls = [
    "news",
    "leaf",
    "empty-a",
    "empty-b",
    "mixed",
    "loop",
    "unknown",
  ].map((p) => `https://cms.mylearning.cn/topic/${p}.html`);
  const [news, leaf, emptyA, emptyB, mixed, loop, unknown] = urls;
  const counts = new Map<string, number>();
  const graph: Record<string, string[]> = {
    [root]: [news, emptyA, mixed, unknown],
    [news]: [leaf],
    [leaf]: [],
    [emptyA]: [emptyB],
    [emptyB]: [emptyA],
    [mixed]: [loop, COURSE],
    [loop]: [mixed],
    [unknown]: [],
  };
  try {
    repo.outcome(repo.upsert(unknown, "a").id, "READY", { ignored: true });
    const result = await traverse(
      [root, leaf],
      async (task) => {
        counts.set(task.url, (counts.get(task.url) || 0) + 1);
        return {
          links: graph[task.url],
          complete: task.url !== unknown,
          ...(task.url === leaf ? { ignored: "已确认内容页没有学习入口" } : {}),
        };
      },
      {
        repo,
        lease: {
          account: "a",
          generation: 0,
          fingerprint: "x",
          signal: new AbortController().signal,
        },
        report() {},
      },
    );
    assert.equal(result.complete, false);
    assert.equal(result.issues.length, 1);
    assert.equal(result.ignored, 4);
    assert.equal(result.found.length, 1);
    assert.equal(Math.max(...counts.values()), 1);
    for (const url of [news, leaf, emptyA, emptyB]) {
      const task = repo.upsert(url, "a");
      assert.equal(task.details.ignored, true);
      assert.equal(
        repo.db
          .prepare("SELECT count(*) n FROM queues WHERE task_id=?")
          .get(task.id)?.n,
        0,
      );
    }
    for (const url of [root, mixed, loop, unknown])
      assert.notEqual(repo.upsert(url, "a").details.ignored, true);
    assert.equal(repo.queue("a", "learn")[0].url, COURSE);
  } finally {
    repo.close();
    await temp.close();
  }
});

test("static discovery reuses one catalog tab and inspects unknown links regardless of their path", async () => {
  const temp = await temporary(),
    repo = new Repository(temp.dir);
  const root = "https://cms.mylearning.cn/topic/root.html",
    child = "https://cms.mylearning.cn/topic/museum/404.html";
  let created = 0,
    closed = 0;
  const visited: string[] = [];
  const ctx = {
    repo,
    lease: { account: "a", signal: new AbortController().signal },
    report() {},
    auth: {
      assert() {},
      async navigate(
        _scope: unknown,
        url: string,
        _lease: unknown,
        existing?: any,
      ) {
        visited.push(url);
        if (existing) {
          existing.url = url;
          return existing;
        }
        created++;
        return {
          url,
          async close() {
            closed++;
          },
          async queryAll() {
            return [];
          },
          async evaluate() {
            return snapshot(this.url, this.url === root ? [child] : [COURSE]);
          },
        };
      },
    },
  } as unknown as DiscoveryContext;
  try {
    const result = await discover([root], ctx);
    assert.equal(result.complete, true);
    assert.deepEqual(visited, [root, child]);
    assert.equal(result.found[0].url, COURSE);
    assert.equal(created, 1);
    assert.equal(closed, 1);
  } finally {
    repo.close();
    await temp.close();
  }
});

test("missing-page evidence is pruned, but a course mentioning file not found is retained", async () => {
  const root = "https://cms.mylearning.cn/topic/retired.html";
  const ctx = {
    lease: { signal: new AbortController().signal },
    auth: { assert() {} },
  } as unknown as DiscoveryContext;
  for (const page of [
    snapshot("https://cms.mylearning.cn/topic/tiji/404.html", [], {
      status: 404,
    }),
    snapshot(root, [], { text: "sorry,file not found" }),
    snapshot(root, [COURSE], {
      text: "案例：sorry,file not found，如何排查404",
    }),
    snapshot("https://cms.mylearning.cn/topic/404.html", [COURSE]),
  ]) {
    const tab = {
      url: root,
      async evaluate() {
        return page;
      },
      async queryAll() {
        return [];
      },
    } as unknown as Tab;
    const result = await catalogManifest(tab, ctx);
    assert.equal(!!result.ignored, page.values.length === 0);
    assert.deepEqual(result.links, page.values);
  }
});

test("failed native expansion preserves collected courses and waits for input cleanup", async () => {
  const root = "https://cms.mylearning.cn/topic/expand.html";
  let cleaned = false;
  const report = {
    action: "click",
    status: "interrupted",
    seed: 1,
    randomState: 1,
    completedSegments: 0,
    events: 0,
    trace: [],
    cleanupErrors: [],
    phase: "done",
    cleanup: "completed",
  } as const;
  const failure = new InputActionError(
    { ...report, trace: [], cleanupErrors: [] },
    new Error("target moved"),
    Promise.resolve().then(() => {
      cleaned = true;
      return {
        status: "completed",
        report: { ...report, trace: [], cleanupErrors: [] },
      };
    }),
  );
  const tab = {
    url: root,
    async evaluate() {
      return snapshot(root, [COURSE]);
    },
    async queryAll() {
      return [
        {
          async evaluate() {
            return { text: "更多", disabled: false, href: "", values: [] };
          },
          async click() {
            throw failure;
          },
        },
      ];
    },
  } as unknown as Tab;
  const ctx = {
    lease: { signal: new AbortController().signal },
    auth: { assert() {} },
  } as unknown as DiscoveryContext;
  const result = await catalogManifest(tab, ctx);
  assert.equal(cleaned, true);
  assert.equal(result.complete, false);
  assert.deepEqual(result.links, [COURSE]);
  assert.match(result.message!, /已发现任务已保留/);
});

test("retrying only a failed child resolves ancestor discovery without completing unfinished courses", async () => {
  const temp = await temporary(),
    repo = new Repository(temp.dir);
  const root = "https://cms.mylearning.cn/topic/parent.html",
    child = "https://cms.mylearning.cn/topic/child.html";
  const ctx = {
    repo,
    lease: {
      account: "a",
      generation: 0,
      fingerprint: "x",
      signal: new AbortController().signal,
    },
    report() {},
  };
  try {
    await traverse(
      [root],
      async (task) =>
        task.url === root
          ? { links: [child], complete: true }
          : { links: [COURSE], complete: false },
      ctx,
    );
    const parent = repo.upsert(root, "a");
    assert.equal(repo.task(parent.id).details.discoveryComplete, false);
    await traverse(
      [child],
      async () => ({ links: [COURSE], complete: true }),
      ctx,
    );
    assert.equal(repo.task(parent.id).details.discoveryComplete, true);
    assert.equal(repo.task(parent.id).status, "WAITING_CHILDREN");
    assert.equal(repo.queue("a", "failure").length, 0);
    repo.complete(repo.upsert(COURSE, "a"), { finishStatus: 2 });
    assert.equal(repo.task(parent.id).status, "COMPLETED");
  } finally {
    repo.close();
    await temp.close();
  }
});

test("reparse removes old entry aliases and article failures while retaining history and account isolation", async () => {
  const temp = await temporary(),
    repo = new Repository(temp.dir);
  const root = "https://cms.mylearning.cn/topic/parent.html",
    article = "https://cms.mylearning.cn/topic/site.html";
  try {
    const parent = repo.upsert(root, "a"),
      old = repo.upsert(article, "a"),
      other = repo.upsert(article, "b");
    repo.link(parent, old);
    repo.review(old, "discovery_incomplete");
    repo.review(other, "discovery_incomplete");
    const url = "https://cms.mylearning.cn//topic//nested.html#top",
      businessId = digest(url);
    const id = digest(
      JSON.stringify([
        "cms.mylearning.cn",
        "a",
        "entry",
        businessId,
        "",
        "",
        "",
      ]),
    );
    repo.db
      .prepare(
        "INSERT INTO tasks(id,account,kind,url,identity,status) VALUES(?,'a','entry',?,?,'NEEDS_REVIEW')",
      )
      .run(
        id,
        url,
        JSON.stringify({ id, account: "a", kind: "entry", url, businessId }),
      );
    const alias = repo.task(id);
    repo.link(parent, alias);
    repo.enqueue(alias, "discover");
    const run = repo.begin(alias, "learn");
    repo.end(run, "NEEDS_REVIEW", {});
    await traverse(
      [root],
      async (task) =>
        task.url === article
          ? { links: [], complete: true, ignored: "内容页无学习入口" }
          : {
              links: task.url === root ? [normalize(url), article] : [COURSE],
              complete: true,
            },
      {
        repo,
        lease: {
          account: "a",
          generation: 0,
          fingerprint: "x",
          signal: new AbortController().signal,
        },
        report() {},
      },
    );
    assert.equal(repo.queue("a", "failure").length, 0);
    assert.equal(repo.queue("a", "discover").length, 0);
    assert.equal(repo.queue("b", "failure").length, 1);
    assert.equal(repo.task(old.id).details.ignored, true);
    assert.equal(
      repo.task(alias.id).details.canonicalId,
      repo.upsert(url, "a").id,
    );
    assert.equal(
      repo.db
        .prepare("SELECT count(*) n FROM runs WHERE task_id=?")
        .get(alias.id)?.n,
      1,
    );
    assert.equal(
      repo.db
        .prepare("SELECT count(*) n FROM edges WHERE child=? OR parent=?")
        .get(alias.id, alias.id)?.n,
      0,
    );
    repo.complete(repo.upsert(COURSE, "a"), { finishStatus: 2 });
    assert.equal(repo.task(parent.id).status, "COMPLETED");
  } finally {
    repo.close();
    await temp.close();
  }
});

test("static topic aliases are deduplicated and placeholder pages are pruned before opening or queuing", async () => {
  const root = "https://cms.mylearning.cn/topic/root.html";
  const aliases = [
    root,
    "https://cms.mylearning.cn//topic//root.html#overview",
  ];
  assert.equal(normalize(aliases[1]), root);
  const placeholders = ["null.html", "none.html", "%75ndefined.html"].map(
    (p) => `https://cms.mylearning.cn/topic/${p}`,
  );
  const valid = "https://cms.mylearning.cn/topic/nullable/pc.html";
  assert.deepEqual(
    extractLinks([...aliases, ...placeholders, valid, COURSE], root),
    [root, valid, COURSE],
  );
  const temp = await temporary(),
    repo = new Repository(temp.dir);
  try {
    const parent = repo.upsert(root, "a"),
      empty = repo.upsert(placeholders[0], "a");
    repo.link(parent, empty);
    repo.enqueue(empty, "discover");
    repo.review(empty, "discovery_incomplete");
    const seen: string[] = [];
    const result = await traverse(
      [...aliases, ...placeholders],
      async (task) => {
        seen.push(task.url);
        return { links: [...placeholders, COURSE], complete: true };
      },
      {
        repo,
        lease: {
          account: "a",
          generation: 0,
          fingerprint: "x",
          signal: new AbortController().signal,
        },
        report() {},
      },
    );
    assert.deepEqual(seen, [root]);
    assert.equal(result.complete, true);
    assert.equal(repo.queue("a", "failure").length, 0);
    assert.equal(repo.queue("a", "discover").length, 0);
    assert.equal(repo.task(empty.id).details.ignored, true);
    repo.complete(repo.upsert(COURSE, "a"), { finishStatus: 2 });
    assert.equal(repo.task(parent.id).status, "COMPLETED");
  } finally {
    repo.close();
    await temp.close();
  }
});

test("large real-shaped catalogs exceed 100 containers while reusing one platform API page", async () => {
  const temp = await temporary(),
    repo = new Repository(temp.dir);
  const root = "https://cms.mylearning.cn/topic/large.html";
  const subjects = Array.from(
    { length: 125 },
    (_, i) =>
      `https://kc.zhixueyun.com/#/study/subject/detail/${String(i + 1).padStart(8, "0")}-1111-4111-8111-111111111111`,
  );
  let opened = 0,
    closed = 0,
    reads = 0;
  try {
    const ctx = {
      repo,
      lease: { account: "a", signal: new AbortController().signal },
      report() {},
      auth: {
        assert() {},
        async navigate(_scope: unknown, url: string) {
          opened++;
          return {
            url,
            async close() {
              closed++;
            },
            async queryAll() {
              return [];
            },
            async waitForFunction() {},
            async evaluate(_fn: unknown, arg: any) {
              if (url === root) return snapshot(url, subjects);
              assert.ok(
                arg.url.includes("/subject/chapter-progress?courseId="),
              );
              reads++;
              return {
                status: 200,
                value: [
                  {
                    courseChapterSections: [
                      {
                        sectionType: 10,
                        attachmentId: entry(COURSE, "a").businessId,
                        required: 1,
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
    const result = await discover([root], ctx);
    assert.equal(result.expanded, 126);
    assert.equal(result.complete, true);
    assert.equal(result.found.length, 125);
    assert.equal(reads, 125);
    assert.equal(opened, 2);
    assert.equal(closed, 2);
    assert.equal(repo.queue("a", "failure").length, 0);
  } finally {
    repo.close();
    await temp.close();
  }
});

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
                  return snapshot(url, [
                    `<a href="${CASE_POOL}">案例库</a><a href="http://ctexpert.cn/expert-assist-h5/casePool">移动案例库</a>`,
                  ]);
                if (url === nested) return snapshot(url, [SUBJECT, CASE_POOL]);
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
      const result = await discover([root, CASE_POOL], ctx);
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
          async navigate(_scope: unknown, _url: string) {
            return {
              async close() {},
              async waitForFunction() {},
              async reload() {
                reloads++;
              },
              async evaluate(_fn: unknown, arg: any) {
                if (!arg?.url) return []; // No unfinished DOM items; refresh server evidence.
                if (arg.url.includes(entry(child, account).businessId))
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
      [SUBJECT, child],
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
    assert.equal(repo.queue("a", "failure").length, 1);
    assert.equal(repo.queue("a", "failure")[0].url, second);
    assert.equal(
      repo.task(repo.upsert(root, "a").id).status,
      "WAITING_CHILDREN",
    );
    assert.ok(!repo.queue("a", "discover").some((t) => t.url === root));
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
    assert.equal(repo.queue("a", "failure").length, 0);
    assert.equal(
      repo.task(repo.upsert(child, "a").id).details.reason,
      "discovery_pending",
    );
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
