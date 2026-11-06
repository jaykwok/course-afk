import type { Tab } from "nodriver-ts";
import { PLATFORM } from "../config.ts";
import {
  entry,
  normalize,
  resource,
  UUID,
  platformHost,
  isCasePoolUrl,
} from "../domain/identity.ts";
import { PlatformHttp } from "../browser/http.ts";
import type { Authentication, Lease } from "../browser/auth.ts";
import type { TabScope } from "../browser/runtime.ts";
import type { Repository, Task } from "../storage/repository.ts";
import {
  AuthenticationError,
  ReviewRequired,
  data,
  list,
  sleep,
} from "../utils.ts";
import { clickText } from "./dom.ts";
export interface Manifest {
  links: string[];
  complete: boolean;
  message?: string;
  residual?: boolean;
  /** URL activity destinations are collected, but do not define that activity's completion. */
  optionalLinks?: string[];
}
export interface DiscoveryContext {
  repo: Repository;
  auth: Authentication;
  lease: Lease;
  scope: TabScope;
  report: (text: string) => void;
  confirm: (message: string) => Promise<boolean>;
}

export function subjectSectionCompleted(
  section: any,
  subject: Pick<Task, "businessId">,
): boolean {
  const p = section?.progress;
  return (
    !!section?.id &&
    !!p &&
    String(p.finishStatus) === "2" &&
    String(p.courseId).toLowerCase() === subject.businessId.toLowerCase() &&
    String(p.sectionId).toLowerCase() === String(section.id).toLowerCase() &&
    !!section.resourceId &&
    String(p.resourceId) === String(section.resourceId)
  );
}

