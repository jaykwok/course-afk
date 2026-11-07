"use strict";
import type { TaskIssue } from "../storage/transfer.ts";
type ElementFor<T extends string> = T extends "auto-submit" | "queue-import"
  ? HTMLInputElement
  : T extends "workbench-input"
    ? HTMLTextAreaElement
    : T extends "progress"
      ? HTMLProgressElement
      : T extends "queue-account"
        ? HTMLSelectElement
        : T extends "prompt" | "queue-dialog" | "settings-dialog"
          ? HTMLDialogElement
          : T extends
                | "recommended"
                | "clear-workbench"
                | "stop"
                | "cancel"
                | "exit"
                | "tasks-open"
                | "settings-open"
                | "settings-save"
                | "queue-submit"
                | "account-card"
            ? HTMLButtonElement
            : HTMLElement;
const $ = <T extends string>(id: T): ElementFor<T> =>
  document.getElementById(id)! as ElementFor<T>;
interface Prompt {
  id: string;
  operation_id: string;
  version: number;
  kind: "confirm";
  title: string;
  message: string;
  rows?: [string, unknown][];
  confirmLabel: string;
  cancelLabel: string;
}
type Tone = "success" | "error" | "info";
interface Snapshot {
  seq: number;
  operation_id: string | null;
  title: string;
  status: string;
  control: {
    exit_requested: boolean;
    cancelled: boolean;
    kind: string;
    cancellable: boolean;
    stop_after_current: boolean;
  };
  dashboard?: Record<string, any>;
  progress?: { description: string; completed: number; total: number };
  prompt: Prompt | null;
  notification: { id: string; tone: Tone; message: string } | null;
  summary?: {
    id: string;
    title: string;
    tone?: Tone;
    rows: [string, unknown][];
    download?: { href: string; filename: string };
  };
  logs: { seq: number; message: string; level: string }[];
  dropped_logs: number;
}
const TITLE = document.title;
let state: Snapshot | undefined,
  csrf = sessionStorage.getItem("csrf") || "",
  client = sessionStorage.getItem("client") || "";
let generation: string | null = null,
  socket: WebSocket | null = null,
  canControl = false,
  replaced = false,
  activePrompt: string | null = null;
let lastLogSeq = -1,
  lastSummaryId = "",
  lastNotificationId = "",
  notificationTimer = 0,
  pending = false;
const promptDialog = $("prompt");
const queueDialog = $("queue-dialog");
const queueAccount = $("queue-account");
const accountMenu = $("account-menu");
type LocalDialogKind =
  "clear_queues" | "clear_credentials" | "export_queues" | "tasks";
interface LocalQueues {
  currentAccount: string;
  accounts: {
    id: string;
    label: string;
    counts: Record<
      "learn" | "discover" | "failure" | "exam" | "review",
      number
    >;
    failures: ({ url: string } & TaskIssue)[];
  }[];
}
let localKind: LocalDialogKind;
let localRequest = 0;
let localQueues: LocalQueues = { currentAccount: "", accounts: [] };
const LOCAL_DIALOGS: Record<
  LocalDialogKind,
  {
    title: string;
    description: string;
    action: string;
    options: [string, string, string][];
  }
> = {
  clear_queues: {
    title: "清空队列",
    action: "清空",
    description:
      "移除本地待办，不影响平台上的学习进度。清空前会自动备份，考试历史与提交记录始终保留。",
    options: [
      [
        "failures",
        "仅清空异常任务",
        "移除可重试和需外部处理的任务，保留待学课程与待解析入口。",
      ],
      [
        "courses",
        "清空全部课程",
        "移除待学课程、待解析入口和异常任务，不影响考试队列。",
      ],
      ["exams", "清空考试队列", "移除待考和待复查的考试。"],
      ["all", "清空全部待办", "移除课程、考试及异常任务。"],
    ],
  },
  clear_credentials: {
    title: "清除登录信息",
    action: "清除登录信息",
    description:
      "清除保存的平台登录信息。任务与已下载的资料会保留，继续学习时需要重新登录。",
    options: [],
  },
  export_queues: {
    title: "导出任务",
    action: "导出 JSON",
    description:
      "选择要导出的任务，可分享给其他人导入学习。文件不包含登录信息或考试答案。",
    options: [
      ["pending", "未学及待完成任务", "包含待学课程、待解析入口和待处理考试。"],
      ["failed", "失败任务", "包含任务链接及失败原因。"],
      ["all", "全部待办与失败任务", "包含以上所有任务。"],
    ],
  },
  tasks: {
    title: "异常任务",
    action: "",
    description: "查看失败原因，选择需要重新排队的任务类型。",
    options: [],
  },
};
const CLEAR_ACTIONS: Record<string, string> = {
  failures: "清空异常任务",
  courses: "清空全部课程",
  exams: "清空考试队列",
  all: "清空全部待办",
};

