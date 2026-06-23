from __future__ import annotations

import json
import logging
from datetime import datetime

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from core.config import (
    AUTO_LOGIN_DATA_TIME,
    COOKIES_FILE,
    MYLEARNING_HOME,
    MYLEARNING_SSO_PATTERN,
)
from core.browser import (
    apply_sync_browser_stealth,
    build_browser_context_options,
    is_target_closed_exception,
    launch_sync_browser,
)
from core.credential import (
    AccountProfile,
    extract_account_profile_from_sync_context,
    save_credential_metadata,
)


class LoginNotCompletedError(RuntimeError):
    """Raised when the login browser is closed before credentials are saved."""


AUTO_LOGIN_GROUP_SELECTOR = (
    "#j-auto-group-qr, #j-auto-group, #j-auto-group-sms, #j-auto-group-qk"
)
AGREEMENT_CHECKBOX_SELECTOR = '[id^="j-agreement-box"]'
SELECT_AUTO_LOGIN_DURATION_SCRIPT = """
(groups, dataTime) => {
  let selectedCount = 0;

  for (const group of groups) {
    const option = group.querySelector(
      `.login-option-list .option[data-time="${dataTime}"]`
    );
    if (option) {
      option.click();
      selectedCount += 1;
    }

    const checkbox = group.querySelector('i[id^="j-auto-login"]');
    if (checkbox && checkbox.getAttribute("data-index") !== "1") {
      checkbox.click();
    }
  }

  return selectedCount;
}
"""
ACCEPT_LOGIN_AGREEMENTS_SCRIPT = """
checkboxes => {
  let checkedCount = 0;

  for (const checkbox of checkboxes) {
    const className = String(checkbox.className || "");
    const isChecked =
      checkbox.getAttribute("data-index") === "1" ||
      className.includes("checkbox-icon2");

    if (!isChecked) {
      checkbox.click();
    }
    checkedCount += 1;
  }

  return checkedCount;
}
"""
INSTALL_LOGIN_PREFERENCES_WATCHER_SCRIPT = """
(_body, dataTime) => {
  const AUTO_LOGIN_GROUP_SELECTOR = "#j-auto-group-qr, #j-auto-group, #j-auto-group-sms, #j-auto-group-qk";
  const AGREEMENT_CHECKBOX_SELECTOR = '[id^="j-agreement-box"]';

  const applyPreferences = () => {
    let selectedCount = 0;
    let checkedCount = 0;

    for (const group of document.querySelectorAll(AUTO_LOGIN_GROUP_SELECTOR)) {
      const option = group.querySelector(
        `.login-option-list .option[data-time="${dataTime}"]`
      );
      const timeText = group.querySelector(".login-time-text");
      const optionText = String(option?.textContent || "").trim();
      const currentTimeText = String(timeText?.textContent || "").trim();
      const durationSelected =
        Boolean(option?.classList.contains("active")) &&
        (!optionText || currentTimeText === optionText);

      if (option && !durationSelected) {
        option.click();
      }

      const activeOption = group.querySelector(
        `.login-option-list .option[data-time="${dataTime}"].active`
      );
      const checkbox = group.querySelector('i[id^="j-auto-login"]');
      const checkboxClassName = String(checkbox?.className || "");
      const checkboxChecked =
        checkbox?.getAttribute("data-index") === "1" ||
        checkboxClassName.includes("checkbox-icon2");

      if (checkbox && !checkboxChecked) {
        checkbox.click();
      }

      const updatedCheckboxClassName = String(checkbox?.className || "");
      const updatedCheckboxChecked =
        checkbox?.getAttribute("data-index") === "1" ||
        updatedCheckboxClassName.includes("checkbox-icon2");
      const updatedTimeText = String(timeText?.textContent || "").trim();
      if (
        activeOption &&
        updatedCheckboxChecked &&
        (!optionText || updatedTimeText === optionText)
      ) {
        selectedCount += 1;
      }
    }

    for (const checkbox of document.querySelectorAll(AGREEMENT_CHECKBOX_SELECTOR)) {
      const className = String(checkbox.className || "");
      const isChecked =
        checkbox.getAttribute("data-index") === "1" ||
        className.includes("checkbox-icon2");

      if (!isChecked) {
        checkbox.click();
      }

      const updatedClassName = String(checkbox.className || "");
      if (
        checkbox.getAttribute("data-index") === "1" ||
        updatedClassName.includes("checkbox-icon2")
      ) {
        checkedCount += 1;
      }
    }

    return { selectedCount, checkedCount };
  };

  window.__courseAfkApplyLoginPreferences = applyPreferences;
  const result = applyPreferences();

  if (window.__courseAfkLoginPreferencesTimer) {
    clearInterval(window.__courseAfkLoginPreferencesTimer);
  }
  window.__courseAfkLoginPreferencesTimer = setInterval(applyPreferences, 100);

  if (window.__courseAfkLoginPreferencesObserver) {
    window.__courseAfkLoginPreferencesObserver.disconnect();
  }
  let observerPending = false;
  window.__courseAfkLoginPreferencesObserver = new MutationObserver(() => {
    if (observerPending) {
      return;
    }
    observerPending = true;
    setTimeout(() => {
      observerPending = false;
      applyPreferences();
    }, 0);
  });
  if (document.body) {
    window.__courseAfkLoginPreferencesObserver.observe(document.body, {
      attributes: true,
      childList: true,
      characterData: true,
      subtree: true,
    });
  }

  return result;
}
"""


