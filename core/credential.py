from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from core.config import (
    CREDENTIAL_META_FILE,
    CREDENTIAL_VALID_DAYS,
    ZHIXUEYUN_HOME,
    ZHIXUEYUN_HOME_PATTERN,
)
from core.page_overlays import (
    dismiss_topmost_overlays_async,
    dismiss_topmost_overlays_sync,
)


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
    now = now or datetime.now()
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


def extract_account_profile_from_storage(storage_value: str | dict[str, Any] | None) -> AccountProfile:
    if storage_value is None:
        return AccountProfile()
    if isinstance(storage_value, str):
        try:
            storage_value = json.loads(storage_value)
        except json.JSONDecodeError:
            return AccountProfile(account_name=storage_value.strip())
    if isinstance(storage_value, dict):
        return extract_account_profile(storage_value)
    return AccountProfile()


async def extract_account_profile_from_async_context(
    context,
    wait_milliseconds: int = 3000,
    navigation_timeout: int = 10000,
) -> AccountProfile:
    page = await context.new_page()
    try:
        await page.goto(ZHIXUEYUN_HOME, timeout=navigation_timeout)
        await page.wait_for_url(
            re.compile(ZHIXUEYUN_HOME_PATTERN),
            timeout=navigation_timeout,
        )
        await page.wait_for_timeout(wait_milliseconds)
        await dismiss_topmost_overlays_async(page)
        storage_value = await page.evaluate(
            "() => window.localStorage.getItem('user')"
        )
        return extract_account_profile_from_storage(storage_value)
    finally:
        await page.close()


def extract_account_profile_from_sync_context(
    context,
    wait_milliseconds: int = 3000,
    navigation_timeout: int = 10000,
) -> AccountProfile:
    page = context.new_page()
    try:
        page.goto(ZHIXUEYUN_HOME, timeout=navigation_timeout)
        page.wait_for_url(
            re.compile(ZHIXUEYUN_HOME_PATTERN),
            timeout=navigation_timeout,
        )
        page.wait_for_timeout(wait_milliseconds)
        dismiss_topmost_overlays_sync(page)
        storage_value = page.evaluate(
            "() => window.localStorage.getItem('user')"
        )
        return extract_account_profile_from_storage(storage_value)
    finally:
        page.close()


def save_credential_metadata(
    saved_at: datetime,
    full_name: str | None = None,
    account_name: str | None = None,
    metadata_path=CREDENTIAL_META_FILE,
) -> CredentialMetadata:
    profile = AccountProfile(
        full_name=(full_name or "").strip(),
        account_name=(account_name or "").strip(),
    )
    expires_at = saved_at + timedelta(days=CREDENTIAL_VALID_DAYS)
    metadata = CredentialMetadata(
        saved_at=saved_at.isoformat(timespec="seconds"),
        expires_at=expires_at.isoformat(timespec="seconds"),
        account_display_name=profile.full_name,
        account_name=profile.account_name,
        account_label=profile.label,
    )
    with open(metadata_path, "w", encoding="utf-8") as file:
        json.dump(asdict(metadata), file, ensure_ascii=False, indent=2)
    return metadata


def load_credential_metadata(metadata_path=CREDENTIAL_META_FILE) -> CredentialMetadata | None:
    try:
        with open(metadata_path, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
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
