import asyncio
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from core.auth.credential import CredentialMetadata
from core.state import ProjectState


class FakeProgress:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.add_task_calls = []
        self.update_calls = []
        self.refresh_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def add_task(self, description, total):
        self.add_task_calls.append((description, total))
        return "task-1"

    def update(self, task_id, advance):
        self.update_calls.append((task_id, advance))

    def refresh(self):
        self.refresh_calls += 1


class UiProgressTests(unittest.TestCase):
    def test_menu_status_contains_account_expiry_counts_and_recommendation(self):
        from rich.console import Console
        from core.ui import build_menu_status_renderable

        state = ProjectState(
            has_credential=True,
            credential_expired=False,
            learning_count=3,
            learning_failure_count=1,
            exam_count=2,
            manual_exam_count=0,
        )
        metadata = CredentialMetadata(
            saved_at="2026-07-01T10:00:00",
            expires_at="2026-07-29T10:00:00",
            account_display_name="测试用户",
            account_name="test_user",
            account_label="测试用户（test_user）",
        )
        console = Console(record=True, width=100)

        with (
            patch("core.ui.load_credential_metadata", return_value=metadata),
            patch("core.ui.datetime") as mock_datetime,
        ):
            mock_datetime.fromisoformat.side_effect = datetime.fromisoformat
            mock_datetime.now.return_value = datetime(2026, 7, 13, 10, 0, 0)
            console.print(build_menu_status_renderable(state))

        output = console.export_text()
        self.assertIn("测试用户（test_user）", output)
        self.assertIn("有效至 2026-07-29", output)
        self.assertIn("课程 3", output)
        self.assertIn("考试 2", output)
        self.assertIn("AI 自动考试", output)

    def test_credential_display_rounds_partial_day_up(self):
        from core.ui import _credential_display

        state = ProjectState(
            has_credential=True,
            credential_expired=False,
            learning_count=0,
            learning_failure_count=0,
            exam_count=0,
            manual_exam_count=0,
        )
        metadata = CredentialMetadata(
            saved_at="2026-05-18T14:34:28",
            expires_at="2026-05-20T14:34:28",
            account_display_name="测试用户",
            account_name="test_user",
            account_label="测试用户（test_user）",
        )

        with patch("core.ui.datetime") as mock_datetime:
            mock_datetime.fromisoformat.side_effect = datetime.fromisoformat
            mock_datetime.now.return_value = datetime(2026, 5, 19, 20, 0, 0)

            display = _credential_display(state, metadata)

        self.assertIn("还有 1 天", display.plain)
        self.assertIn("有效至 2026-05-20", display.plain)
        self.assertNotIn("还有 0 天", display.plain)

    def test_credential_display_treats_expiration_date_as_expired(self):
        from core.ui import _credential_display

        state = ProjectState(
            has_credential=True,
            credential_expired=False,
            learning_count=0,
            learning_failure_count=0,
            exam_count=0,
            manual_exam_count=0,
        )
        metadata = CredentialMetadata(
            saved_at="2026-04-21T14:34:28",
            expires_at="2026-05-19T14:34:28",
            account_display_name="测试用户",
            account_name="test_user",
            account_label="测试用户（test_user）",
        )

        with patch("core.ui.datetime") as mock_datetime:
            mock_datetime.fromisoformat.side_effect = datetime.fromisoformat
            mock_datetime.now.return_value = datetime(2026, 5, 19, 8, 0, 0)

            display = _credential_display(state, metadata)

        from core.ui.terminal_compat import ui_glyphs

        self.assertEqual(display.plain, f"{ui_glyphs().icon_warning} 已过期")

    def test_wait_with_progress_uses_ten_hz_rich_refresh(self):
        from core.ui import wait_with_progress

        fake_sleep = AsyncMock()
        created_progress = []

        def make_progress(*args, **kwargs):
            progress = FakeProgress(*args, **kwargs)
            created_progress.append(progress)
            return progress

        with (
            patch("core.ui.Progress", side_effect=make_progress),
            patch("asyncio.sleep", fake_sleep),
        ):
            asyncio.run(wait_with_progress(3, description="视频学习进度"))

        progress = created_progress[0]
        self.assertTrue(progress.kwargs.get("auto_refresh", False))
        self.assertEqual(progress.kwargs.get("refresh_per_second"), 10)
        self.assertTrue(progress.kwargs.get("transient"))
        self.assertEqual(progress.add_task_calls, [("视频学习进度", 3)])
        self.assertEqual(progress.update_calls, [("task-1", 1), ("task-1", 1), ("task-1", 1)])
        self.assertEqual(progress.refresh_calls, 0)
        self.assertEqual(
            [call.args[0] for call in fake_sleep.await_args_list],
            [1, 1, 1],
        )

    def test_prompt_yes_no_uses_uppercase_choices_and_case_insensitive_input(self):
        from core.ui import prompt_yes_no

        with patch("core.ui._read_console_line", return_value="y"):
            result = prompt_yes_no("是否自动交卷？", default="Y")

        self.assertTrue(result)

    def test_prompt_yes_no_uses_default_for_blank_input(self):
        from core.ui import prompt_yes_no

        with patch("core.ui._read_console_line", return_value=""):
            result = prompt_yes_no("是否自动交卷？", default="N")

        self.assertFalse(result)

    def test_prompt_multiline_input_returns_shortcut_submitted_text(self):
        from core.ui import prompt_multiline_input

        with patch(
            "core.ui._read_multiline_console_input",
            return_value="https://example.com/1\nhttps://example.com/2",
        ):
            result = prompt_multiline_input(["请粘贴入口链接。"])

        self.assertEqual(
            result,
            "https://example.com/1\nhttps://example.com/2",
        )

    def test_prompt_multiline_input_supports_escape_cancel(self):
        from core.abort import UserCancelRequested
        from core.ui import prompt_multiline_input

        with patch(
            "core.ui._read_multiline_console_input",
            side_effect=UserCancelRequested("已取消手动选择课程 / 录入链接"),
        ):
            with self.assertRaises(UserCancelRequested):
                prompt_multiline_input(["请粘贴入口链接。"])

    def test_submit_escape_sequences_support_ctrl_enter_only(self):
        from core.ui import _is_submit_escape_sequence

        self.assertTrue(_is_submit_escape_sequence("\x1b[13;5u"))
        self.assertFalse(_is_submit_escape_sequence("\x1b[A"))

    def test_erase_input_char_writes_raw_backspace_sequence(self):
        from core.ui import _erase_input_char

        chars = ["a", "b"]
        with patch("core.ui._write_console_raw") as mock_write:
            erased = _erase_input_char(chars)

        self.assertTrue(erased)
        self.assertEqual(chars, ["a"])
        mock_write.assert_called_once_with("\b \b")


if __name__ == "__main__":
    unittest.main()
