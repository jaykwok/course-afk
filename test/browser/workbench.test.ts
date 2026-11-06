import test from "node:test";
import assert from "node:assert/strict";
import { join } from "node:path";
import { mkdir } from "node:fs/promises";
import { BrowserRuntime } from "../../src/browser/runtime.ts";
import { Repository } from "../../src/storage/repository.ts";
import { Logger } from "../../src/log.ts";
import { configuration, readSettings } from "../../src/config.ts";
import { Application } from "../../src/application/runtime.ts";
import { WebServer } from "../../src/web/server.ts";
import { temporary } from "../helpers.ts";
import {
  extractQuestions,
  applyAnswers,
  selectedAnswers,
} from "../../src/platform/exam.ts";
import { until } from "../../src/utils.ts";
import type { DiscoveryContext } from "../../src/platform/discovery.ts";
import { platformChecks } from "./platform-checks.ts";
import { acquireInstance } from "../../src/application/instance.ts";

test(
  "one shared Browser: workbench, narrow layouts, native contrast switch, input, session isolation",
  { skip: process.env.COURSE_AFK_BROWSER_TEST !== "1", timeout: 120000 },
  async () => {
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
      const testDirectory = join(config.root, "build", "browser-tests");
      const lock = await acquireInstance(testDirectory);
      if (!lock.owned) throw new Error("已有浏览器测试正在使用测试配置");
      releaseProfile = lock.close;
      runtime = await BrowserRuntime.launch(
        logger,
        config.browserPath,
        join(testDirectory, "browser-profile"),
      );
      await platformChecks(runtime);
      app = new Application(repo, runtime, config, logger);
      server = new WebServer(app);
      const execute = app.execute.bind(app);
      app.execute = async (kind, payload) => {
        if (kind === "settings") return execute(kind, payload);
        await app!.prompt("confirm", {
          title: "测试操作",
          message: "测试中的确认提示",
          default: false,
        });
      };
      const url = await server.start(0),
        page = await runtime.openManagement(url);
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
      await (await page.query("#settings-open"))!.click();
      await page.waitForFunction(
        () =>
          (document.getElementById("settings-dialog") as HTMLDialogElement)
            .open,
        undefined,
        { timeout: 5000 },
      );
      assert.equal(
        await page.evaluate(
          () =>
            document.getElementById("settings-dialog")!.scrollWidth >
            document.getElementById("settings-dialog")!.clientWidth,
        ),
        false,
      );
      await (await page.query('[data-setting="openai.model"]'))!.fill(
        "browser-fixture-model",
      );
      await (await page.query('[data-setting="openai.apiKey"]'))!.fill(
        "browser-fixture-secret",
      );
      await (await page.query("#settings-save"))!.click();
      await page.waitForFunction(
        () =>
          !(document.getElementById("settings-dialog") as HTMLDialogElement)
            .open,
        undefined,
        { timeout: 10000 },
      );
      await until(
        async () =>
          readSettings(config.settingsPath).openai.model ===
            "browser-fixture-model" && !app!.operationId,
        5000,
      );
      await (await page.query("#settings-open"))!.click();
      await page.waitForFunction(
        () =>
          (document.getElementById("settings-dialog") as HTMLDialogElement)
            .open,
        undefined,
        { timeout: 5000 },
      );
      assert.equal(
        await page.evaluate(
          () =>
            (
              document.querySelector(
                '[data-setting="openai.apiKey"]',
              ) as HTMLInputElement
            ).value,
        ),
        "",
      );
      await (await page.query("#settings-close"))!.click();
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
        () => !document.getElementById("prompt")!.hidden,
        undefined,
        { timeout: 10000 },
      );
      assert.ok(app.operationId);
      const cancel = await page.query("#cancel");
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
      const abort = new AbortController(),
        scope = runtime.scope(abort.signal),
        exam = await scope.open();
      await exam.setContent(
        `<html><body><div class="question-type-item" data-dynamic-key="q1"><span class="o-score">多选题</span><div class="stem-content-main">选择一和三</div><dl class="preview-list"><dd><label><input type="checkbox"><span class="option-num">A.</span><span class="answer-options">一</span></label></dd><dd><label><input type="checkbox"><span class="option-num">B.</span><span class="answer-options">二</span></label></dd><dd><label><input type="checkbox"><span class="option-num">C.</span><span class="answer-options">三</span></label></dd></dl></div></body></html>`,
      );
      const parsed = await extractQuestions(exam, abort.signal);
      assert.equal(parsed.questions.length, 1);
      assert.equal(parsed.questions[0].type, "multiple");
      app.auth.assert = () => abort.signal.throwIfAborted();
      const ctx: DiscoveryContext = {
        repo,
        scope,
        report: () => {},
        confirm: async () => false,
        lease: {
          account: "test",
          generation: 0,
          fingerprint: "test",
          signal: abort.signal,
        },
        auth: app.auth,
      };
      assert.equal(
        await applyAnswers(exam, parsed.questions[0], ["A", "C"], ctx),
        true,
      );
      assert.deepEqual(
        await selectedAnswers(exam, parsed.questions[0], abort.signal),
        ["A", "C"],
      );
      assert.equal(exam.inputBehavior.policy.mode, "human");
      assert.ok(exam.inputBehavior.lastReport?.events);
      await scope.close();
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
      await mkdir("build/browser-check", { recursive: true });
      await page.screenshot({ path: "build/browser-check/workbench.png" });
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
