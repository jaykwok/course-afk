import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


MODEL_CONFIG = {
    "model": "model-a",
    "request_type": "responses",
    "web_search": False,
    "thinking": False,
    "reasoning_effort": None,
}


class ManualExamQueueTests(unittest.TestCase):
    def test_append_manual_exam_entry_records_reason_and_ai_model_state(self):
        from core.manual_exam_queue import append_manual_exam_entry, read_manual_exam_queue

        with TemporaryDirectory() as tmp:
            manual_file = Path(tmp) / "manual.json"

            append_manual_exam_entry(
                "https://example.com/exam/1",
                reason="ai_failed",
                reason_text="AI 自动考试仍未通过",
                ai_failed_model_config=MODEL_CONFIG,
                file_path=manual_file,
            )

            entries = read_manual_exam_queue(file_path=manual_file)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].url, "https://example.com/exam/1")
            self.assertEqual(entries[0].reason, "ai_failed")
            self.assertEqual(entries[0].reason_text, "AI 自动考试仍未通过")
            self.assertEqual(entries[0].ai_failed_model_configs, [MODEL_CONFIG])
            self.assertEqual(
                json.loads(manual_file.read_text(encoding="utf-8")),
                [
                    {
                        "url": "https://example.com/exam/1",
                        "reason": "ai_failed",
                        "reason_text": "AI 自动考试仍未通过",
                        "remaining_attempts": None,
                        "threshold": None,
                        "ai_failed_model_configs": [MODEL_CONFIG],
                    }
                ],
            )

    def test_append_manual_exam_entry_merges_duplicate_url_state(self):
        from core.manual_exam_queue import append_manual_exam_entry, read_manual_exam_queue

        with TemporaryDirectory() as tmp:
            manual_file = Path(tmp) / "manual.json"

            append_manual_exam_entry(
                "https://example.com/exam/1",
                reason="attempt_threshold",
                reason_text="当前考试剩余次数为 1, 小于等于 1 次",
                remaining_attempts=1,
                threshold=1,
                file_path=manual_file,
            )
            append_manual_exam_entry(
                "https://example.com/exam/1",
                reason="ai_failed",
                reason_text="AI 自动考试仍未通过",
                ai_failed_model_config=MODEL_CONFIG,
                file_path=manual_file,
            )

            entries = read_manual_exam_queue(file_path=manual_file)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].reason, "ai_failed")
            self.assertEqual(entries[0].remaining_attempts, 1)
            self.assertEqual(entries[0].threshold, 1)
            self.assertEqual(entries[0].ai_failed_model_configs, [MODEL_CONFIG])

    def test_read_manual_exam_queue_rejects_legacy_text_file(self):
        from core.manual_exam_queue import read_manual_exam_queue

        with TemporaryDirectory() as tmp:
            manual_file = Path(tmp) / "manual.json"
            manual_file.write_text("https://example.com/exam/1\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                read_manual_exam_queue(file_path=manual_file)

    def test_read_manual_exam_queue_rejects_non_list_json(self):
        from core.manual_exam_queue import read_manual_exam_queue

        with TemporaryDirectory() as tmp:
            manual_file = Path(tmp) / "manual.json"
            manual_file.write_text(
                json.dumps({"url": "https://example.com/exam/1"}),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                read_manual_exam_queue(file_path=manual_file)


if __name__ == "__main__":
    unittest.main()
