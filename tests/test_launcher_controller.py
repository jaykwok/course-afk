import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


class LauncherControllerTests(unittest.TestCase):
    def test_choose_learning_zone_mode_returns_manual_when_no_learning_zone_urls(self):
        from core.launcher_controller import choose_learning_zone_mode

        self.assertEqual(
            choose_learning_zone_mode([], prompt_choice_func=lambda *args, **kwargs: 1),
            "manual",
        )

    def test_choose_learning_zone_mode_returns_auto_when_user_selects_first_option(self):
        from core.launcher_controller import choose_learning_zone_mode

        self.assertEqual(
            choose_learning_zone_mode(
                ["https://cms.mylearning.cn/safe/topic/resource/2025/zycp/pc.html"],
                prompt_choice_func=lambda *args, **kwargs: 1,
            ),
            "auto",
        )

    def test_choose_learning_zone_mode_returns_manual_when_user_selects_second_option(self):
        from core.launcher_controller import choose_learning_zone_mode

        self.assertEqual(
            choose_learning_zone_mode(
                ["https://cms.mylearning.cn/safe/topic/resource/2025/zycp/pc.html"],
                prompt_choice_func=lambda *args, **kwargs: 2,
            ),
            "manual",
        )

    def test_maybe_delete_empty_exam_queue_file_deletes_without_prompt(self):
        from core.launcher_controller import _maybe_delete_empty_exam_queue_file

        class FakeUi:
            def __init__(self):
                self.messages = []

            def show_success(self, message):
                self.messages.append(message)

        with TemporaryDirectory() as tmp:
            exam_file = Path(tmp) / "考试链接.json"
            exam_file.write_text("[]", encoding="utf-8")
            ui = FakeUi()

            with patch("core.config.EXAM_URLS_FILE", exam_file):
                _maybe_delete_empty_exam_queue_file(ui)

            self.assertFalse(exam_file.exists())
            self.assertIn("已删除空的考试链接.json", ui.messages)

    def test_maybe_delete_empty_exam_queue_file_keeps_non_empty_file(self):
        from core.launcher_controller import _maybe_delete_empty_exam_queue_file

        class FakeUi:
            def __init__(self):
                self.messages = []

            def show_success(self, message):
                self.messages.append(message)

        with TemporaryDirectory() as tmp:
            exam_file = Path(tmp) / "考试链接.json"
            exam_file.write_text(
                json.dumps(
                    [
                        {
                            "url": "https://example.com/exam/1",
                            "ai_failed_model_configs": [],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            ui = FakeUi()

            with patch("core.config.EXAM_URLS_FILE", exam_file):
                _maybe_delete_empty_exam_queue_file(ui)

            self.assertTrue(exam_file.exists())
            self.assertEqual(ui.messages, [])

    def test_maybe_delete_empty_learning_queue_file_deletes_without_prompt(self):
        from core.launcher_controller import _maybe_delete_empty_learning_queue_file

        class FakeUi:
            def __init__(self):
                self.messages = []

            def show_success(self, message):
                self.messages.append(message)

        with TemporaryDirectory() as tmp:
            learning_file = Path(tmp) / "课程链接.json"
            learning_file.write_text("[]", encoding="utf-8")
            ui = FakeUi()

            with patch("core.config.LEARNING_URLS_FILE", learning_file):
                _maybe_delete_empty_learning_queue_file(ui)

            self.assertFalse(learning_file.exists())
            self.assertIn("已删除空的课程链接.json", ui.messages)

    def test_maybe_delete_empty_learning_queue_file_keeps_non_empty_file(self):
        from core.launcher_controller import _maybe_delete_empty_learning_queue_file

        class FakeUi:
            def __init__(self):
                self.messages = []

            def show_success(self, message):
                self.messages.append(message)

        with TemporaryDirectory() as tmp:
            learning_file = Path(tmp) / "课程链接.json"
            learning_file.write_text(
                json.dumps([{"url": "https://example.com/course/1"}]),
                encoding="utf-8",
            )
            ui = FakeUi()

            with patch("core.config.LEARNING_URLS_FILE", learning_file):
                _maybe_delete_empty_learning_queue_file(ui)

            self.assertTrue(learning_file.exists())
            self.assertEqual(ui.messages, [])

    def test_handle_ai_exam_prompts_for_auto_submit(self):
        from core.launcher_controller import handle_ai_exam

        class FakeUi:
            def __init__(self):
                self.messages = []

            def prompt_yes_no(self, message, default="N"):
                self.messages.append((message, default))
                return False

            def show_success(self, message):
                self.messages.append(message)

            def show_warning(self, message):
                self.messages.append(message)

            def show_error(self, message):
                self.messages.append(message)

            def show_info(self, message):
                self.messages.append(message)

            def pause(self):
                self.messages.append("pause")

        with TemporaryDirectory() as tmp:
            exam_file = Path(tmp) / "考试链接.json"
            exam_file.write_text(
                json.dumps(
                    [
                        {
                            "url": "https://example.com/exam/1",
                            "ai_failed_model_configs": [],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            ui = FakeUi()
            with (
                patch("core.config.EXAM_URLS_FILE", exam_file),
                patch(
                    "core.workflows.run_ai_exam_workflow",
                    new=unittest.mock.AsyncMock(return_value=0),
                ) as mock_workflow,
            ):
                handle_ai_exam(ui)

        self.assertIn(("AI考试是否自动交卷？", "N"), ui.messages)
        mock_workflow.assert_awaited_once_with(
            status_callback=ui.show_info,
            auto_submit=False,
        )

    def test_handle_ai_exam_does_not_start_when_missing_links_are_declined(self):
        from core.launcher_controller import handle_ai_exam

        class FakeUi:
            def __init__(self):
                self.messages = []

            def prompt_yes_no(self, message, default="N"):
                self.messages.append((message, default))
                return False

            def show_warning(self, message):
                self.messages.append(message)

            def pause(self):
                self.messages.append("pause")

        with TemporaryDirectory() as tmp:
            exam_file = Path(tmp) / "考试链接.json"
            ui = FakeUi()
            with (
                patch("core.config.EXAM_URLS_FILE", exam_file),
                patch(
                    "core.workflows.run_ai_exam_workflow",
                    new=unittest.mock.AsyncMock(return_value=0),
                ) as mock_workflow,
            ):
                handle_ai_exam(ui)

        self.assertIn(("是否现在粘贴考试链接？", "Y"), ui.messages)
        self.assertIn("pause", ui.messages)
        mock_workflow.assert_not_awaited()

    def test_handle_ai_exam_accepts_multiline_links_and_starts_exam(self):
        from core.exam_queue import read_exam_urls
        from core.launcher_controller import handle_ai_exam

        class FakeUi:
            def __init__(self):
                self.messages = []
                self.yes_no_answers = iter([True, False])

            def prompt_yes_no(self, message, default="N"):
                self.messages.append((message, default))
                return next(self.yes_no_answers)

            def prompt_multiline_input(self, prompts, **kwargs):
                self.messages.append((prompts, kwargs))
                return (
                    "https://example.com/exam/1\n"
                    "https://example.com/exam/2\n"
                    "https://example.com/exam/1"
                )

            def show_success(self, message):
                self.messages.append(message)

            def show_warning(self, message):
                self.messages.append(message)

            def show_error(self, message):
                self.messages.append(message)

            def show_info(self, message):
                self.messages.append(message)

            def pause(self):
                self.messages.append("pause")

        with TemporaryDirectory() as tmp:
            exam_file = Path(tmp) / "考试链接.json"
            ui = FakeUi()
            with (
                patch("core.config.EXAM_URLS_FILE", exam_file),
                patch(
                    "core.workflows.run_ai_exam_workflow",
                    new=unittest.mock.AsyncMock(return_value=0),
                ) as mock_workflow,
            ):
                handle_ai_exam(ui)

            self.assertEqual(
                read_exam_urls(exam_file),
                [
                    "https://example.com/exam/1",
                    "https://example.com/exam/2",
                ],
            )

        self.assertTrue(
            any(
                isinstance(message, str)
                and "已写入 2 条考试链接" in message
                for message in ui.messages
            )
        )
        mock_workflow.assert_awaited_once_with(
            status_callback=ui.show_info,
            auto_submit=False,
        )

    def test_handle_refresh_credential_reports_manual_browser_close(self):
        from core.launcher_controller import handle_refresh_credential
        from core.login import LoginNotCompletedError

        class FakeUi:
            def __init__(self):
                self.messages = []

            def show_info(self, message):
                self.messages.append(message)

            def show_warning(self, message):
                self.messages.append(message)

            def show_success(self, message):
                self.messages.append(message)

            def pause(self):
                self.messages.append("pause")

        state = type("State", (), {"has_credential": False, "credential_expired": True})()
        ui = FakeUi()

        with patch(
            "core.workflows.refresh_credential",
            side_effect=LoginNotCompletedError(
                "已手动关闭浏览器，未完成登录，登录凭证未更新"
            ),
        ):
            handle_refresh_credential(state, ui)

        self.assertIn("已手动关闭浏览器，未完成登录，登录凭证未更新", ui.messages)
        self.assertIn("pause", ui.messages)

    def test_handle_manual_selection_cancel_returns_to_menu(self):
        from core.abort import UserCancelRequested
        from core.launcher_controller import handle_manual_selection

        class FakeUi:
            def __init__(self):
                self.messages = []

            def prompt_multiline_input(self, _prompts):
                raise UserCancelRequested("已取消手动选择课程 / 录入链接")

            def show_warning(self, message):
                self.messages.append(message)

            def pause(self):
                self.messages.append("pause")

        ui = FakeUi()

        handle_manual_selection(["请粘贴入口链接。"], ui)

        self.assertIn("已取消手动选择课程 / 录入链接", ui.messages)
        self.assertIn("pause", ui.messages)


if __name__ == "__main__":
    unittest.main()
