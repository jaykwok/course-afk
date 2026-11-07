import assert from "node:assert/strict";
import type { Tab } from "nodriver-ts";
import type { Application } from "../../src/application/runtime.ts";
import type { WebServer } from "../../src/web/server.ts";
import type { LearningProgress } from "../../src/platform/playback.ts";
import { playbackSnapshot } from "../../src/platform/playback.ts";
import { COURSE, EXAM, SUBJECT } from "../helpers.ts";

/** A local, silent WAV gives Chromium real media time, pause and seek behavior. */
export async function playbackChecks(page: Tab) {
  await page.setContent(
    '<dl id="sample-section" class="chapter-list-box focus"></dl><video style="display:none"></video><video id="sample-video" width="320" height="180" muted></video>',
  );
  await page.evaluate(async () => {
    const samples = 8000 * 15,
      buffer = new ArrayBuffer(44 + samples * 2),
      view = new DataView(buffer);
    const text = (offset: number, value: string) =>
      [...value].forEach((letter, index) =>
        view.setUint8(offset + index, letter.charCodeAt(0)),
      );
    text(0, "RIFF");
    view.setUint32(4, 36 + samples * 2, true);
    text(8, "WAVEfmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, 8000, true);
    view.setUint32(28, 16000, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    text(36, "data");
    view.setUint32(40, samples * 2, true);
    const video = document.getElementById("sample-video") as HTMLVideoElement;
    video.src = URL.createObjectURL(new Blob([buffer], { type: "audio/wav" }));
    await video.play();
  });
  await page.waitForFunction(
    () =>
      (document.getElementById("sample-video") as HTMLVideoElement)
        .currentTime > 1,
    undefined,
    { timeout: 5000 },
  );
  const playing = await page.evaluate(playbackSnapshot, "#sample-section");
  assert.equal(playing.focused, true);
  assert.ok(playing.media!.time > 1);
  assert.equal(playing.media!.duration, 15);
  assert.equal(playing.media!.paused, false);
  await page.evaluate(() =>
    (document.getElementById("sample-video") as HTMLVideoElement).pause(),
  );
  const paused = await page.evaluate(playbackSnapshot, "#sample-section");
  assert.equal(paused.media!.paused, true);
  await page.evaluate(async () => {
    const video = document.getElementById("sample-video") as HTMLVideoElement;
    const seeked = new Promise<void>((resolve) =>
      video.addEventListener("seeked", () => resolve(), { once: true }),
    );
    video.currentTime = 10;
    await seeked;
  });
  const sought = await page.evaluate(playbackSnapshot, "#sample-section");
  assert.equal(sought.media!.time, 10);
  assert.equal(sought.media!.paused, true, "sampling must not restart media");
  await page.evaluate(() => {
    document.getElementById("sample-section")!.classList.remove("focus");
    URL.revokeObjectURL(
      (document.getElementById("sample-video") as HTMLVideoElement).src,
    );
  });
  assert.deepEqual(await page.evaluate(playbackSnapshot, "#sample-section"), {
    focused: false,
    media: null,
  });
}

export async function liveWorkbenchChecks(
  page: Tab,
  app: Application,
  server: WebServer,
) {
  const show = async (value: LearningProgress, label: string) => {
    app.update({ progress: value, status: `正在学习：${value.description}` });
    await page.waitForFunction(
      ({ completed, label }) =>
        (document.getElementById("progress") as HTMLProgressElement).value ===
          completed &&
        document.getElementById("progress-state")!.textContent === label,
      { completed: value.completed, label },
      { timeout: 5000 },
    );
  };
  const current: LearningProgress = {
    description: "实时进度测试",
    completed: 40,
    total: 120,
    state: "playing",
  };
  await show(current, "正在播放");
  await page.cdp.send("Emulation.setEmulatedMedia", {
    features: [{ name: "prefers-reduced-motion", value: "no-preference" }],
  });
  await page.waitForFunction(
    () => {
      const spinner = document.querySelector(".spinner")!;
      return (
        getComputedStyle(spinner).animationName === "spin" &&
        Number(spinner.getAnimations()[0]?.currentTime) > 1100
      );
    },
    undefined,
    { timeout: 5000 },
  );
  assert.equal(
    await page.evaluate(
      () => document.getElementById("progress-time")!.textContent,
    ),
    "0:40 / 2:00",
  );
  await show({ ...current, completed: 41 }, "正在播放");
  await show({ ...current, completed: 41, state: "paused" }, "播放已暂停");
  await page.evaluate(
    () => new Promise((resolve) => setTimeout(resolve, 1100)),
  );
  assert.equal(
    await page.evaluate(
      () => document.getElementById("progress-time")!.textContent,
    ),
    "0:41 / 2:00",
  );
  await show({ ...current, completed: 5, state: "buffering" }, "正在缓冲");
  await show(
    { ...current, completed: 120, state: "syncing" },
    "播放结束，等待平台确认",
  );
  assert.ok(
    app.operationId,
    "ended media is still an active operation while waiting for the platform",
  );
  app.update({
    progress: {
      description: "文档",
      completed: 3,
      total: null,
      state: "reading",
    },
  });
  await page.waitForFunction(
    () =>
      document.getElementById("progress-time")!.textContent === "已用 0:03" &&
      document.getElementById("progress")!.hidden,
    undefined,
    { timeout: 5000 },
  );
  app.update({ progress: null, status: "正在切换章节" });
  await page.waitForFunction(
    () => document.getElementById("progress-area")!.hidden,
    undefined,
    { timeout: 5000 },
  );
  await show({ ...current, description: "下一节", completed: 2 }, "正在播放");
  await page.cdp.send("Emulation.setEmulatedMedia", {
    features: [{ name: "prefers-reduced-motion", value: "reduce" }],
  });
  const opacity = await page.evaluate(() => {
    const style = getComputedStyle(document.querySelector(".spinner")!);
    return {
      name: style.animationName,
      opacity: Number(style.opacity),
      transform: style.transform,
    };
  });
  assert.equal(opacity.name, "pulse");
  assert.equal(opacity.transform, "none");
  await page.waitForFunction(
    (before) =>
      Math.abs(
        Number(getComputedStyle(document.querySelector(".spinner")!).opacity) -
          before,
      ) > 0.1,
    opacity.opacity,
    { timeout: 5000 },
  );
  await page.cdp.send("Emulation.setEmulatedMedia", { features: [] });

  for (const socket of server.sockets.clients) socket.terminate();
  await page.waitForFunction(
    () =>
      document.getElementById("task-banner")!.dataset.phase === "offline" &&
      document
        .getElementById("progress-state")!
        .textContent!.includes("最后收到"),
    undefined,
    { timeout: 5000 },
  );
  app.update({
    progress: { ...current, description: "重连后章节", completed: 8 },
    status: "正在学习：重连后章节",
  });
  await page.waitForFunction(
    () =>
      document.getElementById("task-banner")!.dataset.phase === "active" &&
      document.getElementById("progress-time")!.textContent === "0:08 / 2:00",
    undefined,
    { timeout: 5000 },
  );
  await (await page.query("#stop"))!.click();
  await page.waitForFunction(
    () =>
      (document.getElementById("stop") as HTMLButtonElement).disabled &&
      document.getElementById("stop-label")!.textContent ===
        "已设为本课后停止" &&
      document.getElementById("phase")!.textContent === "本课后停止",
    undefined,
    { timeout: 5000 },
  );
  assert.equal(app.stopAfter, true);
  await page.screenshot({ path: "build/browser-check/live-progress.png" });

  const account = "live-counts";
  app.repo.enqueue(app.repo.upsert(SUBJECT, account), "discover");
  app.repo.enqueue(app.repo.upsert(EXAM, account), "exam");
  app.repo.enqueue(
    app.repo.upsert(EXAM.replaceAll("22222222", "44444444"), account),
    "review",
  );
  await page.waitForFunction(
    () =>
      ["discovery_count", "exam_count", "manual_exam_count"].every(
        (id) => document.getElementById(id)!.textContent === "1",
      ),
    undefined,
    { timeout: 5000 },
  );
  await app.repo.clear(account, "all");
  await page.waitForFunction(
    () =>
      ["discovery_count", "exam_count", "manual_exam_count"].every(
        (id) => document.getElementById(id)!.textContent === "0",
      ),
    undefined,
    { timeout: 5000 },
  );
}

export async function liveQueueChecks(page: Tab, app: Application) {
  const first = "live-selected",
    second = "live-other",
    third = "live-added";
  const add = (account: string) =>
    app.repo.enqueue(app.repo.upsert(COURSE, account), "learn");
  const open = async (kind: string) => {
    await (await page.query(`[data-kind="${kind}"]`))!.click();
    await page.waitForFunction(
      () => (document.getElementById("queue-dialog") as HTMLDialogElement).open,
      undefined,
      { timeout: 5000 },
    );
  };
  const scope = async (account: string, count: number) => {
    await page.waitForFunction(
      ({ account, count }) =>
        (document.getElementById("queue-account") as HTMLSelectElement)
          .value === account &&
        document.getElementById("queue-count")!.textContent ===
          `已选择 ${count} 个任务`,
      { account, count },
      { timeout: 5000 },
    );
  };
  try {
    add(first);
    add(second);
    await open("clear_queues");
    await (await page.query("#queue-account"))!.select(first);
    await (await page.query('#queue-options input[value="courses"]'))!.click();
    await scope(first, 1);
    await app.repo.clear(first, "courses");
    await scope(first, 0);
    assert.equal(
      await page.evaluate(
        () =>
          (document.getElementById("queue-submit") as HTMLButtonElement)
            .disabled,
      ),
      true,
    );
    assert.equal(app.repo.queue(second, "learn").length, 1);
    await (await page.query("#queue-account"))!.select(second);
    await scope(second, 1);
    await page.keyboard.press("Escape");

    add(first);
    await open("export_queues");
    await (await page.query("#queue-account"))!.select("all");
    await scope("all", 2);
    await app.repo.clear(first, "courses");
    await scope("all", 1);
    add(third);
    await scope("all", 2);
    await page.keyboard.press("Escape");
  } finally {
    for (const account of [first, second, third])
      await app.repo.clear(account, "courses");
  }
}
