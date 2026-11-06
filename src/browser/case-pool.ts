import {
  InputActionError,
  ProtocolError,
  StaleElementError,
  TimeoutError,
  type Tab,
} from "nodriver-ts";
import { CASE_POOL, platformHost } from "../domain/identity.ts";
import { ReviewRequired } from "../utils.ts";

/** Wait through the site's own SSO flow; never read its token out of the page. */
export async function waitForCasePoolSession(
  tab: Tab,
  signal: AbortSignal,
  guard: () => void,
  timeout = 60000,
) {
  const deadline = Date.now() + timeout;
  let clicked = false;
  while (Date.now() < deadline) {
    guard();
    signal.throwIfAborted();
    if (tab.closed) throw new Error("案例库页面已关闭");
    try {
      try {
        await tab.waitForFunction(
          (target) => {
            const expected = new URL(target);
            return (
              location.origin === expected.origin &&
              location.pathname.replace(/\/$/, "") === expected.pathname &&
              document.readyState !== "loading" &&
              !!localStorage.getItem("userID")
            );
          },
          CASE_POOL,
          {
            timeout: Math.max(1, Math.min(500, deadline - Date.now())),
            signal,
          },
        );
        guard();
        return;
      } catch (error) {
        if (!(error instanceof TimeoutError)) throw error;
      }
      signal.throwIfAborted();
      if (Date.now() >= deadline) break;
      if (!clicked) {
        for (const frame of tab.frames) {
          if (frame.detached) continue;
          let host: string;
          try {
            host = new URL(frame.url).hostname;
          } catch {
            continue;
          }
          if (
            !["ctexpert.cn", "www.ctexpert.cn"].includes(host) &&
            !platformHost(host)
          )
            continue;
          // A password form needs human input; only follow an explicit SSO entry.
          if (
            await frame.query('input[type="password"]', {
              visible: true,
              timeout: 1000,
              signal,
            })
          )
            continue;
          for (const button of await frame.queryAll(
            'button,a,[role="button"],input[type="button"],input[type="submit"]',
            {
              visible: true,
              timeout: 1000,
              signal,
            },
          )) {
            const eligible = await button.evaluate(
              (e) =>
                [
                  "登录",
                  "立即登录",
                  "去登录",
                  "重新登录",
                  "统一认证登录",
                  "单点登录",
                  "授权登录",
                  "登录并继续",
                  "同意并登录",
                ].includes(
                  (
                    e.innerText ||
                    e.textContent ||
                    (e as HTMLInputElement).value ||
                    ""
                  ).replace(/\s/g, ""),
                ) &&
                !e.matches(":disabled") &&
                e.getAttribute("aria-disabled") !== "true",
            );
            if (!eligible) continue;
            await button.click({ timeout: 3000, signal, guard });
            clicked = true;
            break;
          }
          if (clicked) break;
        }
      }
    } catch (error) {
      signal.throwIfAborted();
      if (error instanceof InputActionError) {
        if (error.report.events > 0) clicked = true;
        if ((await error.cleanup).status !== "completed") throw error;
        error = error.cause;
      }
      if (
        error instanceof TimeoutError ||
        error instanceof StaleElementError ||
        (error instanceof ProtocolError &&
          /context.*(?:destroyed|not found)|cannot find context|no frame with given id/i.test(
            error.message,
          ))
      )
        continue;
      throw error;
    }
  }
  throw new ReviewRequired(
    "discovery_incomplete",
    "案例库认证回跳尚未完成，请检查平台登录提示后重新解析",
  );
}
