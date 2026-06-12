import unittest
import json
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core.state import recommend_next_step
from core.credential import CredentialMetadata


class WorkflowStateTests(unittest.TestCase):
    def test_read_non_empty_lines_deduplicates_while_preserving_order(self):
        from core.state import read_non_empty_lines

        with TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "links.txt"
            file_path.write_text(
                "\n".join(
                    [
                        "https://example.com/exam/1",
                        "",
                        "https://example.com/exam/2",
                        "https://example.com/exam/1",
                        "https://example.com/exam/3",
                        "https://example.com/exam/2",
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                read_non_empty_lines(file_path),
                [
                    "https://example.com/exam/1",
                    "https://example.com/exam/2",
                    "https://example.com/exam/3",
                ],
            )

    def test_collect_project_state_counts_unique_links_only(self):
        from core.state import collect_project_state

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            learning_file = root / "learning.json"
            learning_failures_file = root / "failures.json"
            exam_file = root / "exam.json"
            manual_exam_file = root / "manual.json"

            learning_file.write_text(
                json.dumps(
                    [
                        {"url": "https://example.com/course/1"},
                        {"url": "https://example.com/course/1"},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            learning_failures_file.write_text(
                json.dumps(
                    [
                        {
                            "url": "https://example.com/course/failure",
                            "reason": "no_permission",
                            "reason_text": "无权限访问该学习资源",
                            "detail": {},
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            exam_file.write_text(
                json.dumps(
                    [
                        {
                            "url": "https://example.com/exam/1",
                            "ai_failed_model_configs": [],
                        },
                        {
                            "url": "https://example.com/exam/1",
                            "ai_failed_model_configs": [],
                        },
                        {
                            "url": "https://example.com/exam/2",
                            "ai_failed_model_configs": [],
                        },
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            manual_exam_file.write_text(
                json.dumps(
                    [
                        {
                            "url": "https://example.com/manual/1",
                            "reason": "manual_pending",
                            "reason_text": "测试人工考试待处理",
                            "remaining_attempts": None,
                            "threshold": None,
                            "ai_failed_model_configs": [],
                        },
                        {
                            "url": "https://example.com/manual/1",
                            "reason": "manual_pending",
                            "reason_text": "测试人工考试待处理",
                            "remaining_attempts": None,
                            "threshold": None,
                            "ai_failed_model_configs": [],
                        },
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with (
                patch("core.state.LEARNING_URLS_FILE", learning_file),
                patch("core.state.LEARNING_FAILURES_FILE", learning_failures_file),
                patch("core.state.EXAM_URLS_FILE", exam_file),
                patch("core.state.MANUAL_EXAM_FILE", manual_exam_file),
                patch("core.state.has_valid_credential", return_value=(True, False)),
            ):
                state = collect_project_state()

        self.assertEqual(state.learning_count, 1)
        self.assertEqual(state.learning_failure_count, 1)
        self.assertEqual(state.exam_count, 2)
        self.assertEqual(state.manual_exam_count, 1)

    def test_recommend_manual_course_selection_when_no_learning_links(self):
        self.assertEqual(
            recommend_next_step(
                has_credential=True,
                learning_count=0,
                exam_count=0,
                manual_exam_count=0,
            ),
            "手动选择课程 / 录入课程或考试链接",
        )

    def test_recommend_ai_exam_before_manual_selection(self):
        self.assertEqual(
            recommend_next_step(
                has_credential=True,
                learning_count=0,
                exam_count=1,
                manual_exam_count=0,
            ),
            "AI 自动考试",
        )

    def test_recommend_afk_when_learning_links_are_pending(self):
        self.assertEqual(
            recommend_next_step(
                has_credential=True,
                learning_count=1,
                exam_count=0,
                manual_exam_count=0,
            ),
            "仅挂课",
        )

    def test_has_valid_credential_treats_expiration_date_as_expired(self):
        from core.state import has_valid_credential

        metadata = CredentialMetadata(
            saved_at="2026-04-21T14:34:28",
            expires_at="2026-05-19T14:34:28",
            account_display_name="测试用户",
            account_name="test_user",
            account_label="测试用户（test_user）",
        )

        with (
            patch("core.state.load_credential_metadata", return_value=metadata),
            patch("core.credential.datetime") as mock_datetime,
        ):
            mock_datetime.fromisoformat.side_effect = datetime.fromisoformat
            mock_datetime.now.return_value = datetime(2026, 5, 19, 8, 0, 0)

            self.assertEqual(has_valid_credential(), (True, True))


if __name__ == "__main__":
    unittest.main()