const error = (message: string) => {
  if (message) $("error-toast").hidden = false;
  $("error").textContent = message || "";
  if (!message) $("error-toast").hidden = true;
};
const text = <T extends keyof HTMLElementTagNameMap>(
  tag: T,
  value: unknown,
  className = "",
): HTMLElementTagNameMap[T] => {
  const el = document.createElement(tag);
  el.textContent = String(value ?? "");
  if (className) el.className = className;
  return el;
};
const icon = (name: string, className = "") => {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", `icon ${className}`.trim());
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#${name}`);
  svg.append(use);
  return svg;
};
const TONE_ICONS: Record<Tone, string> = {
  success: "i-check-circle",
  error: "i-warning-fill",
  info: "i-info-fill",
};
// Seconds as m:ss, or h:mm:ss for long sections.
const clock = (seconds: number) => {
  const total = Math.max(0, Math.floor(seconds)),
    hours = Math.floor(total / 3600),
    minutes = Math.floor((total % 3600) / 60),
    rest = String(total % 60).padStart(2, "0");
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${rest}`
    : `${minutes}:${rest}`;
};

function rows(parent: HTMLElement, values?: [string, unknown][]) {
  if (!values || !values.length) return;
  const dl = document.createElement("dl");
  for (const [key, value] of values) {
    dl.append(text("dt", key), text("dd", value));
  }
  parent.append(dl);
}

function setConnection(label: string, mode: string) {
  const el = $("connection");
  el.textContent = label;
  el.dataset.state = mode;
}

function availability() {
  const busy =
    !canControl ||
    pending ||
    !!state?.operation_id ||
    !!state?.control.exit_requested;
  document.querySelectorAll<HTMLButtonElement>("[data-kind]").forEach((el) => {
    el.disabled = busy;
  });
  $("recommended").disabled = busy;
  $("tasks-open").disabled = !state || !!state.control.exit_requested;
  $("auto-submit").disabled = busy;
  $("settings-open").disabled = busy;
  $("settings-save").disabled = busy;
  modelAvailability();

  const isRunning = !!state?.operation_id;
  $("task-banner").dataset.active = isRunning ? "true" : "false";
  $("task-controls").hidden =
    !isRunning || !!state?.prompt || !state?.control.cancellable;
  $("recommended-box").hidden = isRunning;

  $("stop").hidden = state?.control.kind !== "learn";
  $("stop").disabled =
    !canControl ||
    pending ||
    state?.control.kind !== "learn" ||
    state?.control.stop_after_current;
  $("cancel").disabled =
    !canControl || pending || !state?.operation_id || state?.control.cancelled;
  $("exit").disabled =
    !canControl || pending || !!state?.control.exit_requested;
  promptDialog
    .querySelectorAll<HTMLButtonElement | HTMLTextAreaElement>(
      "button,textarea",
    )
    .forEach((el) => {
      el.disabled = !canControl || pending;
    });
  if (queueDialog.open) updateLocalDialog();
}

async function send(path: string, payload: Record<string, unknown>) {
  if (!canControl || pending) return false;
  const body = JSON.stringify({ ...payload, request_id: crypto.randomUUID() });
  pending = true;
  availability();
  error("");
  try {
    let response;
    // Retransmission always preserves the exact ID and payload.
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        response = await fetch(path, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf,
            "X-Control-Client": client,
            "X-Connection-ID": generation || "",
          },
          body,
        });
        break;
      } catch (e) {
        if (attempt) throw e;
        await new Promise((r) => setTimeout(r, 400));
      }
    }
    if (!response) throw new Error("请求未完成");
    const data = await response.json();
    if (!response.ok)
      throw new Error(data.error || `请求失败 (${response.status})`);
    render(data.snapshot);
    return true;
  } catch (e) {
    error(
      e instanceof Error ? e.message : "连接中断，请等待重连后查看操作状态",
    );
    return false;
  } finally {
    pending = false;
    availability();
  }
}

async function answer(prompt: Prompt, value: unknown) {
  const sent = await send(
    `/api/prompts/${encodeURIComponent(prompt.id)}/answer`,
    {
      operation_id: prompt.operation_id,
      version: prompt.version,
      value,
    },
  );
  // Toasts sit below the modal layer, so failures surface inside the sheet.
  const failure = document.getElementById("prompt-error");
  if (!sent && failure) failure.textContent = $("error").textContent;
}

// Chromium restores a sheet's scroll offset, but each opening starts at the top.
function showSheet(dialog: HTMLDialogElement) {
  dialog.showModal();
  dialog.querySelector(".sheet-body")!.scrollTop = 0;
}

function renderPrompt(prompt: Prompt | null) {
  if (!prompt) {
    promptDialog.close();
    promptDialog.replaceChildren();
    activePrompt = null;
    return;
  }
  if (activePrompt === prompt.id) return;
  activePrompt = prompt.id;

  const body = text("div", "", "sheet-body");
  const title = text("h2", prompt.title);
  title.id = "prompt-title";
  body.append(title, text("p", prompt.message));
  rows(body, prompt.rows);
  const failure = text("p", "", "field-error");
  failure.id = "prompt-error";
  failure.setAttribute("role", "alert");
  body.append(failure);

  const actions = text("div", "", "sheet-foot prompt-actions");
  const button = (label: string, cb: () => unknown, variant = "") => {
    const b = text("button", label, variant);
    b.type = "button";
    b.addEventListener("click", cb);
    actions.append(b);
  };
  button(prompt.cancelLabel, () => answer(prompt, false));
  button(prompt.confirmLabel, () => answer(prompt, true), "primary");
  promptDialog.replaceChildren(body, actions);
  if (accountMenu.matches(":popover-open")) accountMenu.hidePopover();
  promptDialog.showModal();
}
promptDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  void answer(state!.prompt!, false);
});

function selectedAccounts() {
  return localQueues.accounts.filter(
    (account) =>
      queueAccount.value === "all" || account.id === queueAccount.value,
  );
}
function selectedScope() {
  return document.querySelector<HTMLInputElement>(
    'input[name="queue-scope"]:checked',
  )!.value;
}
function updateLocalDialog() {
  const accounts = selectedAccounts();
  const counts = { learn: 0, discover: 0, failure: 0, exam: 0, review: 0 };
  for (const account of accounts)
    for (const key of Object.keys(counts) as (keyof typeof counts)[])
      counts[key] += account.counts[key];
  const failures = accounts.flatMap((account) => account.failures);
  const courses = counts.learn + counts.discover + counts.failure,
    exams = counts.exam + counts.review,
    pendingCount = counts.learn + counts.discover + exams;
  let count = courses + exams;
  if (localKind === "clear_queues")
    count = {
      failures: counts.failure,
      courses,
      exams,
      all: courses + exams,
    }[selectedScope()]!;
  if (localKind === "export_queues")
    count =
      selectedScope() === "pending"
        ? pendingCount
        : selectedScope() === "failed"
          ? counts.failure
          : count;
  $("queue-count").textContent =
    localKind === "clear_credentials"
      ? ""
      : localKind === "tasks"
        ? `${failures.length} 个异常任务${state?.operation_id ? " · 当前任务运行中，结束后可重新排队。" : !canControl ? " · 此窗口只读，请在控制窗口操作。" : ""}`
        : `已选择 ${count} 个任务`;
  const submit = $("queue-submit");
  submit.textContent =
    localKind === "clear_queues"
      ? CLEAR_ACTIONS[selectedScope()]
      : LOCAL_DIALOGS[localKind].action;
  const busy =
    !canControl ||
    pending ||
    !!state?.operation_id ||
    !!state?.control.exit_requested;
  submit.disabled =
    localKind === "tasks" ||
    busy ||
    (localKind !== "clear_credentials" && count === 0);
  queueDialog
    .querySelectorAll<HTMLButtonElement>("button[data-recovery]")
    .forEach((button) => {
      button.disabled =
        busy ||
        !failures.some((task) => task.recovery === button.dataset.recovery);
    });
}
function renderLocalIssues() {
  const failures = selectedAccounts().flatMap((account) =>
    account.failures.map((task) => ({ ...task, account: account.label })),
  );
  $("queue-issue-list").replaceChildren(
    ...failures.map((task) => {
      const item = text("li", "");
      const meta = text("div", "", "issue-meta");
      const tag = text(
        "span",
        task.recovery === "retry" ? "可重试" : "需外部处理",
        "tag",
      );
      // [data-recovery] is reserved for the recovery buttons.
      tag.dataset.tone = task.recovery === "retry" ? "info" : "warn";
      meta.append(tag, text("span", task.account));
      const link = text("a", task.url);
      link.href = task.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      item.append(meta, text("p", task.message));
      if (task.sections?.length) {
        const sections = text("ul", "", "issue-sections");
        sections.append(
          ...task.sections.map((section) => {
            const detail = text("li", "");
            detail.append(
              text(
                "strong",
                `章节 ${section.section} · ${section.recovery === "retry" ? "可重试" : "需外部处理"}`,
              ),
              text("p", section.message),
            );
            return detail;
          }),
        );
        item.append(sections);
      }
      item.append(link);
      return item;
    }),
  );
  $("queue-empty").hidden = failures.length > 0;
}
async function loadLocalQueues(): Promise<LocalQueues> {
  const response = await fetch("/api/queues");
  if (!response.ok) throw new Error("无法读取本地任务，请重试");
  return response.json();
}
function renderLocalAccounts(queues: LocalQueues, selected?: string) {
  localQueues = queues;
  queueAccount.replaceChildren(
    ...localQueues.accounts.map(
      (account) => new Option(account.label, account.id),
    ),
  );
  if (localQueues.accounts.length !== 1)
    queueAccount.add(new Option("全部本地账号", "all"));
  const preferred = selected ?? localQueues.currentAccount;
  queueAccount.value = [...queueAccount.options].some(
    (option) => option.value === preferred,
  )
    ? preferred
    : localQueues.accounts[0]?.id || "all";
  $("queue-account-field").hidden =
    localQueues.accounts.length <= 1 || localKind === "clear_credentials";
}
async function openLocalDialog(kind: LocalDialogKind) {
  const request = ++localRequest;
  try {
    const queues = await loadLocalQueues();
    if (request !== localRequest) return;
    localKind = kind;
    renderLocalAccounts(queues);
    const config = LOCAL_DIALOGS[kind];
    $("queue-title").textContent = config.title;
    $("queue-description").textContent = config.description;
    $("queue-error").textContent = "";
    $("queue-scope").hidden = config.options.length === 0;
    $("queue-options").replaceChildren(
      ...config.options.map(([value, title, description], index) => {
        const label = text("label", "", "choice");
        const input = document.createElement("input");
        input.type = "radio";
        input.name = "queue-scope";
        input.value = value;
        input.checked = index === 0;
        const caption = text("span", "", "choice-text");
        caption.append(text("strong", title), text("small", description));
        label.append(input, caption);
        return label;
      }),
    );
    $("queue-issues").hidden = kind !== "tasks";
    $("queue-retry-actions").hidden = kind !== "tasks";
    $("queue-submit").hidden = kind === "tasks";
    $("queue-submit").className =
      kind === "export_queues" ? "primary" : "destructive";
    $("queue-close").textContent = kind === "tasks" ? "关闭" : "取消";
    $("queue-form").onsubmit = (event) => {
      event.preventDefault();
      const payload: Record<string, unknown> = { kind };
      if (kind === "clear_queues") payload.scope = selectedScope();
      if (kind === "export_queues") payload.selection = selectedScope();
      void submitLocal(payload);
    };
    renderLocalIssues();
    updateLocalDialog();
    showSheet(queueDialog);
  } catch (e) {
    if (request === localRequest) error((e as Error).message);
  }
}
$("queue-form").onchange = () => {
  renderLocalIssues();
  updateLocalDialog();
};
function closeLocalDialog() {
  localRequest++;
  queueDialog.close();
}
$("queue-close").onclick = closeLocalDialog;
queueDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  closeLocalDialog();
});
$("queue-refresh").onclick = async () => {
  const request = ++localRequest;
  try {
    const queues = await loadLocalQueues();
    if (request !== localRequest) return;
    renderLocalAccounts(queues, queueAccount.value);
    renderLocalIssues();
    updateLocalDialog();
  } catch (e) {
    if (request === localRequest)
      $("queue-error").textContent = (e as Error).message;
  }
};
async function submitLocal(payload: Record<string, unknown>) {
  if (
    await send("/api/operations", {
      ...payload,
      account: queueAccount.value === "all" ? null : queueAccount.value,
    })
  )
    closeLocalDialog();
  else $("queue-error").textContent = $("error").textContent;
}
queueDialog
  .querySelectorAll<HTMLButtonElement>("[data-recovery]")
  .forEach((button) => {
    button.onclick = () =>
      submitLocal({ kind: "retry_tasks", recovery: button.dataset.recovery });
  });
$("tasks-open").onclick = () => openLocalDialog("tasks");

function renderAccount(d: Record<string, any>) {
  const status = d.credential?.status || "missing",
    name = d.account || "",
    initial = name ? [...name][0].toUpperCase() : "",
    days = d.credential?.days_left;
  for (const id of ["account-card", "account-summary"])
    $(id).dataset.status = status;
  $("account").textContent = $("menu-account").textContent = name || "未登录";
  $("account-initial").textContent = $("menu-initial").textContent = initial;
  $("account-card").setAttribute("aria-label", `账号：${name || "未登录"}`);
  $("credential-tag").textContent =
    status === "valid"
      ? `凭证剩余 ${days ?? "—"} 天`
      : status === "expired"
        ? "凭证已过期"
        : "待登录";
  $("credential").textContent =
    status === "valid"
      ? `凭证有效，${d.credential.expires_on || "未知日期"}到期（剩余 ${days ?? "—"} 天）`
      : status === "expired"
        ? "登录凭证已过期，请更新登录"
        : d.pending_owner
          ? `待办归属：${d.pending_owner}`
          : "登录后核验账号并保存凭证";
}

const PHASES: Record<string, { label: string; tone: string; glyph: string }> = {
  exit: { label: "正在退出", tone: "danger", glyph: "i-power" },
  cancel: { label: "正在取消", tone: "danger", glyph: "i-xmark" },
  prompt: { label: "等待确认", tone: "warn", glyph: "i-question" },
  active: { label: "运行中", tone: "active", glyph: "i-check" },
};
function setHero(
  phase: string,
  view: { label: string; tone: string; glyph: string },
  title: string,
  status: string,
) {
  const banner = $("task-banner");
  banner.dataset.phase = phase;
  banner.dataset.tone = view.tone;
  $("hero-glyph").setAttribute("href", `#${view.glyph}`);
  $("phase").textContent = view.label;
  $("phase").dataset.phase = phase;
  $("operation-title").textContent = title;
  $("status").textContent = status;
}
function renderHero(snapshot: Snapshot, d: Record<string, any>) {
  const phase = snapshot.control.exit_requested
    ? "exit"
    : snapshot.control.cancelled
      ? "cancel"
      : snapshot.prompt
        ? "prompt"
        : snapshot.operation_id
          ? "active"
          : "idle";
  let view = { label: "空闲", tone: "ready", glyph: "i-check" },
    title = "准备就绪",
    status = snapshot.status;
  if (phase !== "idle") {
    view = PHASES[phase];
    title = snapshot.operation_id ? snapshot.title : view.label;
  } else if (d.credential?.status === "expired") {
    view = { ...view, tone: "warn", glyph: "i-warning" };
    title = "登录凭证已过期";
    status = "请更新登录凭证，再继续学习与考试。本地队列仍可直接管理。";
  } else if (d.credential?.status !== "valid") {
    view = { ...view, tone: "info", glyph: "i-person" };
    title = "欢迎使用";
    status =
      "登录平台账号后即可学习、考试与解析链接。导入、导出和清空本地队列无需登录。";
  } else {
    const parts = [
      [d.learning_count, "门课程待学习"],
      [d.discovery_count, "个入口待解析"],
      [d.exam_count, "场考试待完成"],
      [d.manual_exam_count, "场考试待复查"],
      [d.learning_failure_count, "个异常任务"],
      [d.import_count, "个导入任务待归入"],
    ]
      .filter(([count]) => Number(count) > 0)
      .map(([count, label]) => `${count} ${label}`);
    status = parts.length
      ? parts.join(" · ")
      : "队列为空，粘贴课程或考试链接即可开始。";
  }
  setHero(phase, view, title, status);
  $("recommended").textContent = d.recommended?.label || "等待连接";
  document.title = snapshot.operation_id
    ? `${snapshot.title} · ${view.label} — ${TITLE}`
    : TITLE;

  $("progress-area").hidden = !snapshot.progress;
  if (snapshot.progress) {
    const p = snapshot.progress;
    $("progress-label").textContent = p.description;
    $("progress-time").textContent =
      `${clock(p.completed)} / ${clock(p.total)}`;
    $("progress").max = p.total || 1;
    $("progress").value = p.completed;
  }
}

