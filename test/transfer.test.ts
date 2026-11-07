import test from "node:test";
import assert from "node:assert/strict";
import { Repository } from "../src/storage/repository.ts";
import {
  exportBundle,
  importBundle,
  normalizeBundle,
  adoptImports,
  IMPORT_INBOX,
  type QueueBundle,
} from "../src/storage/transfer.ts";
import { temporary, COURSE, EXAM, SUBJECT, application } from "./helpers.ts";
import { token } from "../src/utils.ts";
import { readFile } from "node:fs/promises";
import { traverse } from "../src/platform/discovery.ts";

const second = COURSE.replaceAll("11111111", "aaaaaaaa");
const bundle = (urls = [COURSE]): QueueBundle => ({
  format: "course-pilot.tasks",
  version: 1,
  exportedAt: new Date().toISOString(),
  tasks: urls.map((url) => ({ url, state: "pending" })),
  sources: [],
});

test("portable exports select pending or failed work, preserve ancestry and rebind to a recipient", async () => {
  const temp = await temporary(),
    repo = new Repository(temp.dir);
  try {
    const parent = repo.upsert(SUBJECT, "sender"),
      first = repo.upsert(COURSE, "sender"),
      failed = repo.upsert(second, "sender");
    repo.link(parent, first);
    repo.link(parent, failed, false);
    repo.enqueue(first, "learn");
    repo.review(failed, "opaque_platform_condition", {
      recovery: "blocked",
      message: "需管理员处理 token=secret",
      secret: "private-value",
    });
    const exam = repo.upsert(EXAM, "sender");
    repo.intent(exam, "submit", { secret: "private-record" });
    repo.saveHistory(exam, { secret: "private-history" });
    const pending = exportBundle(repo, "sender", "pending"),
      failures = exportBundle(repo, "sender", "failed");
    assert.deepEqual(
      pending.tasks.map((t) => t.url),
      [COURSE],
    );
    assert.equal(failures.tasks[0].issue?.reason, "opaque_platform_condition");
    assert.equal(failures.tasks[0].issue?.recovery, "blocked");
    assert.match(failures.tasks[0].issue!.message, /需管理员处理/);
    assert.deepEqual(failures.sources, [
      { parent: SUBJECT, child: second, required: false },
    ]);
    const all = exportBundle(repo, "sender", "all");
    assert.equal(all.tasks.length, 2);
    assert.doesNotMatch(
      JSON.stringify(all),
      /sender|private-value|private-record|private-history|token=secret|manualSubmit/,
    );
    assert.deepEqual(importBundle(repo, all, "recipient"), {
      added: 2,
      retained: 0,
    });
    assert.deepEqual(importBundle(repo, all, "recipient"), {
      added: 0,
      retained: 2,
    });
    assert.equal(repo.queue("recipient", "learn").length, 2);
    assert.equal(repo.queue("recipient", "discover").length, 0); // Sources are not executable roots.
    assert.equal(repo.queue("recipient", "failure").length, 0);
    assert.equal(
      repo.upsert(second, "recipient").details.importedIssue.recovery,
      "blocked",
    );
    assert.equal(repo.queue("sender", "failure").length, 1);
    assert.equal(
      repo.pending(repo.upsert(EXAM, "recipient"), "submit").length,
      0,
    );
    repo.complete(repo.upsert(COURSE, "recipient"), { source: "platform" });
    importBundle(repo, all, "recipient");
    assert.equal(repo.upsert(COURSE, "recipient").status, "COMPLETED");
    assert.equal(repo.queue("recipient", "learn").length, 1);
  } finally {
    repo.close();
    await temp.close();
  }
});

test("imports preserve platform identity, graph integrity and the recipient's own progress", async () => {
  const temp = await temporary(),
    repo = new Repository(temp.dir);
  try {
    assert.throws(() =>
      importBundle(
        repo,
        bundle([COURSE, "https://outside.test/course"]),
        "receiver",
      ),
    );
    assert.equal(repo.db.prepare("SELECT count(*) n FROM tasks").get()?.n, 0);
    const input = bundle([EXAM]) as any;
    Object.assign(input.tasks[0], {
      account: "attacker",
      status: "COMPLETED",
      manualSubmit: false,
      details: { completed: true },
      cookies: [{ value: "secret" }],
    });
    importBundle(repo, input, "receiver");
    const exam = repo.upsert(EXAM, "receiver");
    assert.equal(exam.status, "READY");
    assert.deepEqual(exam.details, {});
    assert.equal(repo.queue("attacker", "exam").length, 0);
    repo.intent(exam, "submit", { recordId: "local" });
    repo.review(exam, "uncertain", {}, true);
    importBundle(repo, input, "receiver");
    assert.equal(repo.upsert(EXAM, "receiver").manualSubmit, true);
    assert.equal(repo.pending(exam, "submit").length, 1);
    assert.equal(repo.queue("receiver", "exam").length, 0);
    assert.equal(repo.queue("receiver", "review").length, 1);
    const topic =
      "https://cms.mylearning.cn/topic/catalog.html?token=secret&category=5";
    assert.equal(
      normalizeBundle(bundle([topic])).tasks[0].url,
      topic.replace("token=secret&", ""),
    );
  } finally {
    repo.close();
    await temp.close();
  }
});

