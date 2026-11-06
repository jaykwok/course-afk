import {
  ActionabilityError,
  InputActionError,
  ProtocolError,
  StaleElementError,
  TimeoutError,
  type Tab,
} from "nodriver-ts";
import { HOME } from "../config.ts";
import { AuthenticationError } from "../utils.ts";

const groups =
  "#j-auto-group-qr,#j-auto-group,#j-auto-group-sms,#j-auto-group-qk";
const durationSelected = (e: HTMLElement) => {
  const option = e.querySelector('.option[data-time="3"]');
  return !!(
    option?.classList.contains("active") &&
    option.textContent?.trim() ===
      e.querySelector(".login-time-text")?.textContent?.trim()
  );
};

/** Recheck the visible mode after each switch or rerender; use native input only. */
export async function applyLoginPreferences(tab: Tab, signal: AbortSignal) {
  const options = { signal, timeout: 3000 };
  for (const frame of tab.frames) {
    if (frame.detached) continue;
    try {
      for (const group of await frame.queryAll(groups, {
        ...options,
        visible: true,
      })) {
        const selected = await group.evaluate(durationSelected);
        if (!selected) {
          const menu = await group.query(".login-select", options);
          if (!menu) continue;
          await menu.hover(options);
          let option = await group.query('.option[data-time="3"]', {
            ...options,
            visible: true,
          });
          if (!option) {
            await menu.click(options);
            option = await group.query('.option[data-time="3"]', {
              ...options,
              visible: true,
            });
          }
          if (option) await option.click(options);
        }
        if (!(await group.evaluate(durationSelected))) continue;
        const checkbox = await group.query('i[id^="j-auto-login"]', options);
        if (
          checkbox &&
          !(await checkbox.evaluate(
            (e) =>
              e.getAttribute("data-index") === "1" ||
              e.classList.contains("checkbox-icon2"),
          ))
        )
          await checkbox.click(options);
      }
      for (const checkbox of await frame.queryAll('[id^="j-agreement-box"]', {
        ...options,
        visible: true,
      })) {
        if (
          !(await checkbox.evaluate(
            (e) =>
              e.getAttribute("data-index") === "1" ||
              e.classList.contains("checkbox-icon2"),
          ))
        )
          await checkbox.click(options);
      }
    } catch (error) {
      signal.throwIfAborted();
      if (error instanceof InputActionError) {
        const cleanup = await error.cleanup;
        if (cleanup.status !== "completed") throw error;
        error = error.cause;
      }
      // User mode changes and SSO can replace a frame between observation and input.
      if (
        frame.detached ||
        error instanceof StaleElementError ||
        error instanceof ActionabilityError ||
        error instanceof TimeoutError ||
        (error instanceof ProtocolError &&
          /context.*(?:destroyed|not found)|cannot find context|no frame with given id/i.test(
            error.message,
          ))
      )
        continue;
      throw error;
    }
  }
}

export async function waitForLoginReturn(tab: Tab, signal: AbortSignal) {
  // Human login has no fixed deadline. Each short library wait is cancellable
  // and follows replaced documents; preferences are refreshed between waits.
  while (true) {
    signal.throwIfAborted();
    if (tab.closed)
      throw new AuthenticationError("登录窗口已关闭，原凭证未更新");
    try {
      await applyLoginPreferences(tab, signal);
      await tab.waitForFunction(
        (home) => {
          const expected = new URL(home);
          return (
            location.origin === expected.origin &&
            location.pathname === expected.pathname &&
            document.readyState !== "loading" &&
            !document.querySelector("#esurfingloginiframe,input[type=password]")
          );
        },
        HOME,
        { signal, timeout: 500, world: "isolated" },
      );
      return;
    } catch (error) {
      signal.throwIfAborted();
      if (tab.closed)
        throw new AuthenticationError("登录窗口已关闭，原凭证未更新");
      if (!(error instanceof TimeoutError)) throw error;
    }
  }
}
