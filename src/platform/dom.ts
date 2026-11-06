import type { Tab } from "nodriver-ts";
import { AuthenticationError, ReviewRequired } from "../utils.ts";
export async function clickText(
  tab: Tab,
  labels: string[],
  signal: AbortSignal,
  selector = 'button,a,[role="button"],.btn',
  guard?: () => void,
) {
  signal.throwIfAborted();
  const wanted = labels.map((s) => s.replace(/\s/g, ""));
  for (const element of await tab.queryAll(selector, {
    visible: true,
    timeout: 5000,
    signal,
  })) {
    const value = await element.evaluate((e) => ({
      text: (e.innerText || e.textContent || "").replace(/\s/g, ""),
      disabled:
        e.hasAttribute("disabled") ||
        e.getAttribute("aria-disabled") === "true",
    }));
    if (wanted.includes(value.text) && !value.disabled) {
      await element.click({ signal, timeout: 5000, guard });
      return true;
    }
  }
  return false;
}
export async function inspectAccess(tab: Tab, signal: AbortSignal) {
  const text = await tab.evaluate(
    () => document.body.innerText.slice(0, 15000),
    undefined,
    { timeout: 5000, signal },
  );
  if (/安全验证|访问过于频繁|访问已被拦截|操作过于频繁|验证码/.test(text))
    throw new AuthenticationError("平台要求验证或暂时限制访问，本轮已停止");
  if (/同时学习|正在学习其他课程|同一时间只能/.test(text))
    throw new AuthenticationError(
      "平台提示已有其他学习会话，请结束其他会话后继续",
    );
  if (/没有权限|无权访问|课程已下架|不在学习范围/.test(text))
    throw new ReviewRequired("access_denied", "当前账号无法访问此课程");
}
export async function dismissOverlays(tab: Tab, signal: AbortSignal) {
  const elements = await tab.queryAll(
    '.el-dialog__headerbtn,.ant-modal-close,.modal .close,.dialog .close,[aria-label="Close"],[aria-label="关闭"]',
    { visible: true, timeout: 5000, signal },
  );
  for (const element of elements.slice(-2)) {
    const business = await element.evaluate((e) => {
      const d = e.closest(
        '.ant-modal-content,[role="dialog"],.dialog,[data-region="modal:modal"]',
      );
      return (
        !!d &&
        (!!d.querySelector(".ant-rate,.preview-list,.answer-input-shot") ||
          /归档|继续学习|我要交卷|确认交卷|提交试卷|评分|评价/.test(
            d.textContent || "",
          ))
      );
    });
    if (!business) await element.click({ signal, timeout: 3000 });
  }
}