function armNotification() {
  const host = $("notification");
  clearTimeout(notificationTimer);
  // Errors stay until dismissed; confirmations fade once they can be seen.
  if (document.hidden || host.hidden || host.dataset.tone === "error") return;
  notificationTimer = window.setTimeout(() => {
    host.hidden = true;
  }, 6000);
}
document.addEventListener("visibilitychange", armNotification);

function renderNotification(notification: Snapshot["notification"]) {
  const host = $("notification");
  clearTimeout(notificationTimer);
  host.hidden = !notification;
  host.replaceChildren();
  if (!notification) return;
  host.dataset.tone = notification.tone;
  const close = text("button", "", "toast-close");
  close.type = "button";
  close.setAttribute("aria-label", "关闭提示");
  close.append(icon("i-xmark"));
  close.onclick = () => {
    host.hidden = true;
  };
  host.append(
    icon(TONE_ICONS[notification.tone] || TONE_ICONS.info, "toast-icon"),
    text("p", notification.message),
    close,
  );
  armNotification();
}

function renderSummary(summary: Snapshot["summary"]) {
  const host = $("summary");
  host.replaceChildren();
  host.hidden = !summary;
  if (!summary) return;
  const head = text("div", "", "summary-head");
  const close = text("button", "", "close-button");
  close.type = "button";
  close.setAttribute("aria-label", "收起摘要");
  close.append(icon("i-xmark"));
  close.onclick = () => {
    host.hidden = true;
  };
  const tone = summary.tone || "success";
  const glyph = icon(TONE_ICONS[tone], "card-icon");
  glyph.dataset.tone = tone;
  head.append(glyph, text("h2", summary.title), close);
  host.append(head);
  rows(host, summary.rows);
  if (summary.download) {
    const link = text("a", "", "download-link");
    link.append(icon("i-download"), "下载 JSON 文件");
    link.href = summary.download.href;
    link.download = summary.download.filename;
    host.append(link);
  }
}

