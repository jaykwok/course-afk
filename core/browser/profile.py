"""Seed disposable Windows Chromium profiles without probing OS credentials."""

import json
from pathlib import Path

# Chromium compares this cached int64 against the OS password-change time.
# False alone is insufficient: an absent timestamp still calls LogonUser("").
# This conservative cache is only for disposable automation profiles: it never
# treats the OS account as passwordless and never changes a personal profile.
PASSWORD_CHECK_CACHE_TIME = str(2**63 - 1)


def seed_automation_profile(directory: Path) -> None:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents accidentally overwriting an existing profile.
    with (directory / "Local State").open("x", encoding="utf-8") as stream:
        json.dump({"password_manager": {
            "os_password_blank": False,
            "os_password_last_changed": PASSWORD_CHECK_CACHE_TIME,
        }}, stream)
    profile = directory / "Default"
    profile.mkdir()
    with (profile / "Preferences").open("x", encoding="utf-8") as stream:
        json.dump({"credentials_enable_service": False,
                   "credentials_enable_autosignin": False,
                   "profile": {"password_manager_enabled": False}}, stream)


def validate_automation_profile(directory: Path) -> None:
    try:
        data = json.loads((Path(directory) / "Local State").read_text(encoding="utf-8"))
        prefs = data["password_manager"]
        valid = (prefs["os_password_blank"] is False
                 and prefs["os_password_last_changed"] == PASSWORD_CHECK_CACHE_TIME)
    except (OSError, ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        raise RuntimeError("自动化浏览器安全缓存未就绪，已阻止启动以避免 Windows 空密码检测")
