import {
  ActionabilityError,
  type Tab,
  type Element as BrowserElement,
} from "nodriver-ts";
import { AuthenticationError, ReviewRequired } from "../utils.ts";
/** Only dismiss a confirmed archive prompt; unrelated business dialogs stay intact. */
export async function continueArchivedCourse(
  tab: Tab,
  signal: AbortSignal,
  guard: () => void,
) {
  guard();
  for (const dialog of await tab.queryAll(
    ".study-transition-page,.ant-modal-content",
    {
      visible: true,
      timeout: 5000,
      signal,
    },
  )) {
    if (!/归档/.test(await dialog.text())) continue;
    for (const button of await dialog.queryAll(
      'button,.btn,a,span,[role="button"],[id$="goOnStudy"]',
      {
        visible: true,
        timeout: 3000,
        signal,
      },
    )) {
      if ((await button.text()).replace(/\s/g, "") !== "继续学习") continue;
      if (
        await button.evaluate(
          (e) =>
            e.matches(":disabled") ||
            e.getAttribute("aria-disabled") === "true",
        )
      )
        continue;
      await button.click({ signal, timeout: 5000, guard });
      await waitForOverlayExit(
        tab,
        ".study-transition-page,.ant-modal-content",
        signal,
        guard,
        "归档",
      );
      return true;
    }
  }
  return false;
}
async function waitForOverlayExit(
  tab: Tab,
  selector: string,
  signal: AbortSignal,
  guard: () => void,
  text = "",
) {
  guard();
  await tab.waitForFunction(
    ({ selector, text }) =>
      document.readyState !== "loading" &&
      ![...document.querySelectorAll(selector)].some((e) => {
        const rect = e.getBoundingClientRect(),
          style = getComputedStyle(e);
        return (
          rect.width > 0 &&
          rect.height > 0 &&
          style.visibility !== "hidden" &&
          style.display !== "none" &&
          (!text || e.textContent?.includes(text))
        );
      }),
    { selector, text },
    { timeout: 5000, signal },
  );
  guard();
}
/** Some course gates bind registration to an image ID rather than button text. */
export async function startRegisteredCourse(
  tab: Tab,
  signal: AbortSignal,
  guard: () => void,
) {
  guard();
  const image = await tab.query('.register-mask-layer [id$="registerMask"]', {
    visible: true,
    timeout: 3000,
    signal,
  });
  const clicked = image
    ? (await image.click({ signal, timeout: 5000, guard }), true)
    : await clickText(
        tab,
        ["开始学习", "继续学习"],
        signal,
        ".register-mask-layer button,.register-mask-layer .btn,.register-mask-layer a,.register-mask-layer span",
        guard,
      );
  if (clicked)
    await waitForOverlayExit(tab, ".register-mask-layer", signal, guard);
  return clicked;
}
/** Native input for main-frame player controls, with a fresh hit check before press. */
export async function clickPlayerControl(
  tab: Tab,
  element: BrowserElement,
  signal: AbortSignal,
  guard: () => void,
) {
  signal.throwIfAborted();
  guard();
  if (element.frame !== tab.mainFrame)
    throw new ActionabilityError("播放按钮不在主页面");
  await element.scrollIntoView({ timeout: 5000, signal });
  const point = await element.evaluate((e) => {
    const r = e.getBoundingClientRect();
    return {
      x: (Math.max(0, r.left) + Math.min(innerWidth, r.right)) / 2,
      y: (Math.max(0, r.top) + Math.min(innerHeight, r.bottom)) / 2,
    };
  });
  const verify = async () => {
    guard();
    signal.throwIfAborted();
    const valid = await element.evaluate((e, { x, y }) => {
      const r = e.getBoundingClientRect(),
        style = getComputedStyle(e);
      const hit = document.elementFromPoint(x, y);
      return (
        e.isConnected &&
        r.width > 0 &&
        r.height > 0 &&
        x >= 0 &&
        y >= 0 &&
        x < innerWidth &&
        y < innerHeight &&
        x >= r.left &&
        x < r.right &&
        y >= r.top &&
        y < r.bottom &&
        style.visibility !== "hidden" &&
        style.display !== "none" &&
        !e.matches(":disabled") &&
        e.getAttribute("aria-disabled") !== "true" &&
        !!hit &&
        (hit === e || e.contains(hit))
      );
    }, point);
    if (!valid)
      throw new ActionabilityError("播放按钮已移动或被遮挡，未执行点击");
  };
  await verify();
  await tab.mouse.click(point.x, point.y, {
    timeout: 5000,
    signal,
    guard,
    beforePress: verify,
  });
}
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
export interface AccessSignals {
  challenge: boolean;
  title: string;
  notices: string[];
}
export function accessProblem({
  challenge,
  title,
  notices,
}: AccessSignals): "authentication" | "concurrent" | "denied" | undefined {
  if (
    challenge ||
    /^(?:安全验证|人机验证|访问验证|访问已被拦截|访问过于频繁)(?:\s*[-|·].*)?$/.test(
      title.trim(),
    ) ||
    notices.some((text) =>
      /^(?:安全验证|人机验证|身份验证)(?:\s|$)|请.{0,12}(?:输入|填写).{0,8}验证码|请.{0,12}(?:完成|通过).{0,8}验证|(?:安全|人机|身份)验证失败|访问过于频繁|访问已被拦截|操作过于频繁/.test(
        text,
      ),
    )
  )
    return "authentication";
  if (
    notices.some((text) => /同时学习|正在学习其他课程|同一时间只能/.test(text))
  )
    return "concurrent";
  if (
    notices.some((text) =>
      /没有权限|无权访问|课程已下架|不在学习范围/.test(text),
    )
  )
    return "denied";
}
export async function inspectAccess(tab: Tab, signal: AbortSignal) {
  const signals = await tab.evaluate(
    () => {
      const content =
        ".question-type-item,.single-title,.stem-content-main,.preview-list,.rich-text-style,.chapter-list-box";
      const visible = (e: Element) =>
        e.getBoundingClientRect().height > 0 &&
        getComputedStyle(e).visibility !== "hidden";
      const outsideContent = (e: Element) =>
        !e.closest(content) && !e.querySelector(content);
      const challenge = [
        ...document.querySelectorAll(
          '#captcha,.captcha-container,.geetest_panel,.geetest_panel_box,.yidun_popup,.nc-container,[data-captcha],input[name*="captcha" i],input[id*="captcha" i],iframe[src*="captcha" i]',
        ),
      ].some((e) => visible(e) && outsideContent(e));
      const notices = [
        ...document.querySelectorAll(
          '[role="alert"],[role="dialog"],.ant-modal-content,.el-dialog,.ant-message-notice-content,.el-message,.error-page,.access-denied,.ant-result,.el-result,h1,h2',
        ),
      ]
        .filter((e) => visible(e) && outsideContent(e))
        .map((e) => (e as HTMLElement).innerText.slice(0, 2000));
      // Some platform error pages use plain divs without an alert role.
      for (const e of document.querySelectorAll("p,div,span")) {
        const text = (e.textContent || "").trim();
        if (
          !e.children.length &&
          visible(e) &&
          outsideContent(e) &&
          /^(?:您|你|当前账号)?(?:没有权限|无权)(?:查看|访问)(?:该|此)资源[。！!]?$/u.test(
            text,
          )
        )
          notices.push(text);
      }
      return { challenge, title: document.title, notices };
    },
    undefined,
    { timeout: 5000, signal },
  );
  const problem = accessProblem(signals);
  if (problem === "authentication")
    throw new AuthenticationError("平台要求验证或暂时限制访问，本轮已停止");
  if (problem === "concurrent")
    throw new AuthenticationError(
      "平台提示已有其他学习会话，请结束其他会话后继续",
    );
  if (problem === "denied")
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