function render(snapshot: Snapshot) {
  // Mutation responses and the live connection can arrive in either order.
  if (state && snapshot.seq < state.seq) return;
  state = snapshot;
  const d = state.dashboard || {};
  renderAccount(d);
  renderHero(state, d);

  const aiBadge = $("ai-badge");
  if (d.ai_configured === true) {
    aiBadge.textContent = "AI 已就绪";
    aiBadge.dataset.tone = "ok";
  } else if (d.ai_configured === false) {
    aiBadge.textContent = "未配置 AI";
    aiBadge.dataset.tone = "warn";
  }

  for (const key of [
    "learning_count",
    "discovery_count",
    "learning_failure_count",
    "exam_count",
    "manual_exam_count",
  ]) {
    const el = $(key);
    const val = d[key];
    el.textContent = val ?? "—";
    el.closest(".stat")?.classList.toggle("has-value", Number(val) > 0);
  }

  const retriable = Number(d.retriable_failure_count || 0);
  const blocked = Number(d.blocked_failure_count || 0);
  $("failure-note").textContent = [
    retriable ? `${retriable} 可重试` : "",
    blocked ? `${blocked} 需外部处理` : "",
  ]
    .filter(Boolean)
    .join(" · ");
  $("learning-note").textContent = d.import_count
    ? `另有 ${d.import_count} 个导入任务待归入账号`
    : "";
  $("import-note").textContent = d.import_count
    ? `${d.import_count} 个导入任务将在登录后归入账号`
    : "从分享的 JSON 文件加入队列";

  renderPrompt(state.prompt);
  const notificationId = state.notification?.id || "";
  if (notificationId !== lastNotificationId) {
    lastNotificationId = notificationId;
    renderNotification(state.notification);
  }
  const summaryId = state.summary?.id || "";
  if (summaryId !== lastSummaryId) {
    lastSummaryId = summaryId;
    renderSummary(state.summary);
  }

  const newest = state.logs.at(-1)?.seq || 0;
  if (newest !== lastLogSeq) {
    lastLogSeq = newest;
    const logs = $("logs");
    const pinned = logs.scrollTop < 45;
    const top = logs.getBoundingClientRect().top;
    const anchor = [...logs.children].find(
      (el) => el.getBoundingClientRect().bottom > top,
    ) as HTMLElement | undefined;
    const anchorOffset = anchor ? anchor.getBoundingClientRect().top - top : 0;
    const anchorSeq = anchor?.dataset.seq;
    logs.replaceChildren(
      ...[...state.logs].reverse().map((log) => {
        const row = text("div", log.message, log.level);
        row.dataset.seq = String(log.seq);
        return row;
      }),
    );
    if (pinned) logs.scrollTop = 0;
    else {
      const retained = [...logs.children].find(
        (el) => (el as HTMLElement).dataset.seq === anchorSeq,
      );
      if (retained)
        logs.scrollTop +=
          retained.getBoundingClientRect().top - top - anchorOffset;
    }
  }
  $("log-meta").textContent = state.dropped_logs
    ? `实时更新 · 最新在前 · 已省略较早 ${state.dropped_logs} 条`
    : "实时更新 · 最新在前 · 最近 400 条";
  availability();
}

