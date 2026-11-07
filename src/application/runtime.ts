import { EventEmitter } from "node:events";
import { join } from "node:path";
import { accountId, parseLinkInput, entry } from "../domain/identity.ts";
import {
  HOME,
  readSettings,
  prepareSettings,
  saveSettings,
} from "../config.ts";
import {
  token,
  digest,
  AuthenticationError,
  ReviewRequired,
  failureRecovery,
} from "../utils.ts";
import { redact, type Logger } from "../log.ts";
import type { Repository, Queue } from "../storage/repository.ts";
import { atomicJson } from "../storage/files.ts";
import type { BrowserRuntime } from "../browser/runtime.ts";
import { Authentication } from "../browser/auth.ts";
import type { Configuration } from "../config.ts";
import { AiClient, aiConfigured, aiSettings } from "../ai/client.ts";
import { discover, type DiscoveryContext } from "../platform/discovery.ts";
import { learn } from "../platform/learning.ts";
import { runExam } from "../platform/exam.ts";
import { references } from "../platform/references.ts";
import {
  adoptImports,
  exportBundle,
  importBundle,
  IMPORT_INBOX,
  taskIssue,
} from "../storage/transfer.ts";

export class ProtocolError extends Error {
  status: number;
  constructor(message: string, status = 409) {
    super(message);
    this.status = status;
  }
}
export const OPERATIONS = [
  "learn",
  "exam",
  "links",
  "select",
  "settings",
  "references",
  "retry_tasks",
  "login",
  "switch_account",
  "clear_credentials",
  "clear_queues",
  "export_queues",
  "import_queues",
] as const;
type Operation = (typeof OPERATIONS)[number];
const CLEAR_SCOPES = ["failures", "courses", "exams", "all"] as const;
const OPERATION_TITLES: Record<Operation, string> = {
  learn: "课程学习",
  exam: "自动考试",
  links: "解析链接",
  select: "在平台选择课程",
  settings: "保存设置",
  references: "下载资料",
  retry_tasks: "重新排队",
  login: "登录账号",
  switch_account: "切换账号",
  clear_credentials: "清除登录信息",
  clear_queues: "清空队列",
  export_queues: "导出任务",
  import_queues: "导入任务",
};
export class Application extends EventEmitter {
  readonly repo: Repository;
  readonly browser: BrowserRuntime;
  readonly config: Configuration;
  readonly logger: Logger;
  readonly auth: Authentication;
  readonly instanceId = token();
  readonly exports = new Map<string, { path: string; filename: string }>();
  seq = 0;
  operationId: string | null = null;
  kind: Operation | null = null;
  abort?: AbortController;
  stopAfter = false;
  exitRequested = false;
  connected = false;
  view: any = {
    title: "自动化挂课工作台",
    status: "就绪",
    summary: null,
    progress: null,
    prompt: null,
    notification: null,
  };
  logs: { seq: number; message: string; level: string }[] = [];
  droppedLogs = 0;
  private requests = new Map<
    string,
    { fingerprint: string; result: unknown }
  >();
  private promptWaiter?: {
    resolve: (v: any) => void;
    reject: (e: unknown) => void;
  };
  private active?: Promise<void>;
  private disconnectTimer?: NodeJS.Timeout;
  constructor(
    repo: Repository,
    browser: BrowserRuntime,
    config: Configuration,
    logger: Logger,
  ) {
    super();
    this.repo = repo;
    this.browser = browser;
    this.config = config;
    this.logger = logger;
    this.auth = new Authentication(browser, config.dataDir);
    logger.listener = (message, level) => {
      this.seq++;
      if (this.logs.length === 400) {
        this.logs.shift();
        this.droppedLogs++;
      }
      this.logs.push({ seq: this.seq, message, level });
      this.emit("change");
    };
  }
  get account() {
    return accountId(this.auth.read()?.account || "");
  }
  dashboard() {
    const account = this.account === "unbound" ? null : this.account;
    const counts = Object.fromEntries(
      (["learn", "failure", "exam", "review", "discover"] as Queue[]).map(
        (q) => [q, this.repo.queue(account, q).length],
      ),
    );
    const auth = this.auth.dashboard();
    const failures = this.repo.queue(account, "failure");
    const importedLearning =
      this.repo.queue(IMPORT_INBOX, "learn").length +
      this.repo.queue(IMPORT_INBOX, "discover").length;
    const importedExams = this.repo.queue(IMPORT_INBOX, "exam").length;
    return {
      ...auth,
      pending_owner:
        account === null && this.repo.queueAccounts().length
          ? "本地队列（无需登录即可管理）"
          : undefined,
      learning_count: counts.learn,
      discovery_count: counts.discover,
      learning_failure_count: counts.failure,
      exam_count: counts.exam,
      manual_exam_count: counts.review,
      retriable_failure_count: failures.filter(
        (t) => t.details.recovery === "retry",
      ).length,
      blocked_failure_count: failures.filter(
        (t) => t.details.recovery === "blocked",
      ).length,
      import_count: importedLearning + importedExams,
      ai_configured: aiConfigured(
        readSettings(this.config.settingsPath).openai,
      ),
      recommended:
        auth.credential.status === "missing"
          ? { kind: "login", label: "登录账号" }
          : auth.credential.status !== "valid"
            ? { kind: "login", label: "更新登录凭证" }
            : counts.learn || counts.discover || importedLearning
              ? { kind: "learn", label: "开始课程学习" }
              : counts.exam || importedExams
                ? { kind: "exam", label: "开始考试" }
                : { kind: "links", label: "添加课程链接" },
    };
  }
  localQueues() {
    return {
      currentAccount: this.account,
      accounts: this.repo.queueAccounts().map((account) => ({
        id: account,
        label: this.localAccountLabel(account),
        counts: Object.fromEntries(
          (["learn", "discover", "failure", "exam", "review"] as Queue[]).map(
            (queue) => [queue, this.repo.queue(account, queue).length],
          ),
        ),
        failures: this.repo.queue(account, "failure").map((task) => ({
          url: task.url,
          ...taskIssue(task),
        })),
      })),
    };
  }
  private notify(
    message: string,
    tone: "success" | "error" | "info" = "success",
  ) {
    this.update({ notification: { id: token(), message, tone } });
  }
  update(fields: Record<string, unknown> = {}) {
    Object.assign(this.view, fields);
    this.seq++;
    this.emit("change");
  }
  snapshot() {
    return {
      type: "snapshot",
      instance_id: this.instanceId,
      seq: this.seq,
      operation_id: this.operationId,
      control: {
        kind: this.kind,
        cancellable:
          this.kind !== null &&
          [
            "learn",
            "exam",
            "links",
            "select",
            "references",
            "login",
            "switch_account",
          ].includes(this.kind),
        cancelled: this.abort?.signal.aborted || false,
        stop_after_current: this.stopAfter,
        exit_requested: this.exitRequested,
      },
      ...this.view,
      dashboard: this.dashboard(),
      logs: this.logs,
      dropped_logs: this.droppedLogs,
    };
  }
  connection(connected: boolean) {
    this.connected = connected;
    if (this.disconnectTimer) clearTimeout(this.disconnectTimer);
    if (!connected) {
      this.disconnectTimer = setTimeout(() => {
        if (!this.connected) this.exit();
      }, 10000);
      this.disconnectTimer.unref();
    }
    this.update();
  }
  command(id: unknown, action: string, payload: any) {
    if (typeof id !== "string" || !/^[\w-]{16,100}$/.test(id))
      throw new ProtocolError("请求标识无效", 400);
    const fingerprint = digest(JSON.stringify([action, payload]));
    const old = this.requests.get(id);
    if (old) {
      if (old.fingerprint !== fingerprint)
        throw new ProtocolError("重复请求内容不一致");
      return old.result;
    }
    if (this.requests.size >= 2048)
      throw new ProtocolError("操作记录已满，请重启服务");
    let result: unknown = { ok: true };
    if (action === "operation") {
      if (this.operationId || this.exitRequested)
        throw new ProtocolError("已有任务正在运行");
      if (!OPERATIONS.includes(payload.kind))
        throw new ProtocolError("未知操作", 400);
      if (payload.kind === "settings") {
        try {
          if (
            payload.clearKey !== undefined &&
            typeof payload.clearKey !== "boolean"
          )
            throw new Error("清除密钥选项无效");
          prepareSettings(
            payload.settings,
            this.config.settingsPath,
            payload.clearKey === true,
          );
        } catch (e) {
          throw new ProtocolError(redact(e), 400);
        }
      }
      if (
        payload.auto_submit !== undefined &&
        typeof payload.auto_submit !== "boolean"
      )
        throw new ProtocolError("交卷选项无效", 400);
      if (
        payload.input_text !== undefined &&
        (typeof payload.input_text !== "string" ||
          payload.input_text.length > 50000)
      )
        throw new ProtocolError("链接输入过长或无效", 400);
      if (
        payload.kind === "clear_queues" &&
        !CLEAR_SCOPES.includes(payload.scope)
      )
        throw new ProtocolError("清空范围无效", 400);
      if (
        (payload.kind === "links" || payload.kind === "references") &&
        !payload.input_text?.trim()
      )
        throw new ProtocolError("请先填写课程或考试链接", 400);
      this.operationId = token();
      this.kind = payload.kind;
      this.abort = new AbortController();
      this.stopAfter = false;
      this.update({
        title: OPERATION_TITLES[this.kind!],
        status: "正在准备",
        summary: null,
        notification: null,
      });
      result = { ok: true, operation_id: this.operationId };
      this.active = this.execute(payload.kind, payload)
        .catch((e) => this.logger.log(e, "error"))
        .finally(() => {
          this.operationId = null;
          this.kind = null;
          this.abort = undefined;
          this.promptWaiter = undefined;
          this.update({
            prompt: null,
            progress: null,
            status: this.exitRequested ? "正在退出" : "就绪",
          });
          if (this.exitRequested) this.emit("exit");
        });
    } else if (action === "answer") {
      const p = this.view.prompt;
      if (
        !p ||
        p.id !== payload.prompt_id ||
        p.operation_id !== payload.operation_id ||
        p.version !== payload.version ||
        !this.promptWaiter ||
        this.abort?.signal.aborted ||
        this.exitRequested
      )
        throw new ProtocolError("提示已失效");
      const value = payload.value;
      if (typeof value !== "boolean")
        throw new ProtocolError("回答格式不正确", 400);
      const waiter = this.promptWaiter;
      this.promptWaiter = undefined;
      this.update({ prompt: null });
      waiter.resolve(value);
    } else if (action === "cancel" || action === "stop-after-current") {
      if (!this.operationId || payload.operation_id !== this.operationId)
        throw new ProtocolError("操作已失效");
      if (action === "cancel") {
        this.abort?.abort(new Error("任务已取消，待处理任务已保留"));
        this.promptWaiter?.reject(this.abort?.signal.reason);
        this.promptWaiter = undefined;
        this.update({ prompt: null });
      } else {
        if (this.kind !== "learn")
          throw new ProtocolError("当前任务不支持本课后停止");
        this.stopAfter = true;
      }
      this.update();
    } else if (action === "exit") this.exit();
    else throw new ProtocolError("未知操作", 400);
    this.requests.set(id, { fingerprint, result });
    return result;
  }
  exit() {
    if (this.exitRequested) return;
    this.exitRequested = true;
    if (this.kind === "learn" && !this.view.prompt) this.stopAfter = true;
    else this.abort?.abort(new Error("服务正在关闭"));
    this.promptWaiter?.reject(new Error("服务正在关闭"));
    this.promptWaiter = undefined;
    this.update({ prompt: null });
    if (!this.operationId) this.emit("exit");
  }
  async settled() {
    await this.active;
  }
  async prompt(kind: "confirm", fields: any): Promise<any> {
    this.abort?.signal.throwIfAborted();
    if (this.exitRequested || this.promptWaiter)
      throw new Error("当前无法请求确认");
    const p = {
      id: token(),
      operation_id: this.operationId,
      version: this.seq + 1,
      kind,
      ...fields,
    };
    const result = new Promise((resolve, reject) => {
      this.promptWaiter = { resolve, reject };
    });
    this.update({ prompt: p });
    const value = await result;
    this.abort?.signal.throwIfAborted();
    return value;
  }
  async execute(kind: Operation, payload: any) {
    const scope = this.browser.scope(this.abort!.signal),
      report = (status: string) => {
        this.update({ status: redact(status) });
        this.logger.log(status);
      };
    try {
      if (kind === "settings") {
        const settings = prepareSettings(
          payload.settings,
          this.config.settingsPath,
          payload.clearKey === true,
        );
        await saveSettings(this.config.settingsPath, settings);
        Object.assign(this.config, settings.learning);
        this.notify(
          "设置已保存。AI 和学习参数在下次任务中生效，浏览器路径在重启后生效。",
        );
        return;
      }
      if (kind === "login" || kind === "switch_account") {
        await this.auth.login(scope, report);
        this.notify("账号已登录，登录信息已保存。");
        return;
      }
      if (kind === "clear_credentials") {
        await this.auth.forget();
        this.notify("登录信息已清除，任务与资料已保留。");
        return;
      }
      if (kind === "import_queues") {
        const account =
          this.account === "unbound" ? IMPORT_INBOX : this.account;
        const result = importBundle(this.repo, payload.bundle, account);
        this.update({
          summary: {
            id: token(),
            title: "任务已导入",
            rows: [
              ["新加入队列", result.added],
              ["已有任务（保留本地状态）", result.retained],
              [
                "下一步",
                account === IMPORT_INBOX
                  ? "登录后点击开始学习或开始考试，导入任务将归入该账号"
                  : "点击开始学习或开始考试，平台会核验当前账号的权限与进度",
              ],
            ],
          },
        });
        report(`已导入 ${result.added} 个任务`);
        return;
      }
      if (kind === "export_queues") {
        const { account, selection } = payload;
        const bundle = exportBundle(this.repo, account, selection);
        const id = token(),
          filename = `course-pilot-${selection}-${Date.now()}.json`;
        const path = join(this.config.dataDir, "exports", filename);
        await atomicJson(path, bundle);
        this.exports.set(id, { path, filename });
        this.update({
          summary: {
            id: token(),
            title: "任务已导出",
            rows: [
              ["任务数量", bundle.tasks.length],
              ["文件位置", path],
            ],
            download: { href: `/api/exports/${id}`, filename },
          },
        });
        report(`任务已导出：${path}`);
        return;
      }
      if (kind === "clear_queues") {
        const scope: (typeof CLEAR_SCOPES)[number] = payload.scope;
        const removed = await this.repo.clear(payload.account, scope);
        const message = `已清空 ${removed} 个${scope === "failures" ? "异常" : scope === "exams" ? "考试" : ""}任务，清空前的队列已备份。`;
        report(message);
        this.notify(message);
        return;
      }
      if (kind === "retry_tasks") {
        const selected = this.repo
          .queue(payload.account, "failure")
          .filter((task) => task.details.recovery === payload.recovery);
        for (const task of selected) this.repo.retry(task);
        this.notify(
          `已将 ${selected.length} 个任务重新加入队列，点击“开始学习”继续。`,
        );
        return;
      }
      const lease = await this.auth.lease(scope);
      if (kind === "learn" || kind === "exam") {
        const added = adoptImports(this.repo, lease.account);
        if (added) report(`已将 ${added} 个导入任务归入当前账号`);
      }
      const ctx: DiscoveryContext = {
        scope,
        lease,
        auth: this.auth,
        repo: this.repo,
        report,
      };
      if (kind === "select") {
        const collected = new Set<string>();
        const record = (url: string) => {
          this.auth.assert(lease);
          let candidate;
          try {
            candidate = entry(url, lease.account);
          } catch {
            return;
          }
          if (candidate.kind === "entry" || collected.has(candidate.id)) return;
          const task = this.repo.upsert(candidate.url, lease.account);
          this.repo.enqueue(
            task,
            task.kind === "course"
              ? "learn"
              : task.kind === "exam"
                ? task.manualSubmit
                  ? "review"
                  : "exam"
                : "discover",
          );
          collected.add(task.id);
          report(`已记录 ${collected.size} 个任务`);
        };
        const off = this.browser.browser.cdp.on(
          "Target.targetInfoChanged",
          ({ targetInfo }) => {
            if ([...scope.tabs].some((t) => t.id === targetInfo.targetId)) {
              try {
                record(targetInfo.url);
              } catch (e) {
                this.abort?.abort(e);
                this.promptWaiter?.reject(e);
              }
            }
          },
        );
        try {
          const input = parseLinkInput(
            String(payload.input_text || "").trim() || HOME,
          );
          if (input.rejected)
            report(`已忽略 ${input.rejected} 条无效或不支持的链接`);
          const urls = input.urls;
          if (!urls.length) throw new Error("未找到可用的平台链接");
          for (const url of urls) {
            const tab = await this.auth.navigate(scope, url, lease);
            record(tab.url);
            await tab.bringToFront();
          }
          if (
            !(await this.prompt("confirm", {
              title: "选择课程",
              message:
                "请在打开的平台页中点击所需课程或考试，工作台会记录访问的资源。选择完成后点击确认；取消会结束选择，已记录的任务仍会保留。",
              confirmLabel: "完成选择",
              cancelLabel: "结束选择",
            }))
          )
            return;
          this.update({
            summary: {
              id: token(),
              title: "选择结果",
              rows: [["已记录任务", collected.size]],
            },
          });
        } finally {
          off();
        }
        return;
      }
      if (kind === "links" || kind === "references") {
        const text = payload.input_text;
        const { urls, rejected } = parseLinkInput(String(text));
        if (rejected) report(`已忽略 ${rejected} 条无效或不支持的链接`);
        if (!urls.length) throw new Error("未找到可用的平台链接");
        if (kind === "links") {
          const result = await discover(urls, ctx);
          this.update({
            summary: {
              id: token(),
              title: "解析结果",
              tone: result.issues.length ? "info" : "success",
              rows: [
                ["已发现任务", result.found.length],
                ["待复查入口", result.issues.length],
                ...result.issues.map((issue) => [issue.url, issue.message]),
                ["待继续解析", result.pending.length],
                ["已解析入口", result.expanded],
                ["已跳过占位或内容页", result.ignored],
                ["延伸专题（未展开）", result.related.length],
                ["已忽略无效链接", rejected],
              ],
            },
          });
        } else {
          let saved = 0,
            failed = 0;
          for (const url of urls) {
            this.auth.assert(lease);
            const task = this.repo.upsert(url, lease.account),
              run = this.repo.begin(task, "references");
            try {
              const result = await references(task, ctx, this.config.dataDir);
              saved += result.files.length;
              failed += result.failures.length;
              this.repo.end(
                run,
                result.complete ? "COMPLETED" : "NEEDS_REVIEW",
                result,
              );
            } catch (e) {
              this.repo.end(run, "FAILED", { message: redact(e) });
              throw e;
            }
          }
          this.update({
            summary: {
              id: token(),
              title: "资料下载结果",
              tone: failed ? "info" : "success",
              rows: [
                ["已保存", saved],
                ["失败", failed],
                ["已忽略无效链接", rejected],
              ],
            },
          });
        }
        return;
      }
      const ai =
          kind === "exam"
            ? new AiClient(
                aiSettings(readSettings(this.config.settingsPath).openai),
              )
            : undefined,
        done = new Set<string>();
      let completed = 0,
        review = 0;
      while (true) {
        this.auth.assert(lease);
        if (this.stopAfter) break;
        const queues: Queue[] =
          kind === "learn" ? ["discover", "learn"] : ["exam", "review"];
        const task = queues
          .flatMap((q) => this.repo.queue(lease.account, q))
          .find((t) => !done.has(t.id));
        if (!task) break;
        done.add(task.id);
        if (done.size > 1000)
          throw new Error("本轮任务达到上限，剩余任务已保留");
        const run = this.repo.begin(task, kind);
        try {
          if (kind === "learn")
            await learn(task, ctx, this.config, (value) =>
              this.update({ progress: value }),
            );
          else await runExam(task, ctx, ai!, payload.auto_submit === true);
          const state = this.repo.task(task.id);
          if (state.status === "RUNNING") this.repo.outcome(task.id, "READY");
          this.repo.end(
            run,
            this.repo.task(task.id).status,
            this.repo.task(task.id).details,
          );
          if (state.status === "COMPLETED") completed++;
          else review++;
        } catch (e) {
          const status = lease.signal.aborted ? "INTERRUPTED" : "NEEDS_REVIEW";
          if (lease.signal.aborted || e instanceof AuthenticationError) {
            this.repo.outcome(task.id, status, { message: redact(e) });
            this.repo.end(run, status, { message: redact(e) });
            throw e;
          }
          this.repo.review(
            task,
            e instanceof ReviewRequired ? e.reason : "operation_failed",
            { message: redact(e), recovery: failureRecovery(e) },
            task.kind === "exam",
          );
          this.repo.end(
            run,
            this.repo.task(task.id).status,
            this.repo.task(task.id).details,
          );
          review++;
          report(redact(e));
        }
      }
      this.update({
        summary: {
          id: token(),
          title: kind === "learn" ? "学习结果" : "考试结果",
          tone: review ? "info" : "success",
          rows: [
            ["已完成", completed],
            ["待继续或复查", review],
          ],
        },
      });
    } catch (e) {
      report(redact(e));
      this.update({
        summary: {
          id: token(),
          title: this.abort!.signal.aborted ? "任务已取消" : "操作未完成",
          tone: this.abort!.signal.aborted ? "info" : "error",
          rows: [["状态", redact(e)]],
        },
      });
      this.notify(redact(e), this.abort!.signal.aborted ? "info" : "error");
    } finally {
      await scope.close();
    }
  }
  private localAccountLabel(account: string) {
    return account === IMPORT_INBOX
      ? "待登录导入队列"
      : account === this.account
        ? `账号 ${this.auth.dashboard().account}`
        : `本地账号 ${account.slice(0, 12)}`;
  }
}
