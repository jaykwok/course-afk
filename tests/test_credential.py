import unittest
from datetime import datetime, timedelta

from core.config import MYLEARNING_CENTER_HOME
from core.auth.credential import (
    build_account_label,
    extract_account_profile_from_sync_context,
    is_credential_expired,
    is_credential_expired_at,
)


class CredentialTests(unittest.TestCase):
    def test_credential_expired_after_28_days(self):
        saved_at = datetime(2026, 4, 1, 8, 0, 0)
        now = saved_at + timedelta(days=29)
        self.assertTrue(is_credential_expired(saved_at, now))

    def test_credential_still_valid_within_28_days(self):
        saved_at = datetime(2026, 4, 1, 8, 0, 0)
        now = saved_at + timedelta(days=27)
        self.assertFalse(is_credential_expired(saved_at, now))

    def test_credential_expired_on_expiration_date(self):
        expires_at = datetime(2026, 5, 19, 14, 34, 28)
        now = datetime(2026, 5, 19, 8, 0, 0)
        self.assertTrue(is_credential_expired_at(expires_at, now))

    def test_build_account_label_prefers_full_name(self):
        self.assertEqual(
            build_account_label("测试用户", "test_user"),
            "测试用户（test_user）",
        )


class FakeSyncPage:
    def __init__(self, profile_data):
        self.profile_data = profile_data
        self.calls = []

    def goto(self, url, timeout=None):
        self.calls.append(("goto", url, timeout))

    def wait_for_url(self, pattern, timeout=0):
        self.calls.append(("wait_for_url", pattern.pattern, timeout))

    def wait_for_timeout(self, milliseconds):
        self.calls.append(("wait_for_timeout", milliseconds))

    def evaluate(self, script):
        self.calls.append(("evaluate",))
        if "__courseAfk" in script:
            return None
        return self.profile_data

    def close(self):
        self.calls.append(("close",))


class FakeSyncContext:
    def __init__(self, page):
        self.page = page

    def new_page(self):
        return self.page


class SyncCredentialTests(unittest.TestCase):
    def test_extract_account_profile_from_sync_context_uses_personal_center(self):
        page = FakeSyncPage({"fullName": "测试用户", "name": ""})
        context = FakeSyncContext(page)

        profile = extract_account_profile_from_sync_context(context)

        self.assertEqual(profile.label, "测试用户")
        self.assertEqual(page.calls[0], ("goto", MYLEARNING_CENTER_HOME, 30000))
        self.assertIn("center", page.calls[1][1])
        self.assertEqual(page.calls[1][2], 30000)
        self.assertEqual(page.calls[-1], ("close",))


if __name__ == "__main__":
    unittest.main()