export function subjectManifest(payload: unknown, subject: Task): Manifest {
  const links: string[] = [];
  const optionalLinks: string[] = [];
  const requiredLinks = new Set<string>();
  let complete = true,
    count = 0,
    residual = false;
  for (const chapter of list(payload)) {
    if (!Array.isArray(chapter?.courseChapterSections)) {
      complete = false;
      residual = true;
      continue;
    }
    for (const s of chapter.courseChapterSections) {
      count++;
      try {
        if (Number(s.sectionType) === 10) {
          const url = resource(
            "course",
            s.attachmentId || s.resourceId,
            subject.businessId,
            s.required,
          );
          links.push(url);
          requiredLinks.add(url);
        } else if (Number(s.sectionType) === 9) {
          const url = resource("exam", s.resourceId);
          links.push(url);
          requiredLinks.add(url);
        } else if (Number(s.sectionType) === 3) {
          residual ||= !subjectSectionCompleted(s, subject);
          const destination = new URL(s.url);
          if (
            !["http:", "https:"].includes(destination.protocol) ||
            destination.username ||
            destination.password
          )
            throw new Error("主题外链地址无效");
          // External documents are activities on the subject, not crawl roots.
          if (
            platformHost(destination.hostname) ||
            isCasePoolUrl(destination)
          ) {
            const destinations = extractLinks([destination.href], subject.url);
            optionalLinks.push(
              ...destinations.filter((url) => !links.includes(url)),
            );
            links.push(...destinations);
          }
        } else {
          complete = false;
          residual = true;
        }
      } catch {
        complete = false;
        residual = true;
      }
    }
  }
  return {
    links,
    complete: complete && count > 0,
    residual: residual || !complete || count === 0,
    optionalLinks: optionalLinks.filter((url) => !requiredLinks.has(url)),
    message: !complete ? "主题仍有无法识别的章节，已保留已发现课程" : undefined,
  };
}
export function extractLinks(texts: string[], base: string): string[] {
  const links = new Set<string>();
  const accept = (raw: string) => {
    try {
      raw = raw.trim().replace(/&amp;/g, "&");
      if (
        !raw ||
        raw.length > 8192 ||
        /[\s<>"'`\\{}()]/.test(raw) ||
        /%3[ce]/i.test(raw)
      )
        return;
      if (raw.startsWith("#") && !raw.startsWith("#/")) return;
      if (
        /^#?\/(?:study\/(?:course|subject)\/detail|exam\/(?:answer-paper|exam-detail)|train-new\/class-detail)\//i.test(
          raw,
        )
      )
        raw = `${PLATFORM}/#${raw.replace(/^#/, "")}`;
      const url = normalize(new URL(raw, base).href);
      const parsed = new URL(url);
      const e = entry(url, "discovery");
      if (
        e.kind !== "entry" ||
        (/\/topic\//i.test(parsed.pathname) &&
          /(?:\.html?\/?|\/)$/i.test(parsed.pathname)) ||
        isCasePoolUrl(parsed)
      )
        links.add(url);
    } catch {}
  };
  for (const input of texts) {
    const text = input.replace(/<!--[\s\S]*?-->/g, "");
    accept(text);
    const absolute = /https?:\/\/[^\s"'<>`\\{}()]+/g;
    for (const match of text.matchAll(absolute)) accept(match[0]);
    for (const match of text.matchAll(
      /(?:^|\s)(?:href|src|data-url)\s*=\s*["']([^"'<>`\s]+)["']/gi,
    ))
      accept(match[1]);
    // Remove full URLs first so query-form sources cannot also yield a bare course.
    for (const match of text
      .replace(absolute, "")
      .matchAll(
        /(?:#?\/study\/(?:course|subject)\/detail\/|#?\/exam\/(?:answer-paper|exam-detail)\/|#?\/train-new\/class-detail\/)[0-9a-f-]{36}(?:\/\d+\/[01]\/[0-9a-f-]{36})?(?:\?[^\s"'<>`\\{}()]*)?/gi,
      ))
      accept(`${PLATFORM}/#${match[0].replace(/^#/, "")}`);
  }
  return [...links];
}
export async function traverse(
  roots: string[],
  read: (task: Task, nested: boolean) => Promise<Manifest>,
  ctx: Pick<DiscoveryContext, "repo" | "lease" | "report">,
  limits = { entries: 100, depth: 12 },
) {
  const visited = new Map<string, boolean>(),
    active = new Set<string>();
  let expanded = 0;
  const found: Task[] = [],
    issues: { url: string; message: string }[] = [];
  const failure = (
    task: Task,
    message: string,
    reason = "discovery_incomplete",
  ) => {
    ctx.repo.review(task, reason, { message, discoveryComplete: false });
    issues.push({ url: task.url, message });
    visited.set(task.id, false);
  };
  async function visit(task: Task, depth: number): Promise<boolean> {
    ctx.lease.signal.throwIfAborted();
    if (visited.has(task.id)) return visited.get(task.id)!;
    if (active.has(task.id)) return true;
    if (task.kind === "course" || task.kind === "exam") {
      ctx.repo.enqueue(
        task,
        task.kind === "exam"
          ? task.manualSubmit
            ? "review"
            : "exam"
          : "learn",
      );
      found.push(task);
      visited.set(task.id, true);
      return true;
    }
    ctx.repo.enqueue(task, "discover");
    if (depth > limits.depth || expanded >= limits.entries) {
      failure(task, "入口数量或层级达到上限，请单独解析此入口");
      return false;
    }
    expanded++;
    active.add(task.id);
    ctx.report(`正在解析入口 ${expanded}：${task.url}`);
    try {
      const manifest = await read(task, depth > 0);
      ctx.repo.outcome(task.id, "READY", {
        discoveryComplete: false,
        residual: !!manifest.residual,
      });
      const optional = new Set(manifest.optionalLinks);
      const children = [...new Set(manifest.links)]
        .map((url) => ctx.repo.upsert(url, ctx.lease.account))
        .filter(
          (child) =>
            !active.has(child.id) &&
            ctx.repo.link(
              task,
              child,
              !optional.has(child.url) && child.required !== "0",
            ),
        );
      // Persist all children before recursively touching another container.
      for (const child of children)
        ctx.repo.enqueue(
          child,
          child.kind === "course"
            ? "learn"
            : child.kind === "exam"
              ? child.manualSubmit
                ? "review"
                : "exam"
              : "discover",
        );
      let complete = manifest.complete;
      for (const child of children.filter(
        (c) => c.kind === "course" || c.kind === "exam",
      ))
        await visit(child, depth + 1);
      for (const child of children)
        complete = (await visit(child, depth + 1)) && complete;
      if (manifest.residual) ctx.repo.enqueue(task, "learn");
      if (complete) {
        ctx.repo.remove(task.id, "discover");
        ctx.repo.remove(task.id, "failure");
        ctx.repo.outcome(
          task.id,
          manifest.residual ? "WAITING_SYNC" : "WAITING_CHILDREN",
          { discoveryComplete: true, residual: !!manifest.residual },
        );
        ctx.repo.reconcile(task.id);
      } else
        failure(
          task,
          manifest.message || "子入口尚未完整解析，已发现任务已保留",
        );
      visited.set(task.id, complete);
      return complete;
    } catch (e) {
      if (ctx.lease.signal.aborted || e instanceof AuthenticationError) throw e;
      failure(
        task,
        e instanceof Error ? e.message : "入口解析失败",
        e instanceof ReviewRequired ? e.reason : "discovery_incomplete",
      );
      return false;
    } finally {
      active.delete(task.id);
    }
  }
  const tasks = [...new Set(roots.map(normalize))].map((url) =>
    ctx.repo.upsert(url, ctx.lease.account),
  );
  for (const task of tasks)
    ctx.repo.enqueue(
      task,
      task.kind === "course"
        ? "learn"
        : task.kind === "exam"
          ? task.manualSubmit
            ? "review"
            : "exam"
          : "discover",
    );
  for (const task of tasks) await visit(task, 0);
  return { found, issues, complete: issues.length === 0 };
}
export async function discover(urls: string[], ctx: DiscoveryContext) {
  return traverse(
    urls,
    async (task, nested) => {
      const tab = await ctx.auth.navigate(ctx.scope, task.url, ctx.lease);
      try {
        const http = new PlatformHttp(tab, ctx.auth, ctx.lease);
        if (task.kind === "subject")
          return subjectManifest(
            await http.json(
              `/api/v1/course-study/subject/chapter-progress?courseId=${task.businessId}&knowledgePaymentEnable=false`,
            ),
            task,
          );
        if (task.kind === "class")
          return await classManifest(http, task, nested, ctx);
        return await catalogManifest(tab, ctx);
      } finally {
        await tab.close();
      }
    },
    ctx,
  );
}
async function classManifest(
  http: PlatformHttp,
  task: Task,
  nested: boolean,
  ctx: DiscoveryContext,
): Promise<Manifest> {
  const registration = "/gwapi/v1/saas-stu/web-biz/latest/train/class-student";
  const progress = async () => {
    const result = await http.json(
        `${registration}/registrationProgress?classId=${task.businessId}`,
      ),
      p = data(result);
    if (
      String(result.code) !== "0" ||
      p.classInfoId?.toLowerCase() !== task.businessId ||
      !["0", "1", "2", "3", "4", "5"].includes(String(p.registerStatus))
    )
      throw new ReviewRequired("enrollment_blocked", "培训班报名状态不完整");
    return p;
  };
  let p = await progress();
  const pending = ctx.repo.pending(task, "enroll");
  if (String(p.registerStatus) === "5") {
    for (const intent of pending)
      ctx.repo.confirm(intent.id, { registerStatus: 5 });
  } else {
    if (
      nested ||
      pending.length ||
      ["1", "3", "4"].includes(String(p.registerStatus))
    )
      throw new ReviewRequired(
        "enrollment_blocked",
        "培训班尚未确认报名，请在平台处理后重新解析",
      );
    const options = p.classInfoVo;
    if (
      !options ||
      String(options.payType) !== "0" ||
      String(options.signupFile) === "1" ||
      String(options.isRegisterReason) === "1"
    )
      throw new ReviewRequired(
        "enrollment_blocked",
        "此培训班需要额外报名资料或付费，请在平台报名",
      );
    if (
      !(await ctx.confirm(
        "此培训班尚未报名。是否提交一次免费报名申请并继续解析？",
      ))
    )
      throw new ReviewRequired("enrollment_blocked", "未报名，入口已保留");
    ctx.auth.assert(ctx.lease);
    const intent = ctx.repo.intent(task, "enroll", {
      registerStatus: p.registerStatus,
    });
    const result = await http.json(`${registration}/register`, {
      method: "POST",
      body: { classId: task.businessId },
      headers: { permitenable: "false" },
    });
    if (String(result.code) !== "0")
      throw new ReviewRequired(
        "enrollment_blocked",
        "报名结果未确认，请在平台核对",
      );
    for (let n = 0; n < 5; n++) {
      p = await progress();
      if (String(p.registerStatus) === "5") break;
      await sleep(500, ctx.lease.signal);
    }
    if (String(p.registerStatus) !== "5")
      throw new ReviewRequired(
        "enrollment_blocked",
        "报名申请已提交，待平台确认后重新解析",
      );
    ctx.repo.confirm(intent, { registerStatus: 5 });
  }
  const prefix = "/api/v1/training/student/class-info/safe";
  const chapters = list(
    await http.json(`${prefix}/chapter/paas?classId=${task.businessId}`),
  );
  if (!chapters.length)
    return { links: [], complete: false, message: "培训班章节清单为空" };
  const links: string[] = [];
  let complete = true;
  for (const chapter of chapters) {
    try {
      if (!UUID.test(String(chapter.id))) {
        complete = false;
        continue;
      }
      const seen = new Set<string>();
      let ended = false;
      for (let page = 1; page <= 200; page++) {
        const items = list(
          await http.json(
            `${prefix}/chapter-activity-list/paas${page > 1 ? "/more" : ""}?classId=${task.businessId}&chapterId=${chapter.id}&page=${page}&pageSize=5`,
          ),
        );
        let added = 0;
        for (const item of items) {
          const key = String(
            item.id || item.businessId || item.businessValue || "",
          );
          if (!key || seen.has(key)) continue;
          seen.add(key);
          added++;
          try {
            if (item.businessValue) {
              const value = normalize(decodeURIComponent(item.businessValue));
              links.push(value);
            } else {
              const kind = (
                { "1": "course", "8": "course", "2": "subject" } as const
              )[String(item.businessType) as "1" | "8" | "2"];
              if (kind) links.push(resource(kind, item.businessId));
              else complete = false;
            }
          } catch {
            complete = false;
          }
        }
        if (items.length < 5) {
          ended = true;
          break;
        }
        if (!added) break;
      }
      if (!ended) complete = false;
    } catch (e) {
      if (ctx.lease.signal.aborted || e instanceof AuthenticationError) throw e;
      complete = false;
    }
  }
  return {
    links,
    complete,
    message: complete ? undefined : "培训班部分活动未完整确认",
  };
}
async function catalogManifest(
  tab: Tab,
  ctx: DiscoveryContext,
): Promise<Manifest> {
  const found = new Set<string>();
  let complete = false;
  if (isCasePoolUrl(tab.url)) return casePoolManifest(tab, ctx);
  let previous = "";
  for (let round = 0; round < 21; round++) {
    ctx.auth.assert(ctx.lease);
    const snapshot = await tab.evaluate(
      () => {
        const values = [
          ...document.querySelectorAll(
            "a[href],iframe[src],[onclick],[data-url]",
          ),
        ].flatMap((el) =>
          ["href", "src", "onclick", "data-url"].map(
            (a) => el.getAttribute(a) || "",
          ),
        );
        values.push(document.documentElement.innerHTML);
        return values;
      },
      undefined,
      { timeout: 15000, signal: ctx.lease.signal },
    );
    for (const url of extractLinks(snapshot, tab.url)) found.add(url);
    const clicked = await clickText(
      tab,
      ["查看更多", "加载更多", "更多", "展开更多", "显示更多"],
      ctx.lease.signal,
      "a,button,span,div,li",
      () => ctx.auth.assert(ctx.lease),
    );
    if (!clicked) {
      if (!found.size && round < 10) {
        await sleep(500, ctx.lease.signal);
        continue;
      }
      complete = found.size > 0;
      break;
    }
    const signature = [...found].join("\n");
    if (round > 1 && signature === previous) break;
    previous = signature;
    await sleep(1200, ctx.lease.signal);
  }
  return {
    links: [...found],
    complete,
    message: complete ? undefined : "入口尚有未确认内容，已发现任务已保留",
  };
}
async function casePoolManifest(
  tab: Tab,
  ctx: DiscoveryContext,
): Promise<Manifest> {
  ctx.auth.assert(ctx.lease);
  if (!isCasePoolUrl(tab.url)) throw new Error("案例库页面已变化，请重新解析");
  // Requests remain inside the authenticated case-pool origin; tokens never leave the page.
  const result = await tab.evaluate(
    async () => {
      const values: string[] = [],
        seenTags = new Set<string>();
      const signal = AbortSignal.timeout(90000);
      if (
        location.origin !== "https://www.ctexpert.cn" ||
        location.pathname.replace(/\/$/, "") !== "/expert-assist-web/casePool"
      )
        return { values, complete: false };
      const token = localStorage.getItem("userID");
      if (!token) return { values, complete: false };
      const request = async (path: string, body: unknown) => {
        const r = await fetch(path, {
          method: "POST",
          redirect: "error",
          signal,
          headers: { "Content-Type": "application/json", token },
          body: JSON.stringify(body),
        });
        if (!r.ok) throw new Error("请求失败");
        const j = await r.json();
        if (Number(j.code) !== 200) throw new Error("请求被拒绝");
        return j.data;
      };
      try {
        const roots = await request(
          "/expert-assist-case/tag/listTree?source=home",
          { source: "home" },
        );
        const root = roots.find((r: any) => r.name === "案例资源");
        if (!root || !Array.isArray(root.childrenList))
          throw new Error("分类不完整");
        const tags: any[] = [];
        const visit = (n: any, depth = 0) => {
          if (depth > 12) throw new Error("分类层级过深");
          if (n?.id == null || seenTags.has(String(n.id))) return;
          seenTags.add(String(n.id));
          tags.push(n.id);
          for (const c of n.childrenList || []) visit(c, depth + 1);
        };
        for (const c of root.childrenList) visit(c);
        if (!tags.length) throw new Error("分类为空");
        for (const tag of tags) {
          const seen = new Set<string>();
          let ended = false;
          for (let pageNum = 1; pageNum <= 200; pageNum++) {
            const d = await request(
              "/expert-assist-case/case/getCaseHomeList",
              {
                classifyTagId: tag,
                sortFiled: "",
                sortType: "",
                pageSize: 100,
                pageNum,
              },
            );
            if (!Array.isArray(d?.records)) throw new Error("列表缺失");
            let added = 0;
            for (const r of d.records) {
              const key = String(r.id ?? r.caseId ?? r.url ?? "");
              if (!key) throw new Error("标识缺失");
              if (!seen.has(key)) {
                seen.add(key);
                added++;
              }
              if (typeof r.url === "string") values.push(r.url);
            }
            const total = d.total == null ? null : Number(d.total);
            if (
              (total !== null &&
                Number.isFinite(total) &&
                seen.size >= total) ||
              (total === null && !d.records.length)
            ) {
              ended = true;
              break;
            }
            if (!added) throw new Error("分页停滞");
          }
          if (!ended) throw new Error("分页上限");
        }
        return { values, complete: true };
      } catch {
        return { values, complete: false };
      }
    },
    undefined,
    { timeout: 95000, signal: ctx.lease.signal },
  );
  ctx.auth.assert(ctx.lease);
  return {
    links: extractLinks(result.values, tab.url),
    complete: result.complete,
    message: result.complete ? undefined : "案例库登录或分页未完整确认",
  };
}
