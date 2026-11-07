import assert from "node:assert/strict";
import type { Tab } from "nodriver-ts";
import type { Application } from "../../src/application/runtime.ts";
import { COURSE } from "../helpers.ts";

export async function queueDialogChecks(page: Tab, app: Application) {
  const execute = app.execute;
  const submissions: { kind: string; payload: any }[] = [];
  app.execute = async (kind, payload) => {
    submissions.push({ kind, payload });
  };
  app.repo.enqueue(app.repo.upsert(COURSE, "dialog-first"), "learn");
  const click = async (selector: string) =>
    (await page.query(selector))!.click();
  const waitGates = (count: number) =>
    page.waitForFunction(
      (expected) => (window as any).queueChecks.gates.length === expected,
      count,
      { timeout: 5000 },
    );
  const release = (index: number) =>
    page.evaluate(async (gate) => {
      (window as any).queueChecks.gates[gate]();
      await new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      );
    }, index);
  const waitDialog = (title: string) =>
    page.waitForFunction(
      (expected) =>
        (document.getElementById("queue-dialog") as HTMLDialogElement).open &&
        document.getElementById("queue-title")!.textContent === expected,
      title,
      { timeout: 5000 },
    );
  const submit = async () => {
    await click("#queue-submit");
    await page.waitForFunction(
      () =>
        !(document.getElementById("queue-dialog") as HTMLDialogElement).open,
      undefined,
      { timeout: 5000 },
    );
    await app.settled();
  };
  await page.evaluate(() => {
    const original = window.fetch;
    const gates: (() => void)[] = [];
    (window as any).queueChecks = { original, gates };
    window.fetch = async (...args) => {
      const response = await original(...args);
      if (args[0] !== "/api/queues") return response;
      // Buffer the HTTP response before delaying client delivery so requests
      // for the same URL can finish independently and in either order.
      const body = await response.text();
      return new Promise<Response>((resolve) =>
        gates.push(() =>
          resolve(
            new Response(body, {
              status: response.status,
              headers: response.headers,
            }),
          ),
        ),
      );
    };
  });
  try {
    await click('[data-kind="clear_queues"]');
    await click("#account-card");
    await click('[data-kind="clear_credentials"]');
    await waitGates(2);
    await release(0);
    assert.equal(
      await page.evaluate(
        () =>
          (document.getElementById("queue-dialog") as HTMLDialogElement).open,
      ),
      false,
    );
    await release(1);
    await waitDialog("清除登录信息");
    assert.equal(
      await page.evaluate(
        () => document.getElementById("queue-submit")!.textContent,
      ),
      "清除登录信息",
    );
    await submit();
    assert.equal(submissions[0].kind, "clear_credentials");

    await click('[data-kind="export_queues"]');
    await click('[data-kind="clear_queues"]');
    await waitGates(4);
    await release(3);
    await waitDialog("清空队列");
    await click('#queue-options input[value="courses"]');
    await release(2);
    assert.equal(
      await page.evaluate(
        () => document.getElementById("queue-title")!.textContent,
      ),
      "清空队列",
    );
    assert.equal(
      await page.evaluate(
        () => document.getElementById("queue-submit")!.textContent,
      ),
      "清空全部课程",
    );
    await submit();
    assert.equal(submissions[1].kind, "clear_queues");
    assert.equal(submissions[1].payload.scope, "courses");
    assert.equal(submissions.length, 2);

    await click("#tasks-open");
    await waitGates(5);
    await release(4);
    await waitDialog("异常任务");
    await click("#queue-refresh");
    await waitGates(6);
    await page.keyboard.press("Escape");
    app.repo.enqueue(app.repo.upsert(COURSE, "dialog-second"), "learn");
    await click('[data-kind="export_queues"]');
    await waitGates(7);
    await release(6);
    await waitDialog("导出任务");
    await (await page.query("#queue-account"))!.select("dialog-second");
    await release(5);
    assert.equal(
      await page.evaluate(
        () =>
          (document.getElementById("queue-account") as HTMLSelectElement).value,
      ),
      "dialog-second",
    );
    assert.equal(
      await page.evaluate(
        () => document.getElementById("queue-title")!.textContent,
      ),
      "导出任务",
    );
    assert.equal(
      await page.evaluate(
        () => document.getElementById("queue-count")!.textContent,
      ),
      "已选择 1 个任务",
    );
    await click("#queue-close");
  } finally {
    await page.evaluate(() => {
      const checks = (window as any).queueChecks;
      window.fetch = checks.original;
      checks.gates.forEach((gate: () => void) => gate());
      delete (window as any).queueChecks;
    });
    app.execute = execute;
    await app.repo.clear("dialog-first", "courses");
    await app.repo.clear("dialog-second", "courses");
  }
}