def select_auto_login_duration(login_frame, data_time: str = AUTO_LOGIN_DATA_TIME) -> int:
    login_frame.locator("#j-auto-group-qr").wait_for(state="attached")
    return login_frame.locator(AUTO_LOGIN_GROUP_SELECTOR).evaluate_all(
        SELECT_AUTO_LOGIN_DURATION_SCRIPT,
        data_time,
    )


def accept_login_agreements(login_frame) -> int:
    login_frame.locator("#j-agreement-box-sms").wait_for(state="attached")
    return login_frame.locator(AGREEMENT_CHECKBOX_SELECTOR).evaluate_all(
        ACCEPT_LOGIN_AGREEMENTS_SCRIPT
    )


def install_login_preferences_watcher(
    login_frame,
    data_time: str = AUTO_LOGIN_DATA_TIME,
) -> dict[str, int]:
    login_frame.locator("#j-auto-group-qr").wait_for(state="attached")
    return login_frame.locator("body").evaluate(
        INSTALL_LOGIN_PREFERENCES_WATCHER_SCRIPT,
        data_time,
    )


def login_and_save_credential() -> AccountProfile:
    with sync_playwright() as playwright:
        browser = launch_sync_browser(playwright, headless=False)
        context = browser.new_context(**build_browser_context_options(headless=False))
        apply_sync_browser_stealth(context)
        page = context.new_page()
        try:
            page.goto(MYLEARNING_HOME)
            page.wait_for_url(MYLEARNING_SSO_PATTERN, timeout=0)

            iframe = page.locator("#esurfingloginiframe").content_frame
            preferences = install_login_preferences_watcher(iframe)
            logging.info(
                "已开启登录页偏好自动保持："
                f"{preferences.get('selectedCount', 0)} 个登录方式为30天内自动登录，"
                f"{preferences.get('checkedCount', 0)} 个登录方式已勾选账号协议"
            )

            page.wait_for_url(MYLEARNING_HOME, timeout=0)
            # 防止网盘同步等外部工具把 cookies.json 设为只读，导致覆盖写入时 PermissionError
            if COOKIES_FILE.exists():
                COOKIES_FILE.chmod(0o644)
            with open(COOKIES_FILE, "w", encoding="utf-8") as file:
                json.dump(context.cookies(), file, ensure_ascii=False, indent=2)
            logging.info("已保存登录凭证")

            try:
                profile = extract_account_profile_from_sync_context(context)
            except PlaywrightTimeoutError as exc:
                logging.debug(f"读取知学云账号信息超时，继续保存登录凭证: {exc}")
                profile = AccountProfile()
            save_credential_metadata(
                saved_at=datetime.now(),
                full_name=profile.full_name,
                account_name=profile.account_name,
            )
            logging.info(f"已更新登录凭证元数据，当前账号：{profile.label}")
            return profile
        except Exception as exc:
            if is_target_closed_exception(exc):
                raise LoginNotCompletedError(
                    "已手动关闭浏览器，未完成登录，登录凭证未更新"
                ) from None
            raise
        finally:
            for close_target in (page, context, browser):
                try:
                    close_target.close()
                except Exception:
                    pass
