import test from "node:test";
import assert from "node:assert/strict";
import { join } from "node:path";
import { mkdir } from "node:fs/promises";
import { BrowserRuntime } from "../../src/browser/runtime.ts";
import { Repository } from "../../src/storage/repository.ts";
import { Logger } from "../../src/log.ts";
import { configuration } from "../../src/config.ts";
import { Application } from "../../src/application/runtime.ts";
import { WebServer } from "../../src/web/server.ts";
import { temporary, COURSE } from "../helpers.ts";
import { atomicJson } from "../../src/storage/files.ts";
import { IMPORT_INBOX } from "../../src/storage/transfer.ts";
import { until } from "../../src/utils.ts";
import { platformChecks } from "./platform-checks.ts";
import { loginChecks } from "./login-checks.ts";
import { modelChecks } from "./model-checks.ts";
import { queueDialogChecks } from "./queue-dialog-checks.ts";
import { acquireInstance } from "../../src/application/instance.ts";

test(
  "one shared tab: platform, login, workbench, offline queues and session isolation",
  { skip: process.env.COURSE_AFK_BROWSER_TEST !== "1", timeout: 120000 },
  async (t) => {
    const temp = await temporary(),
      config = {
        ...configuration(undefined, join(temp.dir, "data")),
        dataDir: join(temp.dir, "data"),
        logsDir: join(temp.dir, "logs"),
      };
    const logger = new Logger(config.logsDir),
      repo = new Repository(config.dataDir);
    let runtime: BrowserRuntime | undefined,
      server: WebServer | undefined,
      app: Application | undefined;
    let releaseProfile: (() => Promise<void>) | undefined;
    try {
      await mkdir("build/browser-check", { recursive: true });
      const testDirectory = join(config.root, "build", "browser-tests");
      const lock = await acquireInstance(testDirectory);
      if (!lock.owned) throw new Error("已有浏览器测试正在使用测试配置");
      releaseProfile = lock.close;
      t.diagnostic("Launching the isolated browser");
      runtime = await BrowserRuntime.launch(
        logger,
        config.browserPath,
        join(testDirectory, "browser-profile"),
      );
      const startup = runtime.context.tabs.find(
        (tab) => tab.url === "about:blank",
      );
      assert.ok(startup);
      const created: string[] = [];
      runtime.browser.tabCreated.on((tab) => {
        created.push(tab.id);
      });
      t.diagnostic("Checking platform fixtures");
      await platformChecks(runtime);
      t.diagnostic("Checking login fixtures");
      await loginChecks(runtime);
      assert.deepEqual(created, []);
      assert.notEqual(startup.url, "about:blank");
      app = new Application(repo, runtime, config, logger);
      server = new WebServer(app);
      const execute = app.execute.bind(app);
      let receivedKind = "";
      app.execute = async (kind, payload) => {
        receivedKind = kind;
        if (
          [
            "settings",
            "import_queues",
            "export_queues",
            "clear_queues",
            "retry_tasks",
          ].includes(kind)
        )
          return execute(kind, payload);
        if (kind === "learn") {
          await new Promise<void>((resolve) =>
            app!.abort!.signal.addEventListener("abort", () => resolve(), {
              once: true,
            }),
          );
          return;
        }
        await app!.prompt("confirm", {
          title: "测试操作",
          message: "测试中的确认提示",
          confirmLabel: "完成选择",
          cancelLabel: "取消",
        });
      };
      const url = await server.start(0),
        page = await runtime.openManagement(url);
      const waitLocalDialog = () =>
        page.waitForFunction(
          () =>
            (document.getElementById("queue-dialog") as HTMLDialogElement).open,
          undefined,
          { timeout: 5000 },
        );
      t.diagnostic("Checking workbench and narrow layouts");
      assert.equal(page.id, startup.id);
      assert.deepEqual(
        runtime.context.tabs.map((tab) => tab.id),
        [page.id],
      );
      await page.waitForFunction(
        () =>
          document
            .getElementById("connection")
            ?.textContent?.includes("控制窗口"),
        undefined,
        { timeout: 20000 },
      );
      for (const [width, height] of [
        [390, 844],
        [375, 667],
        [320, 640],
      ]) {
        await page.setEnvironment({ viewport: { width, height } });
        assert.equal(
          await page.evaluate(
            () =>
              document.documentElement.scrollWidth >
              document.documentElement.clientWidth,
          ),
          false,
        );
      }
      const clearQueues = await page.query('[data-kind="clear_queues"]');
      assert.ok(clearQueues);
      await clearQueues.click();
      await waitLocalDialog();
      assert.equal(receivedKind, "");
      assert.equal(app.operationId, null);
      assert.equal(
        await page.evaluate(
          () => document.getElementById("task-controls")!.hidden,
        ),
        true,
      );
      await page.keyboard.press("Escape");
      assert.equal(
        await page.evaluate(
          () =>
            (document.getElementById("queue-dialog") as HTMLDialogElement).open,
        ),
        false,
      );
      assert.equal(
        await page.evaluate(
          () => (document.activeElement as HTMLElement).dataset.kind,
        ),
        "clear_queues",
      );
      await (await page.query('[data-kind="links"]'))!.click();
      assert.equal(receivedKind, "");
      assert.equal(app.operationId, null);
      assert.equal(
        await page.evaluate(() => document.activeElement?.id),
        "workbench-input",
      );
      assert.equal(
        await page.evaluate(() =>
          document
            .getElementById("workbench-input")!
            .getAttribute("aria-invalid"),
        ),
        "true",
      );
      t.diagnostic("Checking the account menu");
      await (await page.query("#account-card"))!.click();
      await page.waitForFunction(
        () => document.getElementById("account-menu")!.matches(":popover-open"),
        undefined,
        { timeout: 5000 },
      );
      await page.screenshot({ path: "build/browser-check/account-menu.png" });
      await (await page.query('[data-kind="clear_credentials"]'))!.click();
      await waitLocalDialog();
      assert.equal(
        await page.evaluate(
          () => document.getElementById("queue-title")!.textContent,
        ),
        "清除登录信息",
      );
      assert.equal(
        await page.evaluate(() =>
          document.getElementById("account-menu")!.matches(":popover-open"),
        ),
        false,
      );
      await (await page.query("#queue-close"))!.click();
      assert.equal(
        await page.evaluate(() => document.activeElement?.id),
        "account-card",
      );
      assert.equal(app.operationId, null);
      await page.cdp.send("Emulation.setEmulatedMedia", {
        features: [{ name: "forced-colors", value: "active" }],
      });
      assert.notEqual(
        await page.evaluate(
          () =>
            getComputedStyle(document.getElementById("auto-submit")!)
              .appearance,
        ),
        "none",
      );
      await page.cdp.send("Emulation.setEmulatedMedia", { features: [] });
      t.diagnostic("Checking offline file import, export and clearing");
      const importPath = join(temp.dir, "shared-tasks.json");
      await atomicJson(importPath, {
        format: "course-pilot.tasks",
        version: 1,
        exportedAt: new Date().toISOString(),
        tasks: [
          {
            url: COURSE,
            state: "failed",
            issue: {
              reason: "opaque",
              message: "原账号无权限",
              recovery: "blocked",
            },
          },
        ],
        sources: [],
      });
      await (await page.query("#queue-import"))!.upload([importPath]);
      await page.waitForFunction(
        () =>
          document.querySelector("#summary h2")?.textContent === "任务已导入",
        undefined,
        { timeout: 10000 },
      );
      assert.equal(repo.queue(IMPORT_INBOX, "learn").length, 1);
      assert.equal(app.auth.read(), undefined);
      await (await page.query('[data-kind="export_queues"]'))!.click();
      await waitLocalDialog();
      assert.equal(app.operationId, null);
      assert.deepEqual(
        await page.evaluate(() =>
          [...document.querySelectorAll("#queue-options strong")].map(
            (element) => element.textContent,
          ),
        ),
        ["未学及待完成任务", "失败任务", "全部待办与失败任务"],
      );
      await (await page.query("#queue-submit"))!.click();
      await page.waitForFunction(
        () => !!document.querySelector("#summary a[download]"),
        undefined,
        { timeout: 10000 },
      );
      const exported = await page.evaluate(async () => {
        const link = document.querySelector(
          "#summary a[download]",
        ) as HTMLAnchorElement;
        return await (await fetch(link.href)).json();
      });
      assert.equal(exported.tasks[0].url, COURSE);
      assert.equal(exported.tasks[0].issue.message, "原账号无权限");
      assert.equal(
        await page.evaluate(
          () =>
            document.documentElement.scrollWidth >
            document.documentElement.clientWidth,
        ),
        false,
      );
      await page.screenshot({ path: "build/browser-check/transfer.png" });
      repo.review(
        repo.upsert(COURSE.replaceAll("11111111", "aaaaaaaa"), IMPORT_INBOX),
        "unavailable",
        {
          message: "当前账号无权限",
          recovery: "blocked",
        },
      );
      repo.enqueue(repo.upsert(COURSE, "another-account"), "learn");
      for (const choice of ["cancel", "failures", "courses"]) {
        t.diagnostic(`Opening clear form: ${choice}`);
        await (await page.query('[data-kind="clear_queues"]'))!.click();
        await waitLocalDialog();
        assert.equal(app.operationId, null);
        await (await page.query("#queue-account"))!.select(IMPORT_INBOX);
        assert.deepEqual(
          await page.evaluate(() =>
            [...document.querySelectorAll("#queue-options strong")].map(
              (element) => element.textContent,
            ),
          ),
          ["仅清空异常任务", "清空全部课程", "清空考试队列", "清空全部待办"],
        );
        assert.equal(
          await page.evaluate(
            () => document.getElementById("task-controls")!.hidden,
          ),
          true,
        );
        assert.equal(
          await page.evaluate(
            () =>
              document.getElementById("queue-dialog")!.scrollWidth >
              document.getElementById("queue-dialog")!.clientWidth,
          ),
          false,
        );
        if (choice === "cancel")
          await page.screenshot({
            path: "build/browser-check/clear-courses.png",
          });
        if (choice === "cancel") {
          t.diagnostic("Cancelling the clear form");
          await (await page.query("#queue-close"))!.click();
        } else {
          t.diagnostic(`Selecting clear scope: ${choice}`);
          await (await page.query(
            `#queue-options input[value="${choice}"]`,
          ))!.click();
          t.diagnostic(`Submitting clear form: ${choice}`);
          await (await page.query("#queue-submit"))!.click();
        }
        await until(async () => !app!.operationId, 5000);
        await page.waitForFunction(
          () =>
            !(
              document.querySelector(
                '[data-kind="clear_queues"]',
              ) as HTMLButtonElement
            ).disabled,
          undefined,
          { timeout: 5000 },
        );
        assert.equal(
          repo.queue(IMPORT_INBOX, "failure").length,
          choice === "cancel" ? 1 : 0,
        );
        assert.equal(
          repo.queue(IMPORT_INBOX, "learn").length,
          choice === "courses" ? 0 : 1,
        );
        assert.equal(repo.queue("another-account", "learn").length, 1);
      }
      assert.equal(repo.queue(IMPORT_INBOX, "learn").length, 0);
      assert.match(
        await page.evaluate(
          () => document.getElementById("notification")!.textContent,
        ),
        /已清空/,
      );
      t.diagnostic(
        "Checking that viewing failures and editing drafts do not interrupt running work",
      );
      const blockedTask = repo.upsert(
        COURSE.replaceAll("11111111", "bbbbbbbb"),
        "another-account",
      );
      repo.review(blockedTask, "partial_course_failure", {
        recovery: "blocked",
        message: "部分章节尚未完成",
        sections: [
          {
            section: "h5",
            message: "H5 章节需要人工学习",
            recovery: "blocked",
          },
          { section: "video", message: "需管理员授权", recovery: "blocked" },
        ],
      });
      await page.waitForFunction(
        () =>
          document.getElementById("learning_failure_count")!.textContent ===
            "1" &&
          document.getElementById("learning_count")!.textContent === "1",
        undefined,
        { timeout: 5000 },
      );
      await (await page.query('[data-kind="learn"]'))!.click();
      await page.waitForFunction(
        () => !document.getElementById("task-controls")!.hidden,
        undefined,
        { timeout: 5000 },
      );
      const learningOperation = app.operationId;
      await (await page.query("#workbench-input"))!.fill("下一批课程草稿");
      await (await page.query("#tasks-open"))!.click();
      await waitLocalDialog();
      assert.equal(app.operationId, learningOperation);
      assert.equal(
        await page.evaluate(
          () =>
            (
              document.querySelector(
                '[data-recovery="blocked"]',
              ) as HTMLButtonElement
            ).disabled,
        ),
        true,
      );
      assert.match(
        await page.evaluate(
          () => document.getElementById("queue-issue-list")!.textContent,
        ),
        /需管理员授权/,
      );
      assert.deepEqual(
        await page.evaluate(() =>
          [...document.querySelectorAll(".issue-sections strong")].map(
            (element) => element.textContent,
          ),
        ),
        ["章节 h5 · 需外部处理", "章节 video · 需外部处理"],
      );
      assert.match(
        await page.evaluate(
          () => document.getElementById("queue-issue-list")!.textContent,
        ),
        /H5 章节需要人工学习/,
      );
      assert.equal(
        await page.evaluate(
          () =>
            document.getElementById("queue-dialog")!.scrollWidth >
            document.getElementById("queue-dialog")!.clientWidth,
        ),
        false,
      );
      await page.evaluate(async () => {
        await Promise.all(
          document
            .getElementById("queue-dialog")!
            .getAnimations({ subtree: true })
            .map((animation) => animation.finished),
        );
      });
      await page.screenshot({ path: "build/browser-check/failures.png" });
      await page.keyboard.press("Escape");
      assert.equal(app.operationId, learningOperation);
      await (await page.query("#cancel"))!.click();
      await until(async () => !app!.operationId, 5000);
      await (await page.query("#tasks-open"))!.click();
      await waitLocalDialog();
      await page.waitForFunction(
        () =>
          !(
            document.querySelector(
              '[data-recovery="blocked"]',
            ) as HTMLButtonElement
          ).disabled,
        undefined,
        { timeout: 5000 },
      );
      await (await page.query('[data-recovery="blocked"]'))!.click();
      await until(
        async () => !repo.hasFailure(blockedTask.id) && !app!.operationId,
        5000,
      );
      await page.waitForFunction(
        () =>
          (document.getElementById("notification")!.textContent || "").includes(
            "重新加入队列",
          ),
        undefined,
        { timeout: 5000 },
      );
      await repo.clear("another-account", "courses");
      t.diagnostic("Checking delayed and out-of-order local dialogs");
      await queueDialogChecks(page, app);
      t.diagnostic("Checking settings and session isolation");
      await modelChecks(page, config.settingsPath);
      await page.setEnvironment({ viewport: { width: 1280, height: 900 } });
      const input = await page.query("#workbench-input");
      assert.ok(input);
      await input.fill(
        "https://kc.zhixueyun.com/#/study/course/detail/11111111-1111-4111-8111-111111111111",
      );
      assert.match(
        await page.evaluate(
          () =>
            (document.getElementById("workbench-input") as HTMLTextAreaElement)
              .value,
        ),
        /11111111/,
      );
      const next = await page.query("#recommended");
      assert.ok(next);
      await next.click();
      await page.waitForFunction(
        () => (document.getElementById("prompt") as HTMLDialogElement).open,
        undefined,
        { timeout: 10000 },
      );
      assert.ok(app.operationId);
      const cancel = await page.query(
        "#prompt .prompt-actions button:first-child",
      );
      assert.ok(cancel);
      await cancel.click();
      await until(async () => !app!.operationId, 5000);
      await page.waitForFunction(
        () =>
          !(document.getElementById("recommended") as HTMLButtonElement)
            .disabled,
        undefined,
        { timeout: 5000 },
      );
      await (await page.query("#log-details summary"))!.click();
      for (let n = 0; n < 30; n++) logger.log(`顺序检查 ${n}`);
      await page.waitForFunction(
        () =>
          document.querySelector("#logs > div")?.textContent === "顺序检查 29",
        undefined,
        { timeout: 5000 },
      );
      assert.equal(
        await page.evaluate(() => document.getElementById("logs")!.scrollTop),
        0,
      );
      const anchor = await page.evaluate(() => {
        const logs = document.getElementById("logs")!;
        logs.scrollTop = 180;
        const top = logs.getBoundingClientRect().top;
        const row = [...logs.children].find(
          (el) => el.getBoundingClientRect().bottom > top,
        ) as HTMLElement;
        return {
          seq: row.dataset.seq,
          offset: row.getBoundingClientRect().top - top,
        };
      });
      logger.log("自动刷新检查");
      await page.waitForFunction(
        () =>
          document.querySelector("#logs > div")?.textContent === "自动刷新检查",
        undefined,
        { timeout: 5000 },
      );
      assert.ok(
        await page.evaluate((anchor) => {
          const logs = document.getElementById("logs")!;
          const row = [...logs.children].find(
            (el) => (el as HTMLElement).dataset.seq === anchor.seq,
          )!;
          return (
            Math.abs(
              row.getBoundingClientRect().top -
                logs.getBoundingClientRect().top -
                anchor.offset,
            ) < 2
          );
        }, anchor),
      );
      await page.evaluate(() => {
        document.getElementById("logs")!.scrollTop = 0;
      });
      const adminCookie = (await runtime.context.cookies()).find(
        (c) => c.name === "course_web",
      );
      assert.ok(adminCookie);
      await runtime.context.setCookies([
        {
          name: "synthetic",
          value: "test",
          domain: ".mylearning.cn",
          path: "/",
          secure: true,
        },
      ]);
      await runtime.clearPlatform();
      const cookies = await runtime.context.cookies();
      assert.equal(
        cookies.some((c) => c.name === "synthetic"),
        false,
      );
      assert.equal(
        cookies.find((c) => c.name === "course_web")?.value,
        adminCookie.value,
      );
      assert.equal(page.closed, false);
      await page.reload({ waitUntil: "domcontentloaded", timeout: 15000 });
      await page.waitForFunction(
        () =>
          document
            .getElementById("connection")
            ?.textContent?.includes("控制窗口"),
        undefined,
        { timeout: 15000 },
      );
      await page.screenshot({ path: "build/browser-check/workbench.png" });
      assert.deepEqual(created, []);
      assert.deepEqual(
        runtime.context.tabs.map((tab) => tab.id),
        [startup.id],
      );
      assert.equal(
        await page.evaluate(() =>
          document.querySelector('link[rel="icon"]')?.getAttribute("href"),
        ),
        "/favicon.svg",
      );
      await page.goto(`${server.origin}/favicon.svg`);
      assert.equal(
        await page.evaluate(() =>
          document.querySelector("svg")!.getAttribute("viewBox"),
        ),
        "0 0 140 140",
      );
      assert.equal(
        await page.evaluate(() => document.querySelectorAll("path").length),
        2,
      );
      await page.screenshot({ path: "build/browser-check/favicon.png" });
    } catch (error) {
      if (runtime?.management && !runtime.management.closed) {
        await runtime.management.screenshot({
          path: "build/browser-check/interaction-failure.png",
        });
        t.diagnostic(
          JSON.stringify(
            await runtime.management.evaluate(() => ({
              dialog: (
                document.getElementById("queue-dialog") as HTMLDialogElement
              )?.open,
              focused: document.activeElement?.outerHTML.slice(0, 240),
              notification:
                document.getElementById("notification")?.textContent,
            })),
          ),
        );
      }
      throw error;
    } finally {
      app?.exit();
      await app?.settled();
      await server?.close();
      try {
        await runtime?.close();
      } finally {
        repo.close();
        await temp.close();
        await releaseProfile?.();
      }
    }
  },
);
