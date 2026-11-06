"use strict";
const $ = id => document.getElementById(id);
let state, csrf = sessionStorage.getItem("csrf") || "", client = sessionStorage.getItem("client") || "";
let generation = null, socket = null, canControl = false, replaced = false, activePrompt = null;
let lastLogSeq = -1, lastSummaryId = "", pending = false;

const RECOMMENDED_LABELS = {
  "仅挂课": "开始课程学习",
  "AI 自动考试": "开始自动考试",
  "手动选择课程 / 录入课程或考试链接": "录入课程或考试链接",
  "切换账号 / 更新登录凭证": "登录或更新凭证"
};
const RECOMMENDED_ACTIONS = {
  "仅挂课": "learn",
  "AI 自动考试": "exam",
  "手动选择课程 / 录入课程或考试链接": "links",
  "切换账号 / 更新登录凭证": "login"
};

const PROMPT_BADGES = {
  multiline: "输入链接",
  choice: "请选择",
  confirm: "请确认",
  pause: "执行完成"
};

const error = message => { $("error").textContent = message || ""; };
const text = (tag, value, className) => {
  const el = document.createElement(tag);
  el.textContent = value ?? "";
  if (className) el.className = className;
  return el;
};

function rows(parent, values) {
  if (!values || !values.length) return;
  const dl = document.createElement("dl");
  for (const [key, value] of values) {
    dl.append(text("dt", key), text("dd", value));
  }
  parent.append(dl);
}

function setConnection(label, mode) {
  const el = $("connection");
  el.textContent = label;
  el.dataset.state = mode;
}

function availability() {
  const busy = !canControl || pending || !!state?.operation_id || state?.control.exit_requested;
  document.querySelectorAll("[data-kind]").forEach(el => {
    el.disabled = busy;
  });
  $("recommended").disabled = busy || !RECOMMENDED_ACTIONS[state?.dashboard?.recommended];
  $("workbench-input").disabled = busy;
  $("clear-workbench").disabled = busy;
  $("auto-submit").disabled = busy;

  const isRunning = !!state?.operation_id;
  $("task-banner").dataset.active = isRunning ? "true" : "false";
  $("task-controls").hidden = !isRunning;
  $("recommended-box").hidden = isRunning;

  $("stop").hidden = state?.control.kind !== "learn";
  $("stop").disabled = !canControl || pending || state?.control.kind !== "learn" || state?.control.stop_after_current;
  $("cancel").disabled = !canControl || pending || !state?.operation_id || state?.control.cancelled;
  $("exit").disabled = !canControl || pending || state?.control.exit_requested;
  $("prompt").querySelectorAll("button,textarea").forEach(el => {
    el.disabled = !canControl || pending;
  });
}

async function send(path, payload) {
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
            "X-Connection-ID": generation
          },
          body
        });
        break;
      } catch (e) {
        if (attempt) throw e;
        await new Promise(r => setTimeout(r, 400));
      }
    }
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
    return true;
  } catch (e) {
    error(e.message || "连接中断，请等待重连后查看操作状态");
    return false;
  } finally {
    pending = false;
    availability();
  }
}

function answer(prompt, value) {
  return send(`/api/prompts/${encodeURIComponent(prompt.id)}/answer`, {
    operation_id: prompt.operation_id,
    version: prompt.version,
    value
  });
}

