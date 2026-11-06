import { InputActionError, type Tab } from "nodriver-ts";
import { PLATFORM } from "../config.ts";
import {
  entry,
  normalize,
  resource,
  UUID,
  platformHost,
  isCasePoolUrl,
  isPlaceholderEntry,
  isPlatformHome,
  discoveryBoundary,
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
export interface Manifest {
  links: string[];
  complete: boolean;
  message?: string;
  residual?: boolean;
  /** URL activity destinations are collected, but do not define that activity's completion. */
  optionalLinks?: string[];
  /** Static content and missing pages are not learning tasks. */
  ignored?: string;
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
      if (isPlaceholderEntry(url)) return;
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
  limits = { entries: 1000, depth: 32 },
) {
  ctx.lease.signal.throwIfAborted();
  type State = "complete" | "pending" | "failed" | "ignored";
  const visited = new Map<string, State>(),
    active = new Set<string>();
  const observed = new Map<string, { task: Task; children: string[] }>();
  let expanded = 0;
  const found: Task[] = [],
    issues: { url: string; message: string }[] = [],
    pending: { url: string; message: string }[] = [];
  const failure = (
    task: Task,
    message: string,
    reason = "discovery_incomplete",
  ) => {
    ctx.repo.remove(task.id, "failure");
    ctx.repo.review(task, reason, {
      message,
      ignored: undefined,
      discoveryComplete: false,
      ownDiscoveryComplete: false,
    });
    issues.push({ url: task.url, message });
    visited.set(task.id, "failed");
  };
  const defer = (task: Task, message: string, direct = false) => {
    ctx.repo.remove(task.id, "failure");
    ctx.repo.outcome(task.id, direct ? "READY" : "WAITING_CHILDREN", {
      reason: "discovery_pending",
      ignored: undefined,
      message,
      discoveryComplete: false,
      ownDiscoveryComplete: !direct,
    });
    if (!direct) ctx.repo.remove(task.id, "discover");
    visited.set(task.id, "pending");
    if (direct) pending.push({ url: task.url, message });
  };
  ctx.repo.canonicalizeEntries(ctx.lease.account);
  const ignored = new Set(ctx.repo.discardNonCatalogEntries(ctx.lease.account));
  const inScope = discoveryBoundary(roots);
  const related = new Set<string>();
  const usable = (url: string) => {
    if (!isPlaceholderEntry(url) && !isPlatformHome(url)) return true;
    ignored.add(url);
    return false;
  };
  async function visit(task: Task, depth: number): Promise<State> {
    ctx.lease.signal.throwIfAborted();
    if (visited.has(task.id)) return visited.get(task.id)!;
    if (active.has(task.id)) return "complete";
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
      visited.set(task.id, "complete");
      return "complete";
    }
    ctx.repo.enqueue(task, "discover");
    if (depth > limits.depth || expanded >= limits.entries) {
      defer(task, "本轮解析额度已用完，此入口已保留待继续解析", true);
      return "pending";
    }
    expanded++;
    active.add(task.id);
    ctx.report(`正在解析入口 ${expanded}：${task.url}`);
    try {
      const manifest = await read(task, depth > 0);
      if (manifest.ignored && task.kind === "entry") {
        ctx.repo.discardEntry(task, manifest.ignored);
        ignored.add(task.url);
        visited.set(task.id, "ignored");
        return "ignored";
      }
      ctx.repo.outcome(task.id, "READY", {
        discoveryComplete: false,
        ownDiscoveryComplete: manifest.complete,
        residual: !!manifest.residual,
        ignored: undefined,
      });
      const optional = new Set(manifest.optionalLinks);
      const relatedEntries: string[] = [];
      const candidates = [...new Set(manifest.links.map(normalize))]
        .filter(usable)
        .filter((url) => {
          if (inScope(url)) return true;
          relatedEntries.push(url);
          ctx.repo.unlink(task.id, entry(url, ctx.lease.account).id);
          if (!related.has(url))
            ctx.report(`延伸专题未展开，可单独录入：${url}`);
          related.add(url);
          return false;
        })
        .map((url) => ctx.repo.upsert(url, ctx.lease.account));
      ctx.repo.outcome(task.id, "READY", { relatedEntries });
      // Keep the full graph for reachability, including back edges omitted by storage.
      observed.set(task.id, {
        task,
        children: candidates.map((child) => child.id),
      });
      const children = candidates.filter(
        (child) =>
          visited.get(child.id) !== "ignored" &&
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
      let childrenComplete = true;
      for (const child of children.filter(
        (c) => c.kind === "course" || c.kind === "exam",
      ))
        await visit(child, depth + 1);
      for (const child of children) {
        const state = await visit(child, depth + 1);
        childrenComplete =
          (state === "complete" || state === "ignored") && childrenComplete;
      }
      if (manifest.residual) ctx.repo.enqueue(task, "learn");
      if (manifest.complete && childrenComplete) {
        ctx.repo.remove(task.id, "discover");
        ctx.repo.remove(task.id, "failure");
        ctx.repo.outcome(
          task.id,
          manifest.residual ? "WAITING_SYNC" : "WAITING_CHILDREN",
          {
            discoveryComplete: true,
            ownDiscoveryComplete: true,
            residual: !!manifest.residual,
            reason: undefined,
            message: undefined,
          },
        );
        ctx.repo.reconcile(task.id);
        visited.set(task.id, "complete");
      } else if (!manifest.complete)
        failure(
          task,
          manifest.message || "子入口尚未完整解析，已发现任务已保留",
        );
      else defer(task, "子入口待解析或处理，已发现课程可正常学习");
      return visited.get(task.id)!;
    } catch (e) {
      if (ctx.lease.signal.aborted || e instanceof AuthenticationError) throw e;
      failure(
        task,
        e instanceof Error ? e.message : "入口解析失败",
        e instanceof ReviewRequired ? e.reason : "discovery_incomplete",
      );
      return "failed";
    } finally {
      active.delete(task.id);
    }
  }
  const tasks = [...new Set(roots.map(normalize))]
    .filter(usable)
    .map((url) => ctx.repo.upsert(url, ctx.lease.account));
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
  // A successful static branch is useful only if it reaches a business resource
  // or an unresolved page. Propagate backwards so cycles cannot hide such paths.
  const parents = new Map<string, string[]>();
  for (const [id, node] of observed)
    for (const child of node.children) {
      const ids = parents.get(child) || [];
      ids.push(id);
      parents.set(child, ids);
    }
  const useful = new Set(
    [...visited]
      .filter(
        ([id, state]) =>
          state !== "ignored" &&
          (state !== "complete" || ctx.repo.task(id).kind !== "entry"),
      )
      .map(([id]) => id),
  );
  const frontier = [...useful];
  for (let i = 0; i < frontier.length; i++)
    for (const parent of parents.get(frontier[i]) || [])
      if (!useful.has(parent)) {
        useful.add(parent);
        frontier.push(parent);
      }
  for (const [id, { task }] of observed) {
    if (
      task.kind !== "entry" ||
      useful.has(id) ||
      visited.get(id) !== "complete"
    )
      continue;
    ctx.repo.discardEntry(task, "此分支未发现学习入口，已跳过");
    ignored.add(task.url);
  }
  return {
    found,
    issues,
    pending,
    ignored: ignored.size,
    related: [...related],
    expanded,
    complete: issues.length === 0 && pending.length === 0,
  };
}
export async function discover(urls: string[], ctx: DiscoveryContext) {
  let apiTab: Tab | undefined;
  let catalogTab: Tab | undefined;
  const inScope = discoveryBoundary(urls);
  const scanned = new Set<string>();
  const platform = async (task: Task) => {
    if (!apiTab || apiTab.closed)
      apiTab = await ctx.auth.navigate(ctx.scope, task.url, ctx.lease);
    return new PlatformHttp(apiTab, ctx.auth, ctx.lease);
  };
  try {
    return await traverse(
      urls,
      async (task) => {
        if (task.kind === "subject")
          return subjectManifest(
            await (
              await platform(task)
            ).json(
              `/api/v1/course-study/subject/chapter-progress?courseId=${task.businessId}&knowledgePaymentEnable=false`,
            ),
            task,
          );
        if (task.kind === "class")
          return await classManifest(await platform(task), task, ctx);
        catalogTab = await ctx.auth.navigate(
          ctx.scope,
          task.url,
          ctx.lease,
          catalogTab?.closed ? undefined : catalogTab,
        );
        const actual = entry(catalogTab.url, ctx.lease.account);
        if (isPlatformHome(actual.url))
          return {
            links: [],
            complete: true,
            ignored: "页面回到平台首页，已停止扩展",
          };
        if (!inScope(actual.url)) {
          const snapshot = await catalogTab.evaluate(
            catalogSnapshot,
            undefined,
            { timeout: 15000, signal: ctx.lease.signal },
          );
          if (catalogPageState(snapshot) === "missing")
            return {
              links: [],
              complete: true,
              ignored: "平台占位或错误页，已跳过",
            };
          return {
            links: [],
            complete: false,
            message: `入口跳转到其他专题，已停止扩展：${actual.url}`,
          };
        }
        if (actual.kind !== "entry")
          return { links: [actual.url], complete: true };
        if (scanned.has(actual.url))
          return {
            links: [],
            complete: true,
            ignored: "页面返回已解析入口，已停止回流",
          };
        scanned.add(actual.url);
        return await catalogManifest(catalogTab, ctx);
      },
      ctx,
    );
  } finally {
    await apiTab?.close();
    await catalogTab?.close();
  }
}
async function classManifest(
  http: PlatformHttp,
  task: Task,
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
  const blockedStatus = () => {
    const message = (
      {
        "1": "培训班报名待审核，审核通过后可继续解析",
        "2": "培训班报名审核未通过，请在平台处理",
        "3": "培训班报名待审核，审核通过后可继续解析",
        "4": "培训班报名待缴费，请在平台处理",
      } as Record<string, string>
    )[String(p.registerStatus)];
    if (message) throw new ReviewRequired("enrollment_blocked", message);
  };
  const pending = ctx.repo.pending(task, "enroll");
  if (String(p.registerStatus) === "5") {
    for (const intent of pending)
      ctx.repo.confirm(intent.id, { registerStatus: 5 });
    ctx.report("培训班已报名，继续解析课程");
  } else {
    blockedStatus();
    // registrationProgress omits the signup dates. The class page's basic
    // endpoint provides the window and server time, even for an expired class.
    const detail = await http.json(
      `/api/v1/training/test-student/class-info/safe/front/basic/${task.businessId}`,
    );
    const info = data(detail);
    if (
      (detail.code !== undefined && String(detail.code) !== "0") ||
      info.id?.toLowerCase() !== task.businessId ||
      !Object.hasOwn(info, "registerStartTime") ||
      !Object.hasOwn(info, "registerEndTime")
    )
      throw new ReviewRequired(
        "enrollment_blocked",
        "培训班报名期限尚未确认，请重试",
      );
    const date = (value: unknown) => {
      if (value === null || value === undefined || value === "") return;
      // This platform returns Chinese local time without an offset. Do not
      // interpret it in the timezone of whichever machine runs the workbench.
      const raw = String(value).trim();
      const local =
        /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,3})?)?$/;
      const time =
        typeof value === "number"
          ? value
          : Date.parse(
              local.test(raw) ? `${raw.replace(" ", "T")}+08:00` : raw,
            );
      if (!Number.isFinite(time))
        throw new ReviewRequired(
          "enrollment_blocked",
          "培训班报名时间格式无法识别，请在平台核对",
        );
      return time;
    };
    const start = date(info.registerStartTime),
      end = date(info.registerEndTime),
      now = date(info.currentTime);
    if (now === undefined)
      throw new ReviewRequired(
        "enrollment_blocked",
        "平台当前时间尚未确认，请重试",
      );
    const format = (time: number) =>
      new Date(time + 8 * 3600000).toISOString().slice(0, 16).replace("T", " ");
    if (end !== undefined && now > end)
      throw new ReviewRequired(
        "enrollment_closed",
        `培训班报名已结束（截止 ${format(end)}，北京时间），请联系班务人员`,
      );
    if (start !== undefined && now < start)
      throw new ReviewRequired(
        "enrollment_not_open",
        `培训班报名尚未开始（${format(start)}，北京时间），请在开放后重试`,
      );
    if (pending.length)
      throw new ReviewRequired(
        "enrollment_blocked",
        "上次报名结果尚未确认，已重新查询状态，请在平台核实后重试",
      );
    const options = p.classInfoVo;
    if (
      !options ||
      String(options.payType) !== "0" ||
      String(options.signupFile) === "1" ||
      (String(options.isRegister) === "1" &&
        String(options.isRegisterReason) === "1")
    )
      throw new ReviewRequired(
        "enrollment_blocked",
        "此培训班需要额外报名资料或付费，请在平台报名",
      );
    ctx.report("培训班尚未报名，正在自动报名");
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
      if (n) await sleep(500, ctx.lease.signal);
      p = await progress();
      if (String(p.registerStatus) === "5") break;
      blockedStatus();
    }
    if (String(p.registerStatus) !== "5")
      throw new ReviewRequired(
        "enrollment_blocked",
        "报名接口已返回，但平台尚未确认报名成功，请稍后重试核验",
      );
    ctx.repo.confirm(intent, { registerStatus: 5 });
    ctx.report("培训班报名成功，开始解析课程");
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
export interface CatalogSnapshot {
  url: string;
  title: string;
  text: string;
  ready: boolean;
  values: string[];
  status: number;
  article: boolean;
  busy: boolean;
  interactive: boolean;
}
/** Read document semantics; no URL, directory name or site-specific class rules. */
export function catalogSnapshot(): CatalogSnapshot {
  const text = document.body?.innerText.trim() || "";
  const visible = (e: Element) =>
    e.getBoundingClientRect().height > 0 &&
    getComputedStyle(e).visibility !== "hidden";
  const headings = [...document.querySelectorAll("h1,h2")].filter(visible);
  const title = document.title.trim();
  const titleHeading =
    !!title && headings.some((e) => e.textContent?.trim() === title);
  const linkText = [...document.querySelectorAll("a")]
    .filter(visible)
    .reduce((length, e) => length + (e.textContent?.trim().length || 0), 0);
  const prose = text.length >= 100 && text.length > linkText * 3;
  const article =
    prose &&
    (titleHeading ||
      !!document.querySelector(
        'article,[itemtype="https://schema.org/Article"],[itemtype="http://schema.org/Article"],meta[property="og:type"][content="article"]',
      ));
  const navigation = performance.getEntriesByType("navigation")[0] as
    PerformanceNavigationTiming | undefined;
  return {
    url: location.href,
    title,
    text,
    ready: document.readyState !== "loading",
    values: [document.documentElement.outerHTML],
    status: navigation?.responseStatus || 0,
    article,
    busy: [
      ...document.querySelectorAll('[aria-busy="true"],[role="progressbar"]'),
    ].some(visible),
    interactive: [
      ...document.querySelectorAll(
        'button,input,select,[onclick],[data-url],[role="button"]',
      ),
    ].some(visible),
  };
}
export function catalogPageState(snapshot: CatalogSnapshot) {
  const text = snapshot.text.replace(/\s+/g, " ").trim();
  if ([404, 410].includes(snapshot.status)) return "missing";
  if (snapshot.status >= 400 || snapshot.busy) return "unknown";
  if (
    /^(?:sorry\s*[,，:]?\s*)?(?:(?:404|410)\s*[:：-]?\s*)?(?:file|page|resource) not found[.!。]?$/i.test(
      text,
    ) ||
    /^(?:暂无内容[，,\s]*)?敬请期待[.…。\s]*$/.test(text)
  )
    return "missing";
  if (
    snapshot.ready &&
    snapshot.article &&
    !snapshot.interactive &&
    !/加载中|正在加载|请稍候|验证|登录|访问失败|服务异常/.test(text)
  )
    return "article";
  return "unknown";
}
async function expandCatalog(tab: Tab, ctx: DiscoveryContext) {
  for (const element of await tab.queryAll("a,button,span,div,li", {
    visible: true,
    timeout: 5000,
    signal: ctx.lease.signal,
  })) {
    const candidate = await element.evaluate((e) => {
      const link = e.closest("a[href],[onclick],[data-url]");
      return {
        text: (e.innerText || e.textContent || "").replace(/\s/g, ""),
        disabled:
          e.hasAttribute("disabled") ||
          e.getAttribute("aria-disabled") === "true",
        href: link?.getAttribute("href") || "",
        values: ["href", "onclick", "data-url"].map(
          (a) => link?.getAttribute(a) || "",
        ),
      };
    });
    if (
      !["查看更多", "加载更多", "更多", "展开更多", "显示更多"].includes(
        candidate.text,
      ) ||
      candidate.disabled
    )
      continue;
    // A "more" navigation (including its child span) is already in the manifest.
    // Click only controls that expand this document, never links that open a page.
    if (
      (candidate.href &&
        candidate.href !== "#" &&
        !/^javascript:/i.test(candidate.href)) ||
      extractLinks(candidate.values, tab.url).length
    )
      continue;
    await element.click({
      signal: ctx.lease.signal,
      timeout: 5000,
      guard: () => ctx.auth.assert(ctx.lease),
    });
    return true;
  }
  return false;
}
export async function catalogManifest(
  tab: Tab,
  ctx: DiscoveryContext,
): Promise<Manifest> {
  const found = new Set<string>();
  let complete = false;
  if (isCasePoolUrl(tab.url)) return casePoolManifest(tab, ctx);
  let previous = "",
    emptySignature = "",
    emptyStable = 0,
    message: string | undefined;
  for (let round = 0; round < 21; round++) {
    ctx.auth.assert(ctx.lease);
    try {
      const snapshot = await tab.evaluate(catalogSnapshot, undefined, {
        timeout: 15000,
        signal: ctx.lease.signal,
      });
      ctx.auth.assert(ctx.lease);
      const links = extractLinks(snapshot.values, snapshot.url);
      const pageState = catalogPageState(snapshot);
      const text = snapshot.text.replace(/\s+/g, " ").trim();
      const contentSignature = JSON.stringify([snapshot.title, text]);
      emptyStable = contentSignature === emptySignature ? emptyStable + 1 : 0;
      emptySignature = contentSignature;
      if (!found.size && !links.length && pageState === "missing")
        return {
          links: [],
          complete: true,
          ignored: "平台占位或错误页，已跳过",
        };
      for (const url of links) {
        found.add(url);
      }
      if (snapshot.status >= 400) {
        message = `页面请求未成功（HTTP ${snapshot.status}），请稍后重试`;
        break;
      }
      const clicked = await expandCatalog(tab, ctx);
      if (!clicked) {
        if (snapshot.busy) {
          if (round < 10) {
            await sleep(500, ctx.lease.signal);
            continue;
          }
          message = "页面仍在加载，已发现任务已保留，请稍后重试";
          break;
        }
        if (!found.size && round < 10) {
          await sleep(500, ctx.lease.signal);
          continue;
        }
        if (!found.size && emptyStable >= 3 && pageState === "article")
          return {
            links: [],
            complete: true,
            ignored: "内容介绍页未发现学习入口，已跳过",
          };
        complete = found.size > 0;
        break;
      }
      const signature = [...found].join("\n");
      if (round > 1 && signature === previous) break;
      previous = signature;
      await sleep(1200, ctx.lease.signal);
    } catch (e) {
      if (e instanceof InputActionError) await e.cleanup;
      if (ctx.lease.signal.aborted || e instanceof AuthenticationError) throw e;
      // An expansion failure must not erase links collected before the click.
      message = "页面展开未完成，已发现任务已保留，请重新解析此入口";
      break;
    }
  }
  return {
    links: [...found],
    complete,
    message: complete
      ? undefined
      : message || "入口尚有未确认内容，已发现任务已保留",
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