function connect() {
  if (replaced) return;
  socket = new WebSocket(
    location.origin.replace("http:", "ws:") + "/api/events",
  );
  socket.onopen = () => socket?.send(JSON.stringify({ client }));
  socket.onmessage = (event) => {
    const data = JSON.parse(event.data);
    generation = data.generation;
    canControl = !!data.controller;
    setConnection(
      canControl ? "已连接 · 控制窗口" : "已连接 · 只读",
      canControl ? "online" : "readonly",
    );
    render(data);
  };
  socket.onclose = (event) => {
    canControl = false;
    generation = null;
    replaced = event.code === 4001;
    setConnection(
      state?.control.exit_requested
        ? "程序已退出"
        : replaced
          ? "控制已转到重连窗口"
          : "连接中断 · 正在重连",
      "offline",
    );
    availability();
    if (!replaced && !state?.control.exit_requested) setTimeout(connect, 1500);
  };
  socket.onerror = () => socket?.close();
}

async function start() {
  availability();
  try {
    const boot = new URLSearchParams(location.hash.slice(1)).get("bootstrap");
    history.replaceState(null, "", location.pathname);
    if (boot) {
      const r = await fetch("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ bootstrap: boot }),
      });
      if (!r.ok) throw new Error("启动凭据已使用或失效，请重新启动入口");
      const s = await r.json();
      client = s.client;
      csrf = s.csrf;
      sessionStorage.setItem("client", client);
      sessionStorage.setItem("csrf", csrf);
    }
    const response = await fetch("/api/state");
    if (!response.ok) throw new Error("请从项目启动脚本打开管理窗口");
    const initial = await response.json();
    csrf = initial.csrf;
    render(initial);
    // A duplicated tab can inherit sessionStorage. The origin-wide tab lock
    // keeps it read-only while refresh naturally releases and reacquires it.
    if (client && navigator.locks) {
      await navigator.locks.request(
        "course-control-tab",
        { ifAvailable: true },
        async (lock) => {
          if (!lock) client = "";
          connect();
          if (lock) await new Promise(() => {});
        },
      );
    } else {
      client = "";
      connect();
    }
  } catch (e) {
    const message = e instanceof Error ? e.message : "无法连接";
    error(message);
    setConnection("无法连接", "offline");
    setHero(
      "offline",
      { label: "未连接", tone: "danger", glyph: "i-warning" },
      "无法连接工作台",
      message,
    );
  }
}