function renderPrompt(prompt) {
  const host = $("prompt");
  if (!prompt) {
    host.hidden = true;
    host.replaceChildren();
    activePrompt = null;
    return;
  }
  host.hidden = false;
  if (activePrompt === prompt.id) return;
  activePrompt = prompt.id;

  const head = text("div", "", "section-head");
  head.append(text("h2", prompt.title), text("span", PROMPT_BADGES[prompt.kind] || "需确认", "pill"));
  host.replaceChildren(head, text("p", prompt.message));
  rows(host, prompt.rows);

  const actions = text("div", "", "prompt-actions");
  const button = (label, cb, variant = "") => {
    const b = text("button", label, variant);
    b.type = "button";
    b.addEventListener("click", cb);
    actions.append(b);
  };
  if (prompt.kind === "multiline") {
    const input = document.createElement("textarea");
    input.id = "link-input";
    input.placeholder = "每行粘贴一条 HTTP/HTTPS 链接...";
    input.setAttribute("aria-label", prompt.title);
    host.append(input);
    button("继续", () => answer(prompt, input.value), "primary");
  }
  if (prompt.kind === "choice") {
    prompt.options.forEach((label, index) => button(label, () => answer(prompt, index + 1), index === 0 ? "primary" : ""));
  }
  if (prompt.kind === "confirm") {
    button("确认", () => answer(prompt, true), prompt.default ? "primary" : "");
    button("不继续", () => answer(prompt, false), !prompt.default ? "primary" : "");
  }
  if (prompt.kind === "pause") {
    button("返回总览", () => answer(prompt, null), "primary");
  }
  if (prompt.kind !== "pause") {
    button("取消本轮", () => send("/api/control/cancel", { operation_id: prompt.operation_id }), "danger");
  }
  host.append(actions);
}

function render(snapshot) {
  state = snapshot;
  const d = state.dashboard || {};
  const credStatus = d.credential?.status || "missing";
  $("account").textContent = d.account || "未登录";
  $("account-card").dataset.status = credStatus;
  $("credential-tag").textContent = credStatus === "valid" ? "已生效" : credStatus === "expired" ? "已过期" : "待登录";
  $("credential").textContent = credStatus === "valid"
    ? `到期 ${d.credential.expires_on || "未知"}（剩余 ${d.credential.days_left || "—"} 天）`
    : credStatus === "expired"
      ? "登录凭证已过期，请更新登录"
      : d.pending_owner
        ? `待办归属：${d.pending_owner}`
        : "尚未登录账号";

  const rec = d.recommended || "登录或更新凭证";
  $("recommended").textContent = RECOMMENDED_LABELS[rec] || rec;

  const aiBadge = $("ai-badge");
  if (d.ai_configured === true) {
    aiBadge.textContent = "AI 已就绪";
    aiBadge.dataset.tone = "ok";
  } else if (d.ai_configured === false) {
    aiBadge.textContent = "需配置 .env";
    aiBadge.dataset.tone = "warn";
  }

  for (const key of ["learning_count", "learning_failure_count", "exam_count", "manual_exam_count"]) {
    const el = $(key);
    const val = d[key];
    el.textContent = val ?? "—";
    el.closest("article")?.classList.toggle("has-value", Number(val) > 0);
  }

  const retriable = Number(d.retriable_failure_count || 0);
  $("failure-note").textContent = retriable > 0 ? `含 ${retriable} 条可重试异常` : "挂课失败或待重试";

  $("operation-title").textContent = state.operation_id ? state.title : "系统就绪";
  const phaseEl = $("phase");
  if (state.control.exit_requested) {
    phaseEl.textContent = "正在退出";
    phaseEl.dataset.phase = "exit";
  } else if (state.control.cancelled) {
    phaseEl.textContent = "正在取消";
    phaseEl.dataset.phase = "cancel";
  } else if (state.prompt) {
    phaseEl.textContent = "等待确认";
    phaseEl.dataset.phase = "prompt";
  } else if (state.operation_id) {
    phaseEl.textContent = "运行中";
    phaseEl.dataset.phase = "active";
  } else {
    phaseEl.textContent = "空闲";
    phaseEl.dataset.phase = "idle";
  }

  $("status").textContent = !state.operation_id && state.status === "就绪"
    ? "当前无运行任务，可直接启动课程学习、考试或录入链接"
    : state.status;

  $("progress-area").hidden = !state.progress;
  if (state.progress) {
    const p = state.progress;
    $("progress-label").textContent = `${p.description} · ${Math.floor(p.completed)} / ${p.total} 秒`;
    $("progress").max = p.total || 1;
    $("progress").value = p.completed;
  }

  renderPrompt(state.prompt);
  const summaryId = state.summary?.id || "";
  if (summaryId !== lastSummaryId) {
    lastSummaryId = summaryId;
    $("summary").replaceChildren();
    $("summary").hidden = !state.summary;
    if (state.summary) {
      const head = text("div", "", "section-head");
      const closeBtn = text("button", "收起摘要", "btn-sm subtle");
      closeBtn.type = "button";
      closeBtn.onclick = () => {
        $("summary").hidden = true;
      };
      head.append(text("h2", state.summary.title), closeBtn);
      $("summary").append(head);
      rows($("summary"), state.summary.rows);
    }
  }

  const newest = state.logs.at(-1)?.seq || 0;
  if (newest !== lastLogSeq) {
    lastLogSeq = newest;
    const logs = $("logs");
    const pinned = logs.scrollHeight - logs.scrollTop - logs.clientHeight < 45;
    logs.replaceChildren(...state.logs.map(log => text("div", log.message, log.level)));
    if (pinned) logs.scrollTop = logs.scrollHeight;
  }
  $("log-meta").textContent = state.dropped_logs
    ? `最近 400 条 · 已省略较早 ${state.dropped_logs} 条`
    : "最近 400 条";
  availability();
}

