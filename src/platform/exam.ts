import type { Tab } from "nodriver-ts";
import { PLATFORM } from "../config.ts";
import { data, ReviewRequired, sleep, until } from "../utils.ts";
import { PlatformHttp } from "../browser/http.ts";
import type { Task } from "../storage/repository.ts";
import type { DiscoveryContext } from "./discovery.ts";
import { AiClient, validAnswers, type Question } from "../ai/client.ts";
import { clickText, inspectAccess } from "./dom.ts";

function number(...values: unknown[]): number | undefined {
  for (const v of values)
    if (
      v !== null &&
      v !== undefined &&
      v !== "" &&
      typeof v !== "boolean" &&
      Number.isFinite(Number(v)) &&
      Number(v) >= 0
    )
      return Number(v);
}
export function examState(raw: unknown) {
  const d = data(raw),
    record = d.examRecord || {},
    regist = d.examRegist || {};
  const allowed = number(d.allowExamTimes),
    used = number(d.examedTimes, regist.examTimes),
    score = number(regist.topScore, record.score),
    pass = number(d.passScore, d.standardScore),
    total = number(d.paperClass?.totalScore);
  const remaining =
    allowed !== undefined && allowed > 0 && used !== undefined
      ? Math.max(0, allowed - used)
      : undefined;
  const passed =
    score !== undefined &&
    pass !== undefined &&
    (total !== undefined && total > 100 && pass <= 100 ? score / 100 : score) >=
      pass;
  const submitted = Number(record.isFinished) === 1;
  const resume =
    !!record.id &&
    !!record.startTime &&
    [1, 2, 3, 4].includes(Number(record.status)) &&
    !submitted &&
    !record.submitTime &&
    Number(record.isReset) !== 1;
  return {
    passed,
    submitted,
    pending: submitted && !passed && number(record.score) === undefined,
    allowed,
    used,
    remaining,
    recordId: String(record.id || ""),
    resume,
  };
}
export function entryProblem(raw: unknown, id: string): string | undefined {
  const d = data(raw),
    record = d.examRecord,
    state = examState(d),
    now = number(record?.currentTime) ?? Date.now();
  if (
    d.id !== id ||
    !record ||
    typeof record !== "object" ||
    !d.paperClass ||
    !Object.keys(d.paperClass).length
  )
    return "考试信息尚未完整确认";
  if (Number(d.isSetAccessSecret) === 1 || Number(d.isSetPersonalCode) === 1)
    return "考试需要密码或个人码，请在平台进入";
  if (
    Number(d.clientType) === 2 ||
    [1, 7].includes(Number(d.status)) ||
    ![1, 3, 4].includes(Number(d.type))
  )
    return "考试当前不可自动进入";
  if (
    Number(d.signUp?.status) === 3 ||
    (Number(d.needApplicant) === 1 &&
      Number(d.examedTimes) === 0 &&
      !record.id &&
      Number(d.signUp?.status) !== 2)
  )
    return "考试报名尚未通过";
  if (number(d.startTime) !== undefined && now < Number(d.startTime))
    return "考试尚未开始";
  if (
    number(d.endTime) !== undefined &&
    now > Number(d.endTime) &&
    !state.resume
  )
    return "考试已结束";
  if (Number(record.status) === 5) return "平台正在处理试卷";
  if (state.remaining === 0 && !state.resume) return "没有剩余考试次数";
}
export async function readExamState(http: PlatformHttp, id: string) {
  return http.json(`/api/v1/exam/exam/front/user-record/${id}`);
}
export function newSubmission(
  state: ReturnType<typeof examState>,
  baseline: { recordId?: string; used?: number; submitted?: boolean },
) {
  return (
    state.submitted &&
    ((!!state.recordId && state.recordId !== baseline.recordId) ||
      (!!state.recordId &&
        state.recordId === baseline.recordId &&
        baseline.submitted === false) ||
      (state.used !== undefined &&
        baseline.used !== undefined &&
        state.used > baseline.used))
  );
}
export async function extractQuestions(
  tab: Tab,
  signal: AbortSignal,
): Promise<{ single: boolean; questions: Question[] }> {
  return tab.evaluate(
    () => {
      const visible = (e: Element) => e.getBoundingClientRect().height > 0;
      const single = [...document.querySelectorAll(".single-btns")].some(
        visible,
      );
      const roots: (Element | Document)[] = single
        ? [document]
        : [...document.querySelectorAll(".question-type-item")];
      const questions = roots.map((root) => {
        const types = [...root.querySelectorAll(".o-score")]
          .map((e) => e.textContent || "")
          .join(" ");
        let type = "unknown";
        for (const [label, value] of [
          ["不定项", "multiple"],
          ["多选", "multiple"],
          ["单选", "single"],
          ["判断", "judge"],
          ["排序", "ordering"],
          ["阅读理解", "reading"],
          ["填空", "fill_blank"],
        ])
          if (types.includes(label)) {
            type = value;
            break;
          }
        const text = (
          root.querySelector<HTMLElement>(
            ".stem-content-main,.single-title .rich-text-style",
          )?.innerText || ""
        ).trim();
        const id = single
          ? text
          : (root as Element).getAttribute("data-dynamic-key") || "";
        const selector = single
          ? ""
          : `[data-dynamic-key=${JSON.stringify(id)}]`;
        const selectors =
          type === "judge"
            ? [".preview-list dd .pointer", ".option-item"]
            : [
                ".preview-list dd",
                ".preview-list .option-item",
                ".option-item",
                ".answer-list .option-item",
                ".answer-item",
              ];
        const optionSelector =
          selectors.find((s) => root.querySelectorAll(s).length >= 2) ||
          selectors[0];
        const options = [
          ...root.querySelectorAll<HTMLElement>(optionSelector),
        ].map((e, i) => {
          const raw = (
            e.querySelector(".option-num,.label,.option-label,.num")
              ?.textContent || ""
          ).trim();
          const content = (
            e.querySelector<HTMLElement>(
              ".answer-options,.option-content,.content,.answer-text,.label-text,.text",
            )?.innerText ||
            e.innerText ||
            ""
          ).trim();
          const label =
            type === "judge"
              ? /正确|对|√|true/i.test(content)
                ? "T"
                : /错误|错|×|false/i.test(content)
                  ? "F"
                  : ""
              : raw.match(/^([A-Z])(?:[.、．:：)]|$)/)?.[1] ||
                String.fromCharCode(65 + i);
          return {
            label,
            text: content.replace(new RegExp(`^${label}[.、．:：)]?\\s*`), ""),
          };
        });
        const media = !!root.querySelector(
          single
            ? ".single-title img,.single-title canvas,.single-title audio,.single-title video,.single-title iframe,.preview-list img,.option-item img"
            : "img,canvas,audio,video,iframe,object",
        );
        return { id, type, text, options, media, selector, optionSelector };
      });
      return { single, questions };
    },
    undefined,
    { timeout: 10000, signal },
  );
}
export async function selectedAnswers(
  tab: Tab,
  q: Question,
  signal: AbortSignal,
): Promise<string[] | null> {
  return tab.evaluate(
    ({ q }) => {
      const root = q.selector ? document.querySelector(q.selector) : document;
      if (!root) return null;
      const title = (
        root.querySelector<HTMLElement>(
          ".stem-content-main,.single-title .rich-text-style",
        )?.innerText || ""
      ).trim();
      if (title !== q.text) return null;
      if (q.type === "ordering") {
        const input =
          root.querySelector<HTMLInputElement>(".answer-input-shot");
        return input ? [...input.value.trim()] : null;
      }
      const options = [...root.querySelectorAll(q.optionSelector)];
      if (options.length !== q.options.length) return null;
      const result: string[] = [];
      options.forEach((e, index) => {
        const parent =
          e.closest(".preview-list dd,.option-item,.answer-item") || e;
        const input = parent.querySelector<HTMLInputElement>(
          "input[type=checkbox],input[type=radio]",
        );
        const aria =
          e.getAttribute("aria-checked") ??
          parent.getAttribute("aria-checked") ??
          parent.querySelector("[aria-checked]")?.getAttribute("aria-checked");
        const checked = input
          ? input.checked
          : aria === "true" ||
            (aria !== "false" &&
              (parent.matches(
                ".selected,.checked,.active,.is-checked,.ant-checkbox-checked,.ant-radio-checked",
              ) ||
                !!parent.querySelector(
                  ".selected,.checked,.active,.is-checked,.ant-checkbox-checked,.ant-radio-checked",
                )));
        if (checked) result.push(q.options[index].label);
      });
      return result;
    },
    { q },
    { timeout: 5000, signal },
  );
}
export async function applyAnswers(
  tab: Tab,
  q: Question,
  answers: string[],
  ctx: DiscoveryContext,
) {
  if (!validAnswers(q, answers)) return false;
  const before = await selectedAnswers(tab, q, ctx.lease.signal);
  if (before === null) return false;
  const prefix = q.selector ? `${q.selector} ` : "";
  if (q.type === "ordering") {
    ctx.auth.assert(ctx.lease);
    const input = await tab.query(`${prefix}.answer-input-shot`, {
      signal: ctx.lease.signal,
      timeout: 5000,
    });
    if (!input) return false;
    await input.fill(answers.join(""), {
      signal: ctx.lease.signal,
      timeout: 5000,
      guard: () => ctx.auth.assert(ctx.lease),
    });
  } else {
    const indices =
      q.type === "multiple"
        ? q.options.map((_, i) => i)
        : [q.options.findIndex((o) => o.label === answers[0])];
    for (const index of indices) {
      ctx.auth.assert(ctx.lease);
      const current = await selectedAnswers(tab, q, ctx.lease.signal);
      if (!current) return false;
      if (
        current.includes(q.options[index].label) ===
        answers.includes(q.options[index].label)
      )
        continue;
      const elements = await tab.queryAll(`${prefix}${q.optionSelector}`, {
        signal: ctx.lease.signal,
        timeout: 5000,
      });
      if (elements.length !== q.options.length) return false;
      const control =
        (await elements[index].query(
          'input[type="checkbox"],input[type="radio"]',
          { visible: true, timeout: 3000, signal: ctx.lease.signal },
        )) || elements[index];
      ctx.auth.assert(ctx.lease);
      await control.click({
        signal: ctx.lease.signal,
        timeout: 5000,
        guard: () => ctx.auth.assert(ctx.lease),
      });
      await sleep(300, ctx.lease.signal);
    }
  }
  const result = await selectedAnswers(tab, q, ctx.lease.signal);
  return (
    !!result &&
    (q.type === "ordering"
      ? result.join("") === answers.join("")
      : result.sort().join("") === [...answers].sort().join(""))
  );
}
export async function runExam(
  task: Task,
  ctx: DiscoveryContext,
  ai: AiClient,
  autoSubmit: boolean,
) {
  // Detail is read-only; never open the answer route before eligibility and persistent intent.
  const tab = await ctx.scope.open(
    `${PLATFORM}/#/exam/exam-detail/${task.businessId}`,
  );
  const http = new PlatformHttp(tab, ctx.auth, ctx.lease);
  let keep = false;
  try {
    const raw = await readExamState(http, task.businessId),
      before = examState(raw),
      pending = ctx.repo.pending(task, "submit");
    if (before.passed) {
      for (const i of pending)
        ctx.repo.confirm(i.id, { passed: true, recordId: before.recordId });
      ctx.repo.complete(task, { source: "exam-user-record", passed: true });
      return;
    }
    if (
      pending.length &&
      pending.every((i) => newSubmission(before, i.baseline))
    ) {
      for (const i of pending)
        ctx.repo.confirm(i.id, { recordId: before.recordId, submitted: true });
      if (!before.pending)
        ctx.repo.saveHistory(task, {
          failedModels: pending.map((i) => i.baseline.model).filter(Boolean),
        });
      ctx.repo.review(
        task,
        before.pending ? "pending_grading" : "exam_not_passed",
        { message: before.pending ? "等待平台评卷" : "上次考试未通过，请复查" },
        true,
      );
      return;
    }
    if (before.pending)
      throw new ReviewRequired("pending_grading", "试卷已提交，等待评卷");
    const retained = [...ctx.scope.runtime.retained].find(
      (t) => !t.closed && t.url.includes(task.businessId),
    );
    if (retained) {
      await retained.bringToFront();
      ctx.repo.review(
        task,
        "manual_submit",
        { message: "试卷已在浏览器中打开，请完成核对" },
        true,
      );
      return;
    }
    if (pending.length && !before.resume)
      throw new ReviewRequired(
        "submission_unverified",
        "上次考试结果尚未确认，请在平台复查",
      );
    const problem = entryProblem(raw, task.businessId);
    if (problem) throw new ReviewRequired("entry_unavailable", problem);
    if (
      !before.resume &&
      before.allowed !== 0 &&
      (before.remaining === undefined || before.remaining < 1)
    )
      throw new ReviewRequired("attempt_limit", "考试次数尚未确认或已用尽");
    if (ctx.repo.history(task).failedModels?.includes(ai.fingerprint))
      throw new ReviewRequired(
        "ai_failed_model",
        "此模型配置已有未通过记录，请更换模型后重试",
      );
    const validate = data(
      await http.json("/api/v1/exam/exam/front/valiate-exam", {
        method: "POST",
        body: new URLSearchParams({ examId: task.businessId }).toString(),
        headers: {
          "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        },
      }),
    );
    if (validate.status !== 1)
      throw new ReviewRequired("entry_unavailable", "平台未允许进入考试");
    const continuing = before.resume || pending.length > 0;
    const intent =
      pending[0]?.id ||
      ctx.repo.intent(task, "submit", {
        recordId: before.recordId,
        used: before.used,
        submitted: before.submitted,
        model: ai.fingerprint,
      });
    ctx.auth.assert(ctx.lease);
    await tab.goto(task.url, {
      waitUntil: "domcontentloaded",
      timeout: 30000,
      signal: ctx.lease.signal,
    });
    keep = true;
    ctx.scope.retain(tab);
    await tab.bringToFront();
    await until(
      async () => {
        const r = await extractQuestions(tab, ctx.lease.signal);
        return r.questions.length && r.questions.every((q) => q.text && q.id)
          ? r
          : false;
      },
      30000,
      ctx.lease.signal,
      500,
    );
    let snapshot = await extractQuestions(tab, ctx.lease.signal);
    let permitted =
      autoSubmit &&
      !task.manualSubmit &&
      !continuing &&
      (before.allowed === 0 || (before.remaining ?? 0) > 1) &&
      !snapshot.single;
    const expected = new Map<string, { q: Question; answers: string[] }>(),
      seen = new Set<string>();
    for (let round = 0; round < 500; round++) {
      if (
        !snapshot.questions.length ||
        new Set(snapshot.questions.map((q) => q.id)).size !==
          snapshot.questions.length
      )
        throw new ReviewRequired(
          "question_parse_error",
          "无法确认整卷题目清单",
        );
      for (const q of snapshot.questions) {
        ctx.auth.assert(ctx.lease);
        await inspectAccess(tab, ctx.lease.signal);
        if (seen.has(q.id))
          throw new ReviewRequired("question_parse_error", "题目导航未推进");
        seen.add(q.id);
        ctx.report(`正在处理第 ${seen.size} 题`);
        const existing = await selectedAnswers(tab, q, ctx.lease.signal);
        if (existing === null || existing.length) {
          permitted = false;
          continue;
        }
        let answers: string[] = [];
        try {
          answers = await ai.answer(q, ctx.lease.signal);
        } catch (e) {
          if (ctx.lease.signal.aborted) throw e;
          permitted = false;
          ctx.report("AI 未返回可执行答案，该题保留人工核对");
        }
        if (!(await applyAnswers(tab, q, answers, ctx))) {
          permitted = false;
          continue;
        }
        expected.set(q.id, { q, answers });
      }
      if (!snapshot.single) break;
      permitted = false;
      const next = await tab.query(
        ".single-btn-next:not(.next-disabled):not([disabled])",
        { timeout: 3000, signal: ctx.lease.signal },
      );
      if (!next) break;
      ctx.auth.assert(ctx.lease);
      await next.click({
        signal: ctx.lease.signal,
        timeout: 5000,
        guard: () => ctx.auth.assert(ctx.lease),
      });
      await sleep(700, ctx.lease.signal);
      snapshot = await extractQuestions(tab, ctx.lease.signal);
      if (round === 499)
        throw new ReviewRequired(
          "question_parse_error",
          "题数达到上限，请人工核对",
        );
    }
    if (permitted) {
      const final = await extractQuestions(tab, ctx.lease.signal);
      if (final.single || final.questions.length !== expected.size)
        permitted = false;
      for (const q of final.questions) {
        const e = expected.get(q.id);
        const selected = await selectedAnswers(tab, q, ctx.lease.signal);
        if (
          !e ||
          q.text !== e.q.text ||
          JSON.stringify(q.options) !== JSON.stringify(e.q.options) ||
          !selected ||
          selected.sort().join("") !== [...e.answers].sort().join("")
        )
          permitted = false;
      }
    }
    if (!permitted) {
      ctx.repo.review(
        task,
        "manual_submit",
        { message: "答题完成，请在保留的试卷页核对并手动交卷" },
        true,
      );
      return;
    }
    const verify = () => {
      ctx.auth.assert(ctx.lease);
      if (
        ctx.repo.task(task.id).manualSubmit ||
        ctx.repo
          .pending(task, "submit")
          .map((i) => i.id)
          .join("") !== intent
      )
        throw new Error("交卷授权已变化");
    };
    verify();
    if (
      !(await clickText(tab, ["我要交卷"], ctx.lease.signal, undefined, verify))
    )
      throw new ReviewRequired("submission_unverified", "未找到交卷按钮");
    await sleep(1000, ctx.lease.signal);
    verify();
    if (
      !(await clickText(
        tab,
        ["确定"],
        ctx.lease.signal,
        ".el-dialog button,.ant-modal button,.dialog button,button",
        verify,
      ))
    )
      throw new ReviewRequired("submission_unverified", "交卷确认未完成");
    const after = await until(
      async () => {
        const s = examState(await readExamState(http, task.businessId));
        return newSubmission(s, before) ? s : false;
      },
      30000,
      ctx.lease.signal,
      1500,
    );
    ctx.repo.confirm(intent, {
      recordId: after.recordId,
      submitted: true,
      passed: after.passed,
    });
    if (after.passed)
      ctx.repo.complete(task, { source: "exam-user-record", passed: true });
    else {
      ctx.repo.saveHistory(task, {
        failedModels: after.pending ? [] : [ai.fingerprint],
      });
      ctx.repo.review(
        task,
        after.pending ? "pending_grading" : "exam_not_passed",
        { message: after.pending ? "等待平台评卷" : "考试未通过，保留复查" },
        true,
      );
    }
    keep = false;
    ctx.scope.runtime.retained.delete(tab);
  } catch (e) {
    if (keep)
      ctx.repo.review(
        task,
        e instanceof ReviewRequired ? e.reason : "exam_interrupted",
        { message: e instanceof Error ? e.message : "考试需要复查" },
        true,
      );
    throw e;
  } finally {
    if (!keep) await tab.close();
  }
}