async function startOperation(kind?: string) {
  if (!kind || state?.operation_id || state?.control.exit_requested) return;
  if (accountMenu.matches(":popover-open")) accountMenu.hidePopover();
  if (Object.hasOwn(LOCAL_DIALOGS, kind)) {
    await openLocalDialog(kind as LocalDialogKind);
    return;
  }
  if (kind === "import_queues") {
    $("queue-import").click();
    return;
  }
  const payload: Record<string, unknown> = { kind };
  if (kind === "links" || kind === "references" || kind === "select") {
    const input = $("workbench-input").value.trim();
    if (!input && kind !== "select") {
      $("link-error").textContent =
        "请先填写课程或考试链接，也可以直接粘贴包含链接的通知。";
      $("workbench-input").setAttribute("aria-invalid", "true");
      $("workbench-input").focus();
      return;
    }
    if (input) payload.input_text = input;
  } else if (kind === "exam") {
    payload.auto_submit = $("auto-submit").checked;
  }
  // Keep the draft available for retries and reuse; only explicit clear removes it.
  await send("/api/operations", payload);
}

// The menu hangs below its button; the popover itself provides light dismiss.
accountMenu.addEventListener("beforetoggle", (event) => {
  $("account-card").setAttribute(
    "aria-expanded",
    String(event.newState === "open"),
  );
  if (event.newState !== "open") return;
  const anchor = $("account-card").getBoundingClientRect(),
    width = Math.min(280, innerWidth - 24);
  accountMenu.style.top = `${Math.round(anchor.bottom + 8)}px`;
  accountMenu.style.left = `${Math.round(
    Math.max(12, Math.min(anchor.right - width, innerWidth - width - 12)),
  )}px`;
});
addEventListener("resize", () => {
  if (accountMenu.matches(":popover-open")) accountMenu.hidePopover();
});

