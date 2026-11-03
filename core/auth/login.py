from __future__ import annotations

import logging
import os
import stat
from datetime import datetime

from core.config import (
    AUTO_LOGIN_DATA_TIME,
    COOKIES_FILE,
    MYLEARNING_HOME,
)
from core.browser.session import (
    create_browser_context,
    is_target_closed_exception,
)
from core.auth.credential import (
    AccountProfile,
    AccountIdentityUnavailableError,
    extract_account_profile_from_context,
    load_credential_metadata,
    save_credential_bundle,
)
from core.browser.overlays import prepare_page_after_navigation_async


def _clear_readonly(path) -> None:
    """清除文件只读位，兼容 Windows（Path.chmod 在 Windows 上对权限位基本无效）。"""
    try:
        current = os.stat(path).st_mode
        os.chmod(path, current | stat.S_IWRITE)
    except OSError:
        pass


class LoginNotCompletedError(RuntimeError):
    """Raised when the login browser is closed before credentials are saved."""


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


async def install_login_preferences_watcher(
    login_frame,
    data_time: str = AUTO_LOGIN_DATA_TIME,
) -> dict[str, int]:
    await login_frame.locator("#j-auto-group-qr").wait_for(state="attached")
    return await login_frame.locator("body").evaluate(
        INSTALL_LOGIN_PREFERENCES_WATCHER_SCRIPT,
        data_time,
    )


async def login_and_save_credential() -> AccountProfile:
    async with create_browser_context(cookies_path=None, controller=False) as (
        _,
        context,
    ):
        page = await context.new_page()
        stage = "等待扫码登录完成"
        try:
            # SSO's third-party iframe can keep the load event pending until
            # after the user signs in. Observe its DOM before that redirect.
            await page.goto(MYLEARNING_HOME, wait_until="domcontentloaded")
            login_frame = page.locator("#esurfingloginiframe")
            await login_frame.wait_for(state="attached", timeout=120000)
            iframe = login_frame.content_frame
            preferences = await install_login_preferences_watcher(iframe)
            logging.info(
                "已开启登录页偏好自动保持："
                f"{preferences.get('selectedCount', 0)} 个登录方式为30天内自动登录，"
                f"{preferences.get('checkedCount', 0)} 个登录方式已勾选账号协议"
            )

            # Human login may wait indefinitely, but remains cancellable.
            await page.wait_for_url(
                MYLEARNING_HOME, timeout=0, wait_until="domcontentloaded"
            )
            stage = "获取账号资料"
            logging.info("扫码登录已返回主页，正在获取稳定账号标识")
            await prepare_page_after_navigation_async(page)
            profile = await extract_account_profile_from_context(context)
            if not profile.account_name.strip():
                raise LoginNotCompletedError(
                    "账号资料未返回稳定账号标识，登录凭证未更新；请确认中心站账号资料后重试"
                )
            stage = "核对待办账号归属"
            _validate_pending_account(profile)
            stage = "保存登录凭证"
            cookies = await context.cookies()
            if COOKIES_FILE.exists():
                _clear_readonly(COOKIES_FILE)
            save_credential_bundle(
                datetime.now().astimezone(), profile, cookies, cookies_path=COOKIES_FILE
            )
            logging.info(f"已更新登录凭证元数据，当前账号：{profile.label}")
            return profile
        except Exception as exc:
            logging.exception("登录未完成（%s），原凭证未更新", stage)
            if isinstance(exc, LoginNotCompletedError):
                raise
            if isinstance(exc, AccountIdentityUnavailableError):
                raise LoginNotCompletedError(str(exc)) from exc
            if is_target_closed_exception(exc):
                raise LoginNotCompletedError(
                    "已手动关闭浏览器，未完成登录，登录凭证未更新"
                ) from None
            raise LoginNotCompletedError(
                f"登录未完成：{stage}失败（{type(exc).__name__}），原凭证未更新；请查看错误日志"
            ) from exc


def _validate_pending_account(profile) -> None:
    from core.auth.credential import load_pending_owner_metadata
    from core.state import collect_project_state

    state = collect_project_state()
    if not any(
        (
            state.learning_count,
            state.learning_failure_count,
            state.exam_count,
            state.manual_exam_count,
        )
    ):
        return
    old = load_credential_metadata() or load_pending_owner_metadata()
    if old and old.account_name and profile.account_name:
        if old.account_name == profile.account_name:
            return
        raise LoginNotCompletedError(
            "仍有旧账号的待办，请先处理或导出后再切换账号；原凭证未更新"
        )
    raise LoginNotCompletedError(
        "待办缺少稳定账号归属，请先导出或清空待办再登录；原凭证未更新"
    )
