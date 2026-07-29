import unittest
from unittest.mock import AsyncMock, patch


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_ai_exam_workflow_passes_auto_submit_to_batch(self):
        from core.app.workflows import run_ai_exam_workflow

        with (
            patch(
                "core.app.workflows.collect_project_state",
                return_value=type(
                    "State",
                    (),
                    {
                        "has_credential": True,
                        "credential_expired": False,
                        "learning_count": 1,
                        "exam_count": 2,
                        "manual_exam_count": 0,
                    },
                )(),
            ),
            patch(
                "core.app.workflows.run_ai_exam_batch",
                new=AsyncMock(return_value=1),
            ) as mock_batch,
        ):
            result = await run_ai_exam_workflow(auto_submit=False)

        self.assertEqual(result, 1)
        mock_batch.assert_awaited_once_with(status_callback=None, auto_submit=False)

    async def test_run_ai_exam_workflow_disables_auto_submit_by_default(self):
        from core.app.workflows import run_ai_exam_workflow

        with (
            patch(
                "core.app.workflows.collect_project_state",
                return_value=type(
                    "State",
                    (),
                    {"exam_count": 1},
                )(),
            ),
            patch(
                "core.app.workflows.run_ai_exam_batch",
                new=AsyncMock(return_value=0),
            ) as mock_batch,
        ):
            await run_ai_exam_workflow()

        mock_batch.assert_awaited_once_with(status_callback=None, auto_submit=False)

    async def test_run_recommended_flow_returns_manual_exam_pending_without_callback(self):
        from core.app.workflows import run_recommended_flow

        with patch(
            "core.app.workflows.collect_project_state",
            return_value=type(
                "State",
                (),
                {
                    "has_credential": True,
                    "credential_expired": False,
                    "learning_count": 1,
                    "exam_count": 1,
                    "manual_exam_count": 0,
                },
            )(),
        ), patch(
            "core.app.workflows.is_ai_configured", return_value=True,
        ), patch(
            "core.app.workflows.run_afk_workflow",
            return_value=True,
        ), patch(
            "core.app.workflows.run_ai_exam_workflow",
            return_value=1,
        ) as mock_run_ai_exam:
            result = await run_recommended_flow()

        self.assertEqual(result, "manual-exam-pending")
        mock_run_ai_exam.assert_awaited_once_with(
            status_callback=None,
            auto_submit=False,
        )

    async def test_run_recommended_flow_asks_auto_submit_when_exam_detected(self):
        from core.app.workflows import run_recommended_flow

        ask_auto_submit = unittest.mock.Mock(return_value=False)

        with (
            patch(
                "core.app.workflows.collect_project_state",
                return_value=type(
                    "State",
                    (),
                    {
                        "has_credential": True,
                        "credential_expired": False,
                        "learning_count": 1,
                        "exam_count": 0,
                        "manual_exam_count": 0,
                    },
                )(),
            ),
            patch("core.app.workflows.is_ai_configured", return_value=True),
            patch("core.app.workflows.run_afk_workflow", return_value=True),
            patch(
                "core.app.workflows.run_ai_exam_workflow",
                new=AsyncMock(return_value=0),
            ) as mock_run_ai_exam,
        ):
            result = await run_recommended_flow(ask_auto_submit=ask_auto_submit)

        self.assertEqual(result, "done")
        ask_auto_submit.assert_called_once_with()
        mock_run_ai_exam.assert_awaited_once_with(
            status_callback=None,
            auto_submit=False,
        )

    async def test_run_recommended_flow_skips_ai_exam_when_not_configured(self):
        from core.app.workflows import run_recommended_flow

        ask_auto_submit = unittest.mock.Mock(return_value=False)

        with (
            patch(
                "core.app.workflows.collect_project_state",
                return_value=type(
                    "State",
                    (),
                    {
                        "has_credential": True,
                        "credential_expired": False,
                        "learning_count": 1,
                        "exam_count": 1,
                        "manual_exam_count": 0,
                    },
                )(),
            ),
            patch("core.app.workflows.run_afk_workflow", return_value=True),
            patch("core.app.workflows.is_ai_configured", return_value=False),
            patch(
                "core.app.workflows.run_ai_exam_workflow",
                new=AsyncMock(),
            ) as mock_run_ai_exam,
        ):
            result = await run_recommended_flow(ask_auto_submit=ask_auto_submit)

        self.assertEqual(result, "ai-not-configured")
        mock_run_ai_exam.assert_not_awaited()
        ask_auto_submit.assert_not_called()

    async def test_run_recommended_flow_processes_existing_exam_without_learning(self):
        from core.app.workflows import run_recommended_flow

        with (
            patch(
                "core.app.workflows.collect_project_state",
                return_value=type(
                    "State",
                    (),
                    {
                        "has_credential": True,
                        "credential_expired": False,
                        "learning_count": 0,
                        "exam_count": 2,
                        "manual_exam_count": 0,
                    },
                )(),
            ),
            patch("core.app.workflows.is_ai_configured", return_value=True),
            patch(
                "core.app.workflows.run_afk_workflow", new=AsyncMock()
            ) as mock_run_afk,
            patch(
                "core.app.workflows.run_ai_exam_workflow",
                new=AsyncMock(return_value=0),
            ) as mock_run_ai_exam,
        ):
            result = await run_recommended_flow()

        self.assertEqual(result, "done")
        mock_run_afk.assert_not_awaited()
        mock_run_ai_exam.assert_awaited_once_with(
            status_callback=None,
            auto_submit=False,
        )

    async def test_run_recommended_flow_reports_existing_manual_exam(self):
        from core.app.workflows import run_recommended_flow

        with patch(
            "core.app.workflows.collect_project_state",
            return_value=type(
                "State",
                (),
                {
                    "has_credential": True,
                    "credential_expired": False,
                    "learning_count": 0,
                    "exam_count": 0,
                    "manual_exam_count": 1,
                },
            )(),
        ):
            result = await run_recommended_flow()

        self.assertEqual(result, "manual-exam-pending")

    async def test_format_status_error_message_sanitizes_raw_playwright_error(self):
        from core.app.workflows import _format_status_error_message

        message = _format_status_error_message(
            "记录新页面链接失败",
            RuntimeError(
                "Locator.wait_for: Timeout 3000ms exceeded.\n"
                "Call log:\n"
                '  - waiting for locator(".single-btns") to be visible\n'
            ),
        )

        self.assertEqual(message, "记录新页面链接失败")


class RefreshCredentialTests(unittest.TestCase):
    def test_refresh_credential_returns_login_result_without_profile_fallback(self):
        from core.auth.credential import AccountProfile
        from core.app.workflows import refresh_credential

        messages = []
        profile = AccountProfile()

        with patch(
            "core.app.workflows.login_and_save_credential",
            return_value=profile,
        ) as mock_login:
            result = refresh_credential(status_callback=messages.append)

        self.assertIs(result, profile)
        self.assertEqual(messages, ["正在打开浏览器，请完成登录"])
        mock_login.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
