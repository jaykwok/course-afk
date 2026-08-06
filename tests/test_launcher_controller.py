import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


class LauncherControllerTests(unittest.TestCase):
    def test_handle_show_output_state_groups_failures_and_can_requeue(self):
        from core.app.launcher_controller import handle_show_output_state
        from core.queues.learning import (
            read_learning_failures,
            read_learning_urls,
            record_learning_failure,
            write_learning_urls,
        )

        class FakeUi:
            def __init__(self):
                self.summaries = []
                self.messages = []
                self.paused = 0

            def show_summary(self, title, rows):
                self.summaries.append((title, list(rows)))

            def prompt_yes_no(self, _message, default="N"):
                return True

            def show_success(self, message):
                self.messages.append(message)

            def pause(self):
                self.paused += 1

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            learning = root / "课程链接.json"
            exam = root / "考试链接.json"
            manual = root / "人工考试链接.json"
            failures = root / "挂课失败链接.json"
            write_learning_urls([], file_path=learning)
            exam.write_text("[]", encoding="utf-8")
            manual.write_text("[]", encoding="utf-8")
            record_learning_failure(
                "https://kc.zhixueyun.com/#/study/course/detail/"
                "11111111-1111-1111-1111-111111111111",
                reason="retryable_error",
                reason_text="失败",
                file_path=failures,
            )
            record_learning_failure(
                "https://kc.zhixueyun.com/#/study/course/detail/"
                "22222222-2222-2222-2222-222222222222",
                reason="no_permission",
                reason_text="无权限",
                file_path=failures,
            )
            # 脏数据：展示状态时会 prune 掉
            record_learning_failure(
                "https://kc.zhixueyun.com/#/study/course/detail/a",
                reason="unknown_learning_type",
                reason_text="测试占位",
                file_path=failures,
            )

            ui = FakeUi()
            with patch(
                "core.config.LEARNING_FAILURES_FILE",
                failures,
            ):
                handle_show_output_state(exam, learning, manual, ui)

            self.assertEqual(ui.paused, 1)
            self.assertTrue(ui.summaries)
            labels = [row[0] for row in ui.summaries[0][1]]
            self.assertIn("失败·可重试错误", labels)
            self.assertIn("失败·可重试", labels)
            self.assertEqual(len(read_learning_urls(file_path=learning)), 1)
            # 无权限保留 + 可重试已 requeue 移除 + 脏数据 prune
            self.assertEqual(len(read_learning_failures(file_path=failures)), 1)
            self.assertTrue(any("重新加入" in m for m in ui.messages))
            self.assertTrue(any("无效失败链接" in m for m in ui.messages))

    def test_choose_learning_zone_mode_returns_manual_when_no_learning_zone_urls(self):
        from core.app.launcher_controller import choose_learning_zone_mode

        self.assertEqual(
            choose_learning_zone_mode([], prompt_choice_func=lambda *args, **kwargs: 1),
            "manual",
        )

    def test_choose_learning_zone_mode_returns_auto_when_user_selects_first_option(self):
        from core.app.launcher_controller import choose_learning_zone_mode

        self.assertEqual(
            choose_learning_zone_mode(
                ["https://cms.mylearning.cn/safe/topic/resource/2025/zycp/pc.html"],
                prompt_choice_func=lambda *args, **kwargs: 1,
            ),
            "auto",
        )

    def test_choose_learning_zone_mode_returns_manual_when_user_selects_second_option(self):
        from core.app.launcher_controller import choose_learning_zone_mode

        self.assertEqual(
            choose_learning_zone_mode(
                ["https://cms.mylearning.cn/safe/topic/resource/2025/zycp/pc.html"],
                prompt_choice_func=lambda *args, **kwargs: 2,
            ),
            "manual",
        )

    def test_maybe_delete_empty_exam_queue_file_deletes_without_prompt(self):
        from core.app.launcher_controller import _maybe_delete_empty_exam_queue_file

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
        from core.app.launcher_controller import _maybe_delete_empty_exam_queue_file

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
        from core.app.launcher_controller import _maybe_delete_empty_learning_queue_file

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
        from core.app.launcher_controller import _maybe_delete_empty_learning_queue_file

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
        from core.app.launcher_controller import handle_ai_exam

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
                patch("core.config.is_ai_configured", return_value=True),
                patch(
                    "core.app.workflows.run_ai_exam_workflow",
                    new=unittest.mock.AsyncMock(return_value=0),
                ) as mock_workflow,
            ):
                handle_ai_exam(ui)

        self.assertIn(("AI考试是否自动交卷？", "N"), ui.messages)
        mock_workflow.assert_awaited_once_with(
            status_callback=ui.show_info,
            auto_submit=False,
        )

    def test_handle_ai_exam_warns_when_ai_not_configured(self):
        from core.app.launcher_controller import handle_ai_exam

        class FakeUi:
            def __init__(self):
                self.messages = []

            def show_warning(self, message):
                self.messages.append(message)

            def show_info(self, message):
                self.messages.append(message)

            def pause(self):
                self.messages.append("pause")

        ui = FakeUi()
        with (
            patch("core.config.is_ai_configured", return_value=False),
            patch(
                "core.app.workflows.run_ai_exam_workflow",
                new=unittest.mock.AsyncMock(),
            ) as mock_workflow,
        ):
            handle_ai_exam(ui)

        self.assertTrue(
            any("AI 配置" in message for message in ui.messages), ui.messages
        )
        self.assertIn("pause", ui.messages)
        mock_workflow.assert_not_awaited()

    def test_handle_ai_exam_does_not_start_when_missing_links_are_declined(self):
        from core.app.launcher_controller import handle_ai_exam

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
                patch("core.config.is_ai_configured", return_value=True),
                patch(
                    "core.app.workflows.run_ai_exam_workflow",
                    new=unittest.mock.AsyncMock(return_value=0),
                ) as mock_workflow,
            ):
                handle_ai_exam(ui)

        self.assertIn(("是否现在粘贴考试链接？", "Y"), ui.messages)
        self.assertIn("pause", ui.messages)
        mock_workflow.assert_not_awaited()

    def test_handle_ai_exam_accepts_multiline_links_and_starts_exam(self):
        from core.queues.exam import read_exam_urls
        from core.app.launcher_controller import handle_ai_exam

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
                patch("core.config.is_ai_configured", return_value=True),
                patch(
                    "core.app.workflows.run_ai_exam_workflow",
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
        from core.app.launcher_controller import handle_refresh_credential
        from core.auth.login import LoginNotCompletedError

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
            "core.app.workflows.refresh_credential",
            side_effect=LoginNotCompletedError(
                "已手动关闭浏览器，未完成登录，登录凭证未更新"
            ),
        ):
            handle_refresh_credential(state, ui)

        self.assertIn("已手动关闭浏览器，未完成登录，登录凭证未更新", ui.messages)
        self.assertIn("pause", ui.messages)

    def test_handle_manual_selection_cancel_returns_to_menu(self):
        from core.abort import UserCancelRequested
        from core.app.launcher_controller import handle_manual_selection

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

    def test_handle_manual_selection_confirms_link_categories_before_browser(self):
        from core.app.launcher_controller import handle_manual_selection

        class FakeUi:
            def __init__(self):
                self.confirmation = None
                self.messages = []

            def prompt_multiline_input(self, _prompts):
                return "\n".join(
                    (
                        "https://kc.zhixueyun.com/#/study/course/detail/12345678-1234-1234-1234-123456789abc",
                        "https://kc.zhixueyun.com/#/exam/exam/answer-paper/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                        "https://cms.mylearning.cn/safe/topic/resource/2025/zycp/pc.html",
                        "https://example.com/entry",
                    )
                )

            def prompt_summary_confirmation(
                self, title, rows, message, *, default
            ):
                self.confirmation = (title, dict(rows), message, default)
                return False

            def show_warning(self, message):
                self.messages.append(message)

        ui = FakeUi()
        with patch(
            "core.app.workflows.run_manual_course_selection",
            new=unittest.mock.AsyncMock(),
        ) as workflow:
            handle_manual_selection(["请粘贴入口链接。"], ui)

        title, rows, message, default = ui.confirmation
        self.assertEqual(title, "链接解析确认")
        self.assertEqual(rows["有效链接（去重）"], "4")
        self.assertEqual(rows["课程 / 主题链接"], "1")
        self.assertEqual(rows["考试链接"], "1")
        self.assertEqual(rows["学习专区链接"], "1")
        self.assertEqual(rows["培训班链接（自动解析）"], "0")
        self.assertEqual(rows["其他入口链接"], "1")
        self.assertIn("继续处理", message)
        self.assertEqual(default, "Y")
        workflow.assert_not_awaited()

    def test_handle_manual_selection_shows_ok_summary_after_browser(self):
        from core.app.launcher_controller import handle_manual_selection

        class FakeUi:
            def __init__(self):
                self.result_summary = None
                self.waited_handle = None

            def prompt_multiline_input(self, _prompts):
                return "https://example.com/entry"

            def prompt_summary_confirmation(self, *_args, **_kwargs):
                return True

            def prompt_choice(self, *_args, **_kwargs):
                return 2

            def show_info(self, _message):
                pass

            def prepare_pause_with_summary(self, title, rows, message):
                self.result_summary = (title, dict(rows), message)
                return "prepared-result"

            def wait_prepared_prompt(self, handle):
                self.waited_handle = handle

        ui = FakeUi()
        result = {
            "input_url_count": 1,
            "direct_learning_count": 0,
            "direct_exam_count": 0,
            "learning_zone_url_count": 0,
            "learning_zone_parsed_count": 0,
            "train_class_url_count": 0,
            "train_class_parsed_count": 0,
            "entry_url_count": 1,
            "manual_record_count": 2,
            "manual_exam_record_count": 1,
            "learning_total": 3,
            "exam_total": 2,
        }
        with patch(
            "core.app.workflows.run_manual_course_selection",
            new=unittest.mock.AsyncMock(return_value=result),
        ) as workflow:
            handle_manual_selection(["请粘贴入口链接。"], ui)

        workflow.assert_awaited_once_with(
            "https://example.com/entry",
            learning_zone_mode="manual",
            status_callback=ui.show_info,
            result_ready_callback=unittest.mock.ANY,
        )
        title, rows, message = ui.result_summary
        self.assertEqual(title, "链接解析完成")
        self.assertEqual(rows["浏览器记录的学习链接"], "2")
        self.assertEqual(rows["浏览器记录的考试链接"], "1")
        self.assertEqual(rows["当前学习链接总数"], "3")
        self.assertEqual(rows["当前考试链接总数"], "2")
        self.assertIn("确认结果", message)
        self.assertEqual(ui.waited_handle, "prepared-result")

    def test_handle_reference_collection_cancel_returns_to_menu(self):
        from core.abort import UserCancelRequested
        from core.app.launcher_controller import handle_reference_collection

        class FakeUi:
            def __init__(self):
                self.messages = []

            def prompt_multiline_input(self, _prompts, **_kwargs):
                raise UserCancelRequested("已取消保存课程课件 / AI导学资料")

            def show_warning(self, message):
                self.messages.append(message)

            def pause(self):
                self.messages.append("pause")

        ui = FakeUi()

        handle_reference_collection(ui)

        self.assertIn("已取消保存课程课件 / AI导学资料", ui.messages)
        self.assertIn("pause", ui.messages)

    def test_handle_reference_collection_reports_invalid_subject_url(self):
        from core.app.launcher_controller import handle_reference_collection

        class FakeUi:
            def __init__(self):
                self.messages = []

            def prompt_multiline_input(self, _prompts, **_kwargs):
                return "https://example.com/not-a-subject"

            def show_warning(self, message):
                self.messages.append(message)

            def show_info(self, message):
                self.messages.append(message)

            def pause(self):
                self.messages.append("pause")

        ui = FakeUi()

        with patch(
            "core.app.workflows.run_reference_collection_workflow",
            new=unittest.mock.AsyncMock(side_effect=ValueError("未识别到有效的知学云学习专区链接")),
        ):
            handle_reference_collection(ui)

        self.assertIn("未识别到有效的知学云学习专区链接", ui.messages)
        self.assertIn("pause", ui.messages)

    def test_handle_reference_collection_shows_summary(self):
        from core.app.launcher_controller import handle_reference_collection

        class FakeUi:
            def __init__(self):
                self.messages = []
                self.summary = None

            def prompt_multiline_input(self, _prompts, **_kwargs):
                return (
                    "https://kc.zhixueyun.com/#/study/subject/detail/"
                    "12345678-1234-1234-1234-123456789abc"
                )

            def show_info(self, message):
                self.messages.append(message)

            def show_summary(self, title, rows):
                self.summary = (title, rows)

            def pause(self):
                self.messages.append("pause")

        ui = FakeUi()
        result = {
            "output_dir": "D:/ChinaTelecom/course-afk/参考资料/知学云资料_20260623_181800",
            "course_count": 2,
            "section_count": 3,
            "document_count": 1,
            "document_failed_count": 0,
            "video_count": 2,
            "video_with_items": 1,
        }

        with patch(
            "core.app.workflows.run_reference_collection_workflow",
            new=unittest.mock.AsyncMock(return_value=result),
        ) as mock_workflow:
            handle_reference_collection(ui)

        mock_workflow.assert_awaited_once_with(
            [
                "https://kc.zhixueyun.com/#/study/subject/detail/"
                "12345678-1234-1234-1234-123456789abc"
            ],
            status_callback=ui.show_info,
        )
        self.assertEqual(ui.summary[0], "课程资料保存结果")
        self.assertIn(("输出目录", result["output_dir"]), ui.summary[1])
        self.assertIn(("文档保存成功", "1"), ui.summary[1])
        self.assertIn(("有AI导学内容的视频", "1"), ui.summary[1])
        self.assertIn("pause", ui.messages)

    def test_handle_reference_collection_can_convert_downloaded_pdfs(self):
        from core.app.launcher_controller import handle_reference_collection

        class FakeUi:
            def __init__(self):
                self.messages = []
                self.summary = None

            def prompt_multiline_input(self, _prompts, **_kwargs):
                return (
                    "https://kc.zhixueyun.com/#/study/subject/detail/"
                    "12345678-1234-1234-1234-123456789abc"
                )

            def prompt_yes_no(self, message, default="N"):
                self.messages.append((message, default))
                return True

            def begin_operation(self, title, message):
                self.messages.append((title, message))

            def show_info(self, message):
                self.messages.append(message)

            def show_warning(self, message):
                self.messages.append(message)

            def show_summary(self, title, rows):
                self.summary = (title, rows)

            def pause(self):
                self.messages.append("pause")

        ui = FakeUi()
        collection_result = {
            "output_dir": "D:/course-afk/data/references/run",
            "course_count": 2,
            "section_count": 3,
            "document_count": 2,
            "pdf_count": 2,
            "document_failed_count": 0,
            "video_count": 1,
            "video_with_items": 1,
        }
        ocr_result = {
            "ocr_output_dir": "D:/course-afk/data/references/run/ocr",
            "pdf_count": 2,
            "ocr_converted_count": 2,
            "ocr_failed_count": 0,
            "ocr_reused_count": 0,
            "ocr_linked_count": 2,
            "pdf_deleted_count": 2,
        }

        with (
            patch(
                "core.app.workflows.run_reference_collection_workflow",
                new=unittest.mock.AsyncMock(return_value=collection_result),
            ),
            patch(
                "core.app.workflows.run_pdf_markdown_workflow",
                new=unittest.mock.AsyncMock(return_value=ocr_result),
            ) as mock_ocr,
        ):
            handle_reference_collection(ui)

        mock_ocr.assert_awaited_once_with(
            collection_result["output_dir"],
            status_callback=ui.show_info,
        )
        self.assertIn(
            ("已下载 2 个 PDF，是否使用 PP-OCRv6 转换为 Markdown？", "N"),
            ui.messages,
        )
        self.assertIn(("PDF 转 Markdown", "已完成"), ui.summary[1])
        self.assertIn(("PDF 新转换", "2"), ui.summary[1])
        self.assertIn(("已删除 PDF 源文件", "2"), ui.summary[1])

    def test_handle_reference_collection_can_skip_pdf_conversion(self):
        from core.app.launcher_controller import handle_reference_collection

        class FakeUi:
            def __init__(self):
                self.messages = []
                self.summary = None

            def prompt_multiline_input(self, _prompts, **_kwargs):
                return (
                    "https://kc.zhixueyun.com/#/study/subject/detail/"
                    "12345678-1234-1234-1234-123456789abc"
                )

            def prompt_yes_no(self, message, default="N"):
                self.messages.append((message, default))
                return False

            def begin_operation(self, title, message):
                self.messages.append((title, message))

            def show_info(self, message):
                self.messages.append(message)

            def show_summary(self, title, rows):
                self.summary = (title, rows)

            def pause(self):
                self.messages.append("pause")

        ui = FakeUi()
        collection_result = {
            "output_dir": "D:/course-afk/data/references/run",
            "course_count": 1,
            "section_count": 1,
            "document_count": 1,
            "pdf_count": 1,
            "document_failed_count": 0,
            "video_count": 0,
            "video_with_items": 0,
        }

        with (
            patch(
                "core.app.workflows.run_reference_collection_workflow",
                new=unittest.mock.AsyncMock(return_value=collection_result),
            ),
            patch(
                "core.app.workflows.run_pdf_markdown_workflow",
                new=unittest.mock.AsyncMock(),
            ) as mock_ocr,
        ):
            handle_reference_collection(ui)

        mock_ocr.assert_not_awaited()
        self.assertIn(("PDF 转 Markdown", "已跳过"), ui.summary[1])
        self.assertNotIn(("已删除 PDF 源文件", "1"), ui.summary[1])


if __name__ == "__main__":
    unittest.main()
