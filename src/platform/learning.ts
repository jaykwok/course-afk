import {
  InputActionError,
  StaleElementError,
  TimeoutError,
  type Tab,
} from "nodriver-ts";
import type { Configuration } from "../config.ts";
import type { Task } from "../storage/repository.ts";
import { resource } from "../domain/identity.ts";
import { PlatformHttp } from "../browser/http.ts";
import {
  data,
  ReviewRequired,
  failureRecovery,
  type Recovery,
  AuthenticationError,
  sleep,
  list,
} from "../utils.ts";
import type { DiscoveryContext } from "./discovery.ts";
import { discover, subjectManifest } from "./discovery.ts";
import { examState, readExamState } from "./exam.ts";
import {
  clickText,
  clickPlayerControl,
  dismissOverlays,
  inspectAccess,
  continueArchivedCourse,
  startRegisteredCourse,
} from "./dom.ts";
export interface Section {
  id: string;
  resourceId: string;
  sectionType: number;
  name?: string;
  totalTime?: number;
  attachmentId?: string;
  fileType?: string;
  guideStudyFlag?: boolean;
}
export async function courseInfo(http: PlatformHttp, task: Task) {
  const query = task.sourceType
    ? `?${new URLSearchParams({ type: task.sourceType, sourceId: task.sourceId! })}`
    : "";
  const info = data(
    await http.json(
      `/api/v1/course-study/course-front/info/${task.businessId}${query}`,
    ),
  );
  if (
    String(info.id).toLowerCase() !== task.businessId ||
    !Array.isArray(info.courseChapters)
  )
    throw new Error("课程详情尚未完整确认");
  const sections: Section[] = info.courseChapters.flatMap((c: any) => {
    if (!Array.isArray(c.courseChapterSections))
      throw new Error("章节清单缺失");
    return c.courseChapterSections;
  });
  if (
    !sections.length ||
    sections.some((s) => !s.id || !s.resourceId) ||
    new Set(sections.map((s) => s.id)).size !== sections.length
  )
    throw new Error("课程章节标识不完整");
  return { info, sections };
}
export function sectionStates(
  payload: unknown,
  task: Pick<Task, "businessId">,
  sections: Section[],
): Map<string, any> {
  if (!Array.isArray(payload)) throw new Error("章节进度列表缺失");
  const map = new Map<string, any>(),
    resources = new Map(
      sections.map((s) => [s.id.toLowerCase(), s.resourceId]),
    );
  for (const r of payload) {
    const id = String(r.sectionId || "").toLowerCase();
    if (
      String(r.courseId).toLowerCase() !== task.businessId ||
      resources.get(id) !== String(r.resourceId) ||
      map.has(id)
    )
      throw new Error("章节进度与课程不匹配");
    map.set(id, r);
  }
  return map;
}
export async function progress(
  http: PlatformHttp,
  task: Task,
  sections: Section[],
) {
  const body = new URLSearchParams({
    courseId: task.businessId,
    resourceIds: [...new Set(sections.map((s) => s.resourceId))].join(","),
  }).toString();
  return sectionStates(
    await http.json(
      "/api/v1/course-study/course-front/course-section-progress",
      {
        method: "POST",
        body,
        readOnly: true,
        headers: {
          "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        },
      },
    ),
    task,
    sections,
  );
}
async function prepare(tab: Tab, ctx: DiscoveryContext) {
  await inspectAccess(tab, ctx.lease.signal);
  ctx.auth.assert(ctx.lease);
  await continueArchivedCourse(tab, ctx.lease.signal, () =>
    ctx.auth.assert(ctx.lease),
  );
  await dismissOverlays(tab, ctx.lease.signal);
  await clickText(
    tab,
    ["课程学习"],
    ctx.lease.signal,
    '.guide-tab,.guide-tab--item,[class*="guide-tab"]',
  );
}
async function rating(tab: Tab, ctx: DiscoveryContext) {
  ctx.auth.assert(ctx.lease);
  await continueArchivedCourse(tab, ctx.lease.signal, () =>
    ctx.auth.assert(ctx.lease),
  );
  for (const dialog of await tab.queryAll(".ant-modal-content", {
    visible: true,
    timeout: 5000,
    signal: ctx.lease.signal,
  })) {
    const star = await dialog.query(".ant-rate li:nth-child(5)", {
      visible: true,
      timeout: 3000,
      signal: ctx.lease.signal,
    });
    if (!star) continue;
    await star.click({
      signal: ctx.lease.signal,
      timeout: 5000,
      guard: () => ctx.auth.assert(ctx.lease),
    });
    for (const button of await dialog.queryAll("button:not([disabled])", {
      visible: true,
      timeout: 3000,
      signal: ctx.lease.signal,
    }))
      if ((await button.text()).replace(/\s/g, "") === "确定") {
        await button.click({
          signal: ctx.lease.signal,
          timeout: 5000,
          guard: () => ctx.auth.assert(ctx.lease),
        });
        break;
      }
  }
}
export async function activate(tab: Tab, s: Section, ctx: DiscoveryContext) {
  await rating(tab, ctx);
  await dismissOverlays(tab, ctx.lease.signal);
  const selector = `dl.chapter-list-box[id$="${s.id}"]`;
  for (let n = 0; n < 3; n++) {
    ctx.auth.assert(ctx.lease);
    try {
      const box = await tab.waitForSelector(
        `${selector} .section-item-wrapper`,
        {
          timeout: 10000,
          signal: ctx.lease.signal,
        },
      );
      // The platform can activate the first chapter itself during page startup.
      if (
        await box.evaluate((e) =>
          e.closest("dl.chapter-list-box")?.classList.contains("focus"),
        )
      )
        return selector;
      await box.click({
        signal: ctx.lease.signal,
        timeout: 5000,
        guard: () => ctx.auth.assert(ctx.lease),
      });
      await tab.waitForFunction(
        (selector) =>
          document.querySelector(selector)?.classList.contains("focus"),
        selector,
        { timeout: 5000, signal: ctx.lease.signal },
      );
      return selector;
    } catch (e) {
      if (ctx.lease.signal.aborted) throw e;
      if (!tab.closed) await inspectAccess(tab, ctx.lease.signal);
      // The SPA can replace chapter rows after the initial API response. A
      // click interrupted before dispatch is safe to retry with a fresh row.
      const detachedBeforeInput =
        e instanceof InputActionError &&
        e.report.events === 0 &&
        e.report.completedSegments === 0 &&
        e.cause instanceof StaleElementError;
      if (detachedBeforeInput) {
        const cleanup = await e.cleanup;
        if (cleanup.status !== "completed") throw e;
      } else if (
        !(e instanceof StaleElementError) &&
        !(e instanceof TimeoutError)
      )
        throw e;
    }
    await sleep(500, ctx.lease.signal);
  }
  throw new ReviewRequired("section_activation_failed", "章节未进入学习状态");
}
async function resumeMedia(tab: Tab, selector: string, ctx: DiscoveryContext) {
  ctx.auth.assert(ctx.lease);
  await startRegisteredCourse(tab, ctx.lease.signal, () =>
    ctx.auth.assert(ctx.lease),
  );
  const focused = await tab.evaluate(
    (selector) =>
      !!document.querySelector(selector)?.classList.contains("focus"),
    selector,
    { timeout: 5000, signal: ctx.lease.signal },
  );
  if (!focused) return { focused: false, videos: [] };
  // Video.js needs a genuine input gesture to unlock playback on a fresh profile.
  const play = await tab.query(
    ".video-js.vjs-paused .vjs-big-play-button,.video-js.vjs-paused .vjs-play-control",
    { visible: true, timeout: 5000, signal: ctx.lease.signal },
  );
  if (play)
    await clickPlayerControl(tab, play, ctx.lease.signal, () =>
      ctx.auth.assert(ctx.lease),
    );
  return tab.evaluate(
    async (selector) => {
      const target = document.querySelector(selector);
      if (!target?.classList.contains("focus"))
        return { focused: false, videos: [] };
      const videos = [...document.querySelectorAll("video")].filter(
        (v) => v.getBoundingClientRect().height > 0,
      );
      for (const v of videos)
        if (v.paused && !v.ended && v.readyState >= 1) {
          try {
            await v.play();
          } catch {}
        }
      return {
        focused: true,
        videos: videos.map((v) => ({
          time: v.currentTime,
          duration: v.duration,
          paused: v.paused,
          ended: v.ended,
          error: !!v.error,
        })),
      };
    },
    selector,
    { timeout: 5000, signal: ctx.lease.signal },
  );
}
export async function learn(
  task: Task,
  ctx: DiscoveryContext,
  config: Configuration,
  onProgress: (value: unknown) => void,
) {
  if (task.kind !== "course") {
    await discover([task.url], ctx);
    if (
      task.kind === "subject" &&
      !ctx.repo.hasFailure(task.id) &&
      ctx.repo.task(task.id).details.residual
    )
      await learnSubjectLinks(task, ctx);
    return;
  }
  const tab = await ctx.auth.navigate(ctx.scope, task.url, ctx.lease);
  await tab.bringToFront();
  const http = new PlatformHttp(tab, ctx.auth, ctx.lease);
  try {
    await prepare(tab, ctx);
    const { sections } = await courseInfo(http, task);
    let states = await progress(http, task, sections);
    const issues: { section: string; message: string; recovery: Recovery }[] =
      [];
    let examsPending = false;
    for (const s of sections) {
      ctx.auth.assert(ctx.lease);
      if (Number(s.sectionType) === 9) {
        const exam = ctx.repo.upsert(
          resource("exam", s.resourceId),
          task.account,
        );
        ctx.repo.link(task, exam);
        const state = examState(await readExamState(http, exam.businessId));
        if (state.passed)
          ctx.repo.complete(exam, { source: "exam-user-record", passed: true });
        else {
          examsPending = true;
          if (
            state.pending ||
            (state.allowed !== 0 &&
              (state.remaining === undefined || state.remaining <= 1))
          )
            ctx.repo.review(
              exam,
              state.pending ? "pending_grading" : "attempt_limit",
              { message: state.pending ? "等待评卷" : "考试次数不足或未知" },
              true,
            );
          else ctx.repo.enqueue(exam, exam.manualSubmit ? "review" : "exam");
        }
        continue;
      }
      if (String(states.get(s.id.toLowerCase())?.finishStatus) === "2")
        continue;
      if (![1, 2, 3, 5, 6].includes(Number(s.sectionType))) {
        issues.push({
          section: s.id,
          recovery: "blocked",
          message:
            Number(s.sectionType) === 4
              ? "H5 章节需要人工学习"
              : "章节类型需要人工处理",
        });
        continue;
      }
      try {
        ctx.report(`正在学习：${s.name || s.id}`);
        const selector = await activate(tab, s, ctx),
          video = [5, 6].includes(Number(s.sectionType));
        const start = Date.now(),
          timeout = video
            ? config.sectionTimeout * 1000
            : Math.min(config.sectionTimeout, 300) * 1000;
        let lastTime = -1,
          lastAdvance = Date.now(),
          finished = false;
        while (Date.now() - start < timeout) {
          ctx.auth.assert(ctx.lease);
          await inspectAccess(tab, ctx.lease.signal);
          await rating(tab, ctx);
          await dismissOverlays(tab, ctx.lease.signal);
          const playback = await resumeMedia(tab, selector, ctx);
          if (!playback.focused)
            throw new ReviewRequired(
              "section_focus_lost",
              "当前章节焦点已变化",
            );
          const media = playback.videos[0];
          if (video) {
            if (media?.error)
              throw new ReviewRequired("video_error", "视频播放失败");
            if (media && media.time > lastTime + 0.1) {
              lastTime = media.time;
              lastAdvance = Date.now();
            }
            if (
              Date.now() - lastAdvance > config.stallLimit * 1000 &&
              !media?.ended
            )
              throw new ReviewRequired("video_stalled", "视频长时间未推进");
          }
          states = await progress(http, task, sections);
          if (String(states.get(s.id.toLowerCase())?.finishStatus) === "2") {
            finished = true;
            break;
          }
          onProgress({
            description: s.name || "学习进度",
            completed: Math.floor((Date.now() - start) / 1000),
            total: Math.ceil(
              video && media?.duration ? media.duration : timeout / 1000,
            ),
          });
          await sleep(config.watchSlice * 1000, ctx.lease.signal);
        }
        if (!finished)
          throw new ReviewRequired("sync_timeout", "平台尚未确认本节完成");
      } catch (e) {
        if (ctx.lease.signal.aborted || e instanceof AuthenticationError)
          throw e;
        issues.push({
          section: s.id,
          message: e instanceof Error ? e.message : "章节学习失败",
          recovery: failureRecovery(e),
        });
      }
    }
    states = await progress(http, task, sections);
    const allMedia = sections
      .filter((s) => Number(s.sectionType) !== 9)
      .every(
        (s) => String(states.get(s.id.toLowerCase())?.finishStatus) === "2",
      );
    if (allMedia && !examsPending && !issues.length)
      ctx.repo.complete(task, {
        source: "course-section-progress",
        sections: [...states.keys()],
        readAt: new Date().toISOString(),
      });
    else if (issues.length)
      ctx.repo.review(task, "partial_course_failure", {
        message: "部分章节尚未完成",
        sections: issues,
        recovery: issues.some((issue) => issue.recovery === "retry")
          ? "retry"
          : "blocked",
      });
    else
      ctx.repo.outcome(
        task.id,
        examsPending ? "WAITING_CHILDREN" : "WAITING_SYNC",
        {
          message: examsPending ? "课程考试待完成" : "等待平台同步",
          discoveryComplete: true,
          residual: !allMedia,
        },
      );
  } finally {
    onProgress(null);
    await tab.close();
  }
}
async function learnSubjectLinks(task: Task, ctx: DiscoveryContext) {
  const tab = await ctx.auth.navigate(ctx.scope, task.url, ctx.lease);
  try {
    const items = await tab.evaluate(
      () =>
        [...document.querySelectorAll(".item.current-hover")].map(
          (e, index) => ({
            index,
            type: e.querySelector(".section-type")?.textContent?.trim(),
            done: !!e.querySelector(".icon-reload"),
          }),
        ),
      undefined,
      { timeout: 10000, signal: ctx.lease.signal },
    );
    for (const item of items.filter((i) => i.type === "URL" && !i.done)) {
      ctx.auth.assert(ctx.lease);
      const popup = tab.waitForPopup({
        timeout: 10000,
        signal: ctx.lease.signal,
      });
      const click = (async () => {
        const items = await tab.queryAll(".item.current-hover", {
          timeout: 5000,
          signal: ctx.lease.signal,
        });
        const element = await items[item.index]?.query(
          ".inline-block.operation",
          { timeout: 5000, signal: ctx.lease.signal },
        );
        if (!element) throw new Error("外链入口缺失");
        await element.click({
          timeout: 5000,
          signal: ctx.lease.signal,
          guard: () => ctx.auth.assert(ctx.lease),
        });
      })();
      const [child] = await Promise.all([popup, click]);
      ctx.scope.tabs.add(child);
      await sleep(10000, ctx.lease.signal);
      await child.close();
    }
    await tab.reload({
      waitUntil: "domcontentloaded",
      timeout: 30000,
      signal: ctx.lease.signal,
    });
    // Only the platform's refreshed state is evidence; a dwell time itself is not completion.
    const http = new PlatformHttp(tab, ctx.auth, ctx.lease);
    const chapters = list(
      await http.json(
        `/api/v1/course-study/subject/chapter-progress?courseId=${task.businessId}&knowledgePaymentEnable=false`,
      ),
    );
    const manifest = subjectManifest(chapters, task);
    // Local activity progress cannot repair an incomplete child discovery.
    const discoveryComplete =
      manifest.complete &&
      ctx.repo.task(task.id).details.discoveryComplete === true;
    if (!manifest.residual && discoveryComplete) {
      ctx.repo.remove(task.id, "failure");
      ctx.repo.outcome(task.id, "WAITING_CHILDREN", {
        residual: false,
        discoveryComplete: true,
      });
      ctx.repo.reconcile(task.id);
    } else if (manifest.complete && !manifest.residual) {
      // A child's failure is recorded on that child, not again on every parent.
      ctx.repo.remove(task.id, "learn");
      ctx.repo.remove(task.id, "failure");
      ctx.repo.outcome(task.id, "WAITING_CHILDREN", {
        residual: false,
        discoveryComplete: false,
        ownDiscoveryComplete: true,
        recovery: undefined,
        reason: "discovery_pending",
        message: "子入口待处理，已确认的主题活动进度已保留",
      });
    } else
      ctx.repo.review(
        task,
        manifest.residual ? "subject_residual" : "discovery_incomplete",
        {
          residual: !!manifest.residual,
          discoveryComplete,
          message: manifest.residual
            ? "主题外链或其他章节仍需平台确认或人工处理"
            : "子入口尚未完整解析，已确认的主题活动进度已保留",
        },
      );
  } finally {
    await tab.close();
  }
}