function connect() {
  if (replaced) return;
  socket = new WebSocket(location.origin.replace("http:", "ws:") + "/api/events");
  socket.onopen = () => socket.send(JSON.stringify({ client }));
  socket.onmessage = event => {
    const data = JSON.parse(event.data);
    generation = data.generation;
    canControl = !!data.controller;
    setConnection(canControl ? "已连接 · 控制窗口" : "已连接 · 只读", canControl ? "online" : "readonly");
    render(data);
  };
  socket.onclose = event => {
    canControl = false;
    generation = null;
    replaced = event.code === 4001;
    setConnection(
      state?.control.exit_requested ? "程序已退出" : replaced ? "控制已转到重连窗口" : "连接中断 · 正在重连",
      "offline"
    );
    availability();
    if (!replaced && !state?.control.exit_requested) setTimeout(connect, 1500);
  };
  socket.onerror = () => socket.close();
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
        body: JSON.stringify({ bootstrap: boot })
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
      await navigator.locks.request("course-control-tab", { ifAvailable: true }, async lock => {
        if (!lock) client = "";
        connect();
        if (lock) await new Promise(() => {});
      });
    } else {
      client = "";
      connect();
    }
  } catch (e) {
    error(e.message);
    setConnection("无法连接", "offline");
  }
}

async function startOperation(kind) {
    if (!kind || state?.operation_id || state?.control.exit_requested) return;
    const payload = { kind };
    if (kind === "links" || kind === "references") {
      const input = $("workbench-input").value.trim();
      if (input) payload.input_text = input;
    } else if (kind === "exam") {
      payload.auto_submit = $("auto-submit").checked;
    }
    // Keep the draft available for retries and reuse; only explicit clear removes it.
    await send("/api/operations", payload);
}
document.querySelectorAll("[data-kind]").forEach(el =>
  el.addEventListener("click", () => startOperation(el.dataset.kind))
);
$("recommended").onclick = () => startOperation(RECOMMENDED_ACTIONS[state?.dashboard?.recommended]);
$("clear-workbench").onclick = () => {
  $("workbench-input").value = "";
  $("workbench-input").focus();
};
$("cancel").onclick = () => send("/api/control/cancel", { operation_id: state?.operation_id });
$("stop").onclick = () => send("/api/control/stop-after-current", { operation_id: state?.operation_id });
$("exit").onclick = () => send("/api/control/exit", {});
start();