$("queue-import").onchange = async () => {
  const input = $("queue-import");
  const file = input.files?.[0];
  if (!file) return;
  try {
    const bundle = JSON.parse(await file.text());
    await send("/api/import", { bundle });
  } catch (e) {
    error(e instanceof Error ? e.message : "无法读取 JSON 文件");
  } finally {
    input.value = "";
  }
};
document
  .querySelectorAll<HTMLButtonElement>("[data-kind]")
  .forEach((el) =>
    el.addEventListener("click", () => startOperation(el.dataset.kind)),
  );
$("recommended").onclick = () => {
  const kind = state?.dashboard?.recommended.kind;
  // An empty queue only needs the composer, not an error about missing links.
  if (kind === "links" && !$("workbench-input").value.trim()) {
    $("workbench-input").focus();
    return;
  }
  void startOperation(kind);
};
$("workbench-input").oninput = () => {
  $("workbench-input").removeAttribute("aria-invalid");
  $("link-error").textContent = "";
};
$("clear-workbench").onclick = () => {
  $("workbench-input").value = "";
  $("workbench-input").removeAttribute("aria-invalid");
  $("link-error").textContent = "";
  $("workbench-input").focus();
};
$("error-close").onclick = () => error("");
$("cancel").onclick = () =>
  send("/api/control/cancel", { operation_id: state?.operation_id });
$("stop").onclick = () =>
  send("/api/control/stop-after-current", {
    operation_id: state?.operation_id,
  });
$("exit").onclick = () => {
  accountMenu.hidePopover();
  void send("/api/control/exit", {});
};
const settingsDialog = $("settings-dialog");
const settingsFields = [
  ...document.querySelectorAll<HTMLInputElement | HTMLSelectElement>(
    "[data-setting]",
  ),
];
const settingsKeyClear = $("settings-clear-key") as HTMLInputElement;
const settingsBaseURL = $("settings-base-url") as HTMLInputElement;
const settingsApiKey = $("settings-api-key") as HTMLInputElement;
const modelInput = $("settings-model") as HTMLInputElement;
const modelsFetch = $("models-fetch") as HTMLButtonElement;
const modelsToggle = $("models-toggle") as HTMLButtonElement;
let models: string[] = [],
  matchingModels: string[] = [],
  modelsLoaded = false,
  activeModel = -1,
  modelRequest: AbortController | null = null;

