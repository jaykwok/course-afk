import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class LearningQueueTests(unittest.TestCase):
    def test_append_learning_url_writes_json_entries_and_deduplicates(self):
        from core.learning_queue import append_learning_url, read_learning_queue

        with TemporaryDirectory() as tmp:
            learning_file = Path(tmp) / "learning.json"

            append_learning_url("https://example.com/course/1", file_path=learning_file)
            append_learning_url("https://example.com/course/1", file_path=learning_file)
            append_learning_url("https://example.com/course/2", file_path=learning_file)

            self.assertEqual(
                [entry.url for entry in read_learning_queue(file_path=learning_file)],
                ["https://example.com/course/1", "https://example.com/course/2"],
            )
            self.assertEqual(
                json.loads(learning_file.read_text(encoding="utf-8")),
                [
                    {"url": "https://example.com/course/1"},
                    {"url": "https://example.com/course/2"},
                ],
            )

    def test_write_learning_urls_removes_completed_urls_and_can_delete_empty_file(self):
        from core.learning_queue import write_learning_urls

        with TemporaryDirectory() as tmp:
            learning_file = Path(tmp) / "learning.json"

            write_learning_urls(
                [
                    "https://example.com/course/1",
                    "https://example.com/course/1",
                    "https://example.com/course/2",
                ],
                file_path=learning_file,
            )
            write_learning_urls(
                ["https://example.com/course/2"],
                file_path=learning_file,
            )

            self.assertEqual(
                json.loads(learning_file.read_text(encoding="utf-8")),
                [{"url": "https://example.com/course/2"}],
            )

            write_learning_urls([], file_path=learning_file, keep_file=False)

            self.assertFalse(learning_file.exists())

    def test_record_learning_failure_records_reason_and_merges_duplicate_urls(self):
        from core.learning_queue import record_learning_failure, read_learning_failures

        with TemporaryDirectory() as tmp:
            failures_file = Path(tmp) / "failures.json"

            record_learning_failure(
                "https://example.com/course/1",
                reason="retryable_error",
                reason_text="课程处理失败，稍后重试",
                file_path=failures_file,
            )
            record_learning_failure(
                "https://example.com/course/1",
                reason="no_permission",
                reason_text="无权限访问该学习资源",
                detail={"source": "subject"},
                file_path=failures_file,
            )

            entries = read_learning_failures(file_path=failures_file)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].url, "https://example.com/course/1")
            self.assertEqual(entries[0].reason, "no_permission")
            self.assertEqual(entries[0].reason_text, "无权限访问该学习资源")
            self.assertEqual(entries[0].detail, {"source": "subject"})
            self.assertEqual(
                json.loads(failures_file.read_text(encoding="utf-8")),
                [
                    {
                        "url": "https://example.com/course/1",
                        "reason": "no_permission",
                        "reason_text": "无权限访问该学习资源",
                        "detail": {"source": "subject"},
                    }
                ],
            )

    def test_remove_learning_failure_deletes_matching_url_and_can_delete_empty_file(self):
        from core.learning_queue import record_learning_failure, remove_learning_failure

        with TemporaryDirectory() as tmp:
            failures_file = Path(tmp) / "failures.json"

            record_learning_failure(
                "https://example.com/course/1",
                reason="url_type_pending",
                reason_text="URL 类型学习等待复查",
                file_path=failures_file,
            )
            remove_learning_failure(
                "https://example.com/course/1",
                file_path=failures_file,
                keep_file=False,
            )

            self.assertFalse(failures_file.exists())

    def test_read_learning_queue_rejects_legacy_text_file(self):
        from core.learning_queue import read_learning_queue

        with TemporaryDirectory() as tmp:
            learning_file = Path(tmp) / "learning.json"
            learning_file.write_text("https://example.com/course/1\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                read_learning_queue(file_path=learning_file)

    def test_read_learning_failures_rejects_non_list_json(self):
        from core.learning_queue import read_learning_failures

        with TemporaryDirectory() as tmp:
            failures_file = Path(tmp) / "failures.json"
            failures_file.write_text(
                json.dumps({"url": "https://example.com/course/1"}),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                read_learning_failures(file_path=failures_file)


if __name__ == "__main__":
    unittest.main()