test("merged exports prune cyclic dependencies without losing tasks or existing sources", async () => {
  const temp = await temporary(),
    repo = new Repository(temp.dir);
  const a = "https://cms.mylearning.cn/topic/a.html",
    b = "https://cms.mylearning.cn/topic/b.html";
  try {
    // Each discovery is acyclic; merging the same URLs across accounts is not.
    for (const [account, root] of [
      ["sender-a", a],
      ["sender-b", b],
    ]) {
      const result = await traverse(
        [root],
        async (task) => ({
          links: [task.url === a ? b : a, COURSE],
          complete: true,
        }),
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
      assert.equal(result.issues.length, 0);
    }
    const exported = exportBundle(repo, null, "all");
    assert.deepEqual(
      exported.tasks.map((task) => task.url),
      [COURSE],
    );
    assert.equal(exported.sources.length, 4);
    assert.ok(
      exported.sources.some(
        (source) => source.parent === a && source.child === b,
      ),
    );
    assert.ok(
      exported.sources.some(
        (source) => source.parent === b && source.child === a,
      ),
    );
    const required = exported.sources.find(
      (source) => source.child === COURSE,
    )!;
    const normalized = normalizeBundle({
      ...exported,
      sources: [
        { ...required, required: false },
        ...exported.sources,
        { ...required, required: false },
      ],
    });
    assert.equal(normalized.sources.length, 4);
    assert.equal(
      normalized.sources.find(
        (source) =>
          source.parent === required.parent && source.child === required.child,
      )?.required,
      true,
    );
    for (const recipient of ["fresh", "existing"]) {
      if (recipient === "existing")
        repo.link(repo.upsert(b, recipient), repo.upsert(a, recipient));
      assert.deepEqual(
        importBundle(repo, JSON.parse(JSON.stringify(normalized)), recipient),
        { added: 1, retained: 0 },
      );
      assert.equal(repo.queue(recipient, "learn").length, 1);
      assert.equal(repo.queue(recipient, "discover").length, 0);
      const edges = repo.db
        .prepare(
          "SELECT p.url parent, c.url child FROM edges e JOIN tasks p ON p.id=e.parent JOIN tasks c ON c.id=e.child WHERE p.account=?",
        )
        .all(recipient);
      assert.equal(edges.length, 3);
      assert.equal(edges.filter((edge) => edge.child === COURSE).length, 2);
      assert.equal(edges.filter((edge) => edge.child !== COURSE).length, 1);
      if (recipient === "existing")
        assert.ok(edges.some((edge) => edge.parent === b && edge.child === a));
      assert.deepEqual(importBundle(repo, exported, recipient), {
        added: 0,
        retained: 1,
      });
    }
  } finally {
    repo.close();
    await temp.close();
  }
});

test("offline import inbox transfers once to the verified learner with source relationships intact", async () => {
  const temp = await temporary(),
    repo = new Repository(temp.dir);
  try {
    const input = {
      ...bundle([COURSE, EXAM]),
      sources: [{ parent: SUBJECT, child: COURSE, required: true }],
    };
    importBundle(repo, input, IMPORT_INBOX);
    assert.equal(repo.queue("learner", "learn").length, 0);
    assert.equal(adoptImports(repo, "learner"), 2);
    assert.equal(adoptImports(repo, "another"), 0);
    assert.equal(repo.queue(IMPORT_INBOX, "learn").length, 0);
    assert.equal(repo.queue(IMPORT_INBOX, "exam").length, 0);
    assert.equal(repo.queue("learner", "learn").length, 1);
    assert.equal(repo.queue("learner", "exam").length, 1);
    assert.equal(
      repo.db
        .prepare("SELECT required FROM edges WHERE parent=? AND child=?")
        .get(
          repo.upsert(SUBJECT, "learner").id,
          repo.upsert(COURSE, "learner").id,
        )?.required,
      1,
    );
  } finally {
    repo.close();
    await temp.close();
  }
});

test("inbox adoption keeps the recipient's graph when imported sources create a cycle", async () => {
  const temp = await temporary(),
    repo = new Repository(temp.dir);
  try {
    const a = "https://cms.mylearning.cn/topic/a.html",
      b = "https://cms.mylearning.cn/topic/b.html";
    repo.link(repo.upsert(b, "learner"), repo.upsert(a, "learner"));
    importBundle(
      repo,
      {
        ...bundle(),
        sources: [
          { parent: a, child: b, required: true },
          { parent: b, child: COURSE, required: true },
        ],
      },
      IMPORT_INBOX,
    );
    assert.equal(adoptImports(repo, "learner"), 1);
    assert.equal(adoptImports(repo, "another"), 0);
    assert.equal(repo.queue(IMPORT_INBOX, "learn").length, 0);
    assert.equal(repo.queue("learner", "learn").length, 1);
    assert.equal(repo.queue("learner", "discover").length, 0);
    assert.deepEqual(exportBundle(repo, "learner", "all").sources, [
      { parent: b, child: COURSE, required: true },
    ]);
    assert.equal(
      repo.link(repo.upsert(a, "learner"), repo.upsert(b, "learner")),
      false,
    );
  } finally {
    repo.close();
    await temp.close();
  }
});

test("import, export, diagnostics and requeue are local operations without an authentication lease", async (t) => {
  const f = await application();
  try {
    const lease = t.mock.method(f.app.auth, "lease", async () => {
      throw new Error("must not authenticate");
    });
    const prompt = t.mock.method(f.app, "prompt", async () => {
      throw new Error("Must not ask another question");
    });
    f.app.command(token(), "operation", {
      kind: "import_queues",
      bundle: bundle(),
    });
    await f.app.settled();
    assert.equal(f.repo.queue(IMPORT_INBOX, "learn").length, 1);
    const task = f.repo.upsert(COURSE, IMPORT_INBOX);
    const diagnostic = {
      reason: "partial_course_failure",
      message: "部分章节尚未完成",
      recovery: "blocked" as const,
      sections: [
        {
          section: "h5",
          message: "H5 章节需要人工学习",
          recovery: "blocked" as const,
        },
        {
          section: "video",
          message: "视频加载失败 token=[已隐藏]",
          recovery: "retry" as const,
        },
      ],
    };
    f.repo.review(task, diagnostic.reason, {
      message: diagnostic.message,
      recovery: "blocked",
      sections: [
        diagnostic.sections[0],
        {
          ...diagnostic.sections[1],
          message: "视频加载失败 token=private-token",
          answers: ["private-answer"],
        },
      ],
      cookie: "private-cookie",
    });
    assert.equal(f.app.dashboard().learning_count, 0);
    assert.equal(f.app.dashboard().discovery_count, 0);
    assert.equal(f.app.dashboard().blocked_failure_count, 1);
    assert.equal(f.app.dashboard().retriable_failure_count, 0);
    assert.deepEqual(f.app.localQueues().accounts[0].failures[0], {
      url: COURSE,
      ...diagnostic,
    });
    assert.equal(f.app.operationId, null);
    f.app.command(token(), "operation", {
      kind: "retry_tasks",
      account: IMPORT_INBOX,
      recovery: "retry",
    });
    await f.app.settled();
    assert.equal(f.repo.hasFailure(task.id), true);
    f.app.command(token(), "operation", {
      kind: "export_queues",
      account: IMPORT_INBOX,
      selection: "failed",
    });
    await f.app.settled();
    const file = [...f.app.exports.values()][0];
    const data = normalizeBundle(JSON.parse(await readFile(file.path, "utf8")));
    assert.deepEqual(data.tasks[0].issue, diagnostic);
    assert.doesNotMatch(
      JSON.stringify(data),
      /private-token|private-answer|private-cookie|answers|cookie/,
    );
    importBundle(f.repo, data, "recipient");
    assert.deepEqual(
      exportBundle(f.repo, "recipient", "pending").tasks[0].issue,
      diagnostic,
    );
    assert.match(f.app.view.summary.download.href, /^\/api\/exports\//);
    f.app.command(token(), "operation", {
      kind: "retry_tasks",
      account: IMPORT_INBOX,
      recovery: "blocked",
    });
    await f.app.settled();
    assert.equal(f.repo.hasFailure(task.id), false);
    assert.equal(f.repo.queue(IMPORT_INBOX, "learn").length, 1);
    assert.equal(f.repo.task(task.id).details.sections, undefined);
    f.repo.review(task, "access_denied", {
      message: "当前账号无权限",
      recovery: "blocked",
    });
    assert.equal(
      exportBundle(f.repo, IMPORT_INBOX, "failed").tasks[0].issue?.sections,
      undefined,
    );
    assert.equal(lease.mock.callCount(), 0);
    assert.equal(prompt.mock.callCount(), 0);
    f.chrome.assertDone();
  } finally {
    await f.close();
  }
});