function modelAvailability() {
  modelsFetch.disabled =
    !!modelRequest || !canControl || pending || !!state?.control.exit_requested;
  modelsFetch.textContent = modelRequest ? "获取中…" : "获取模型";
  modelsToggle.disabled = !modelsLoaded;
}
function modelStatus(message: string, failed = false) {
  $("models-status").textContent = message;
  $("models-status").dataset.tone = failed ? "error" : "info";
}
function closeModels() {
  $("model-popup").hidden = true;
  modelInput.setAttribute("aria-expanded", "false");
  modelInput.removeAttribute("aria-activedescendant");
  modelsToggle.setAttribute("aria-expanded", "false");
  modelsToggle.setAttribute("aria-label", "展开模型列表");
  activeModel = -1;
}
function chooseModel(model: string) {
  modelInput.value = model;
  modelInput.focus();
  closeModels();
}
function showModels(query = "") {
  matchingModels = models.filter((model) =>
    model.toLowerCase().includes(query.trim().toLowerCase()),
  );
  activeModel = -1;
  modelInput.removeAttribute("aria-activedescendant");
  $("model-options").replaceChildren(
    ...matchingModels.map((model, index) => {
      const option = text("div", model);
      option.id = `model-option-${index}`;
      option.setAttribute("role", "option");
      option.setAttribute("aria-selected", "false");
      option.onpointerdown = (event) => event.preventDefault();
      option.onclick = () => chooseModel(model);
      return option;
    }),
  );
  $("models-empty").hidden = matchingModels.length > 0;
  $("models-empty").textContent = models.length
    ? "没有匹配的模型，可继续手动填写。"
    : "接口未返回可用模型，可手动填写模型名称。";
  $("model-popup").hidden = false;
  $("model-popup").scrollIntoView({ block: "nearest" });
  modelInput.setAttribute("aria-expanded", "true");
  modelsToggle.setAttribute("aria-expanded", "true");
  modelsToggle.setAttribute("aria-label", "收起模型列表");
}
function resetModels() {
  modelRequest?.abort();
  modelRequest = null;
  models = [];
  modelsLoaded = false;
  closeModels();
  modelStatus("填写接口地址和 API Key 后可获取模型，也可直接手动填写。");
  modelAvailability();
}
modelInput.oninput = modelInput.onfocus = () => {
  if (modelsLoaded) showModels(modelInput.value);
};
modelInput.onkeydown = (event) => {
  if (event.isComposing) return;
  if ((event.key === "ArrowDown" || event.key === "ArrowUp") && modelsLoaded) {
    event.preventDefault();
    if ($("model-popup").hidden) showModels();
    if (!matchingModels.length) return;
    const direction = event.key === "ArrowDown" ? 1 : -1;
    activeModel =
      activeModel < 0
        ? direction === 1
          ? 0
          : matchingModels.length - 1
        : (activeModel + direction + matchingModels.length) %
          matchingModels.length;
    const options = $("model-options").children;
    for (let i = 0; i < options.length; i++)
      options[i].setAttribute("aria-selected", String(i === activeModel));
    modelInput.setAttribute("aria-activedescendant", options[activeModel].id);
    options[activeModel].scrollIntoView({ block: "nearest" });
  } else if (event.key === "Enter" && !$("model-popup").hidden) {
    event.preventDefault();
    if (activeModel >= 0) chooseModel(matchingModels[activeModel]);
    else if (matchingModels.length === 1) chooseModel(matchingModels[0]);
    else closeModels();
  } else if (event.key === "Escape" && !$("model-popup").hidden) {
    event.preventDefault();
    event.stopPropagation();
    closeModels();
  }
};
modelsToggle.onpointerdown = (event) => event.preventDefault();
modelsToggle.onclick = () => {
  const wasOpen = !$("model-popup").hidden;
  modelInput.focus();
  if (wasOpen) closeModels();
  else showModels();
};
$("model-picker").addEventListener("focusout", (event) => {
  if (!$("model-picker").contains(event.relatedTarget as Node | null))
    closeModels();
});
settingsDialog.addEventListener("pointerdown", (event) => {
  if (!$("model-picker").contains(event.target as Node)) closeModels();
});
settingsDialog.addEventListener("cancel", (event) => {
  if (!$("model-popup").hidden) {
    event.preventDefault();
    closeModels();
  }
});
settingsBaseURL.oninput =
  settingsApiKey.oninput =
  settingsKeyClear.onchange =
    resetModels;
modelsFetch.onclick = async () => {
  if (!settingsBaseURL.reportValidity()) return;
  resetModels();
  const request = new AbortController();
  modelRequest = request;
  modelAvailability();
  modelStatus("正在获取模型列表…");
  try {
    const response = await fetch("/api/models", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf,
        "X-Control-Client": client,
        "X-Connection-ID": generation || "",
      },
      body: JSON.stringify({
        baseURL: settingsBaseURL.value.trim(),
        apiKey: settingsApiKey.value.trim(),
        clearKey: settingsKeyClear.checked,
      }),
      signal: request.signal,
    });
    const data = await response.json();
    if (request.signal.aborted) return;
    if (!response.ok) throw new Error(data.error);
    models = data.models;
    modelsLoaded = true;
    modelStatus(
      models.length
        ? `已获取 ${models.length} 个模型。输入可筛选，也可填写其他模型名称。`
        : "接口未返回可用模型，可手动填写模型名称。",
    );
    modelInput.focus();
    showModels();
  } catch (error) {
    if (!request.signal.aborted)
      modelStatus(
        error instanceof Error ? error.message : "获取模型失败，请重试。",
        true,
      );
  } finally {
    if (modelRequest === request) {
      modelRequest = null;
      modelAvailability();
    }
  }
};
$("settings-open").onclick = async () => {
  try {
    const response = await fetch("/api/settings");
    if (!response.ok) throw new Error("无法读取设置");
    const settings = await response.json();
    for (const field of settingsFields) {
      const [group, key] = field.dataset.setting!.split(".");
      field.value = String(settings[group][key]);
      if (key === "apiKey")
        field.setAttribute(
          "placeholder",
          settings.openai.keyConfigured ? "已配置，留空保持不变" : "未配置",
        );
    }
    settingsKeyClear.checked = false;
    resetModels();
    $("settings-error").textContent = "";
    showSheet(settingsDialog);
  } catch (e) {
    error(e instanceof Error ? e.message : "无法读取设置");
  }
};
$("settings-close").onclick = () => settingsDialog.close();
settingsDialog.addEventListener("close", () => {
  resetModels();
  settingsApiKey.value = "";
});
$("settings-form").onsubmit = async (event) => {
  event.preventDefault();
  const settings: Record<string, any> = {
    version: 1,
    openai: {},
    learning: {},
    browser: {},
  };
  for (const field of settingsFields) {
    const [group, key] = field.dataset.setting!.split(".");
    settings[group][key] =
      field.type === "number" ? Number(field.value) : field.value.trim();
  }
  if (
    await send("/api/operations", {
      kind: "settings",
      settings,
      clearKey: settingsKeyClear.checked,
    })
  )
    settingsDialog.close();
  else
    $("settings-error").textContent =
      $("error").textContent || "设置未保存，请重试";
};
start();
