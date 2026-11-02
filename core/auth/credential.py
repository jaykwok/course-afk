from __future__ import annotations

import json
import re
from pathlib import Path
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from core.config import (
    COOKIES_FILE,
    CREDENTIAL_VALID_DAYS,
    MYLEARNING_CENTER_HOME,
    MYLEARNING_CENTER_HOME_PATTERN,
)
from core.browser.overlays import prepare_page_after_navigation_async
from core.storage import serialized, write_json_atomic


class AccountIdentityUnavailableError(ValueError):
    pass


@dataclass
class AccountProfile:
    """统一入口展示用账号信息。"""

    full_name: str = ""
    account_name: str = ""

    @property
    def label(self) -> str:
        return build_account_label(self.full_name, self.account_name)


@dataclass
class CredentialMetadata:
    """登录凭证元数据。"""

    saved_at: str
    expires_at: str
    account_display_name: str
    account_name: str
    account_label: str


def build_account_label(full_name: str | None, account_name: str | None) -> str:
    full_name = (full_name or "").strip()
    account_name = (account_name or "").strip()
    if full_name and account_name:
        return f"{full_name}（{account_name}）"
    return full_name or account_name or "未知账号"


def is_credential_expired(saved_at: datetime, now: datetime | None = None) -> bool:
    now = now or datetime.now(tz=saved_at.tzinfo)
    return now >= saved_at + timedelta(days=CREDENTIAL_VALID_DAYS)


def is_credential_expired_at(expires_at: datetime, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    return now.date() >= expires_at.date()


def extract_account_profile(user_data: dict[str, Any] | None) -> AccountProfile:
    if not user_data:
        return AccountProfile()
    return AccountProfile(
        full_name=str(user_data.get("fullName") or "").strip(),
        account_name=str(user_data.get("name") or "").strip(),
    )


def extract_center_account_profile(data) -> AccountProfile:
    if not isinstance(data, dict):
        return AccountProfile()
    user_id = data.get("id")
    if not isinstance(user_id, (str, int)) or isinstance(user_id, bool) or not str(user_id).strip():
        return AccountProfile()
    return AccountProfile(
        full_name=str(data.get("name") or "").strip(),
        account_name="center:" + str(user_id).strip(),
    )


async def extract_account_profile_from_context(context, navigation_timeout: int = 30000) -> AccountProfile:
    from core.runtime import close_safely
    page = await context.new_page()
    try:
        await page.goto(MYLEARNING_CENTER_HOME, timeout=navigation_timeout)
        await page.wait_for_url(re.compile(MYLEARNING_CENTER_HOME_PATTERN), timeout=navigation_timeout)
        await prepare_page_after_navigation_async(page)
        await page.wait_for_function("() => !!localStorage.getItem('token')", timeout=navigation_timeout)
        profile = await page.evaluate("""async () => {
            const token = localStorage.getItem('token');
            if (!token) return null;
            const response = await fetch('/api/personal/user/info', {
                headers: {'x-token': token}, redirect: 'error', signal: AbortSignal.timeout(15000)
            });
            if (!response.ok) return null;
            const body = await response.json();
            return body.code === 0 ? body.data : null;
        }""")
        result = extract_center_account_profile(profile)
        if not result.account_name:
            raise AccountIdentityUnavailableError("账号资料接口未返回稳定账号标识，登录凭证未更新；请确认中心站账号资料与登录状态")
        return result
    finally:
        await close_safely(page.close(), label="account profile page")


def save_credential_bundle(saved_at: datetime, profile: AccountProfile, cookies: list, *, cookies_path=COOKIES_FILE) -> CredentialMetadata:
    if not profile.account_name.strip() or not cookies:
        raise ValueError("账号信息或 cookie 不完整，凭证未更新")
    metadata = CredentialMetadata(saved_at=saved_at.isoformat(timespec="seconds"), expires_at=(saved_at + timedelta(days=CREDENTIAL_VALID_DAYS)).isoformat(timespec="seconds"), account_display_name=profile.full_name, account_name=profile.account_name, account_label=profile.label)
    # Cookie and account metadata share one atomic commit.
    write_json_atomic(cookies_path, {"version": 1, "cookies": cookies, "metadata": asdict(metadata)})
    return metadata


def load_credential_metadata(cookies_path=None) -> CredentialMetadata | None:
    try:
        bundle = json.loads(Path(cookies_path or COOKIES_FILE).read_text(encoding="utf-8"))
        if not isinstance(bundle, dict) or bundle.get("version") != 1 or not bundle.get("cookies"):
            return None
        data = bundle.get("metadata")
        if not isinstance(data, dict):
            return None
    except (OSError, json.JSONDecodeError):
        return None
    return CredentialMetadata(
        saved_at=str(data.get("saved_at") or ""),
        expires_at=str(data.get("expires_at") or ""),
        account_display_name=str(data.get("account_display_name") or ""),
        account_name=str(data.get("account_name") or ""),
        account_label=str(data.get("account_label") or ""),
    )


def parse_saved_at(metadata: CredentialMetadata | None) -> datetime | None:
    if not metadata or not metadata.saved_at:
        return None
    try:
        return datetime.fromisoformat(metadata.saved_at)
    except ValueError:
        return None


def parse_expires_at(metadata: CredentialMetadata | None) -> datetime | None:
    if not metadata or not metadata.expires_at:
        return None
    try:
        return datetime.fromisoformat(metadata.expires_at)
    except ValueError:
        return None


def load_pending_owner_metadata(cookies_path=None) -> CredentialMetadata | None:
    """清空 Cookie 后保留的不含令牌的待办归属，不作为登录凭据使用。"""
    try:
        bundle = json.loads(Path(cookies_path or COOKIES_FILE).read_text(encoding="utf-8"))
        data = bundle.get("pending_owner") if isinstance(bundle, dict) and bundle.get("version") == 1 else None
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not (data.get("account_name") or data.get("account_display_name")):
        return None
    return CredentialMetadata(
        saved_at="", expires_at="",
        account_display_name=str(data.get("account_display_name") or ""),
        account_name=str(data.get("account_name") or ""),
        account_label=str(data.get("account_label") or ""),
    )


@serialized
def clear_local_credentials(*, preserve_pending_owner: bool = False, cookies_path=None) -> bool:
    """清空本地 Cookie 和登录元数据；有待办时只留下账号归属。"""
    cookies_path = Path(cookies_path or COOKIES_FILE)
    if not cookies_path.exists():
        return False
    owner = None
    if preserve_pending_owner:
        try:
            bundle = json.loads(cookies_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            bundle = None
        data = (bundle.get("metadata") or bundle.get("pending_owner")) if isinstance(bundle, dict) and bundle.get("version") == 1 else None
        if isinstance(data, dict) and (data.get("account_name") or data.get("account_display_name")):
            owner = {key: str(data.get(key) or "") for key in ("account_display_name", "account_name", "account_label")}
    # 不备份 Cookie；重新登录即可获得新的凭据。
    write_json_atomic(cookies_path, {"version": 1, "cookies": [], "metadata": None, "pending_owner": owner})
    return True
