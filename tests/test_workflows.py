from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from core.app import workflows


def state(*, learning=0, exams=0, manual=0, failures=0, credential=True):
    return SimpleNamespace(has_credential=credential, credential_expired=False,
        learning_count=learning, exam_count=exams, manual_exam_count=manual,
        learning_failure_count=failures)


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_ai_workflow_defaults_to_manual_review_and_forwards_auto_submit_opt_in(self):
        for requested in (None, False, True):
            with self.subTest(requested=requested), patch.object(workflows, "collect_project_state", return_value=state(exams=2)), patch.object(workflows, "run_ai_exam_batch", new=AsyncMock(return_value=1)) as batch:
                result = await workflows.run_ai_exam_workflow(**({"auto_submit": requested} if requested is not None else {}))
                self.assertEqual(result, 1)
                batch.assert_awaited_once_with(status_callback=None, auto_submit=requested is True)

    async def test_format_status_error_message_sanitizes_raw_playwright_error(self):
        message = workflows._format_status_error_message("记录新页面链接失败", RuntimeError("Locator.wait_for: Timeout 3000ms exceeded.\nCall log:\n  - waiting for locator"))
        self.assertEqual(message, "记录新页面链接失败")


class RefreshCredentialTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_credential_returns_login_result_without_profile_fallback(self):
        from core.auth.credential import AccountProfile
        messages = []
        profile = AccountProfile()
        with patch.object(workflows, "login_and_save_credential", new=AsyncMock(return_value=profile)) as login:
            result = await workflows.refresh_credential(status_callback=messages.append)
        self.assertIs(result, profile)
        self.assertEqual(messages, ["正在打开浏览器，请完成登录"])
        login.assert_awaited_once_with(confirm_same_account=None)


if __name__ == "__main__":
    unittest.main()
