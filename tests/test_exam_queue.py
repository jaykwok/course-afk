import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core.tasks.store import write_queue


MODEL_CONFIG_A = {
    "model": "model-a",
    "request_type": "responses",
    "web_search": False,
    "thinking": False,
    "reasoning_effort": None,
    "base_url": None,
    "provider": None,
    "output_mode": None,
    "prompt_version": None,
    "temperature": None,
    "account": None,
}

MODEL_CONFIG_A_WITH_WEB = {
    "model": "model-a",
    "request_type": "responses",
    "web_search": True,
    "thinking": False,
    "reasoning_effort": None,
    "base_url": None,
    "provider": None,
    "output_mode": None,
    "prompt_version": None,
    "temperature": None,
    "account": None,
}


class ExamQueueTests(unittest.TestCase):
    def test_append_exam_url_writes_json_entries_and_deduplicates(self):
        from core.queues.exam import append_exam_url, read_exam_queue

        with TemporaryDirectory() as tmp:
            exam_file = Path(tmp) / "exam.json"

            append_exam_url("https://example.com/exam/1", file_path=exam_file)
            append_exam_url("https://example.com/exam/1", file_path=exam_file)
            append_exam_url("https://example.com/exam/2", file_path=exam_file)

            self.assertEqual(
                [entry.url for entry in read_exam_queue(file_path=exam_file)],
                ["https://example.com/exam/1", "https://example.com/exam/2"],
            )
            self.assertEqual(
                json.loads(exam_file.read_text(encoding="utf-8")),
                [
                    {
                        "url": "https://example.com/exam/1",
                        "ai_failed_model_configs": [],
                    },
                    {
                        "url": "https://example.com/exam/2",
                        "ai_failed_model_configs": [],
                    },
                ],
            )

    def test_append_exam_urls_returns_only_new_urls(self):
        from core.queues.exam import append_exam_urls, read_exam_urls

        with TemporaryDirectory() as tmp:
            exam_file = Path(tmp) / "exam.json"

            added = append_exam_urls(
                [
                    "https://example.com/exam/1",
                    "https://example.com/exam/1",
                    "https://example.com/exam/2",
                ],
                file_path=exam_file,
            )

            self.assertEqual(
                added,
                [
                    "https://example.com/exam/1",
                    "https://example.com/exam/2",
                ],
            )
            self.assertEqual(read_exam_urls(exam_file), added)

    def test_remove_exam_url_preserves_other_entries(self):
        from core.queues.exam import append_exam_urls, read_exam_urls, remove_exam_url

        with TemporaryDirectory() as tmp:
            exam_file = Path(tmp) / "exam.json"
            append_exam_urls(
                [
                    "https://example.com/exam/1",
                    "https://example.com/exam/2",
                ],
                file_path=exam_file,
            )

            self.assertTrue(
                remove_exam_url("https://example.com/exam/1", file_path=exam_file)
            )
            self.assertEqual(
                read_exam_urls(exam_file),
                ["https://example.com/exam/2"],
            )

    def test_read_exam_queue_returns_json_entries(self):
        from core.queues.exam import read_exam_queue

        with TemporaryDirectory() as tmp:
            exam_file = Path(tmp) / "exam.json"
            write_queue(
                exam_file,
                [
                    {
                        "url": "https://example.com/exam/1",
                        "ai_failed_model_configs": [],
                    },
                    {
                        "url": "https://example.com/exam/2",
                        "ai_failed_model_configs": [MODEL_CONFIG_A],
                    },
                ],
            )

            entries = read_exam_queue(file_path=exam_file)

            self.assertEqual(
                [(entry.url, entry.ai_failed_model_configs) for entry in entries],
                [
                    ("https://example.com/exam/1", []),
                    ("https://example.com/exam/2", [MODEL_CONFIG_A]),
                ],
            )

    def test_write_exam_urls_preserves_failed_models_for_retained_urls(self):
        from core.queues.exam import read_exam_queue, write_exam_urls

        with TemporaryDirectory() as tmp:
            exam_file = Path(tmp) / "exam.json"
            write_queue(
                exam_file,
                [
                    {
                        "url": "https://example.com/exam/1",
                        "ai_failed_model_configs": [MODEL_CONFIG_A],
                    },
                    {
                        "url": "https://example.com/exam/2",
                        "ai_failed_model_configs": [],
                    },
                ],
            )

            write_exam_urls(["https://example.com/exam/1"], file_path=exam_file)

            self.assertEqual(
                [
                    (entry.url, entry.ai_failed_model_configs)
                    for entry in read_exam_queue(file_path=exam_file)
                ],
                [("https://example.com/exam/1", [MODEL_CONFIG_A])],
            )

    def test_failed_model_config_matches_exact_runtime_options(self):
        from core.queues.exam import (
            has_ai_failed_model_config,
            record_ai_failed_model_config,
        )

        with TemporaryDirectory() as tmp:
            exam_file = Path(tmp) / "exam.json"
            url = "https://example.com/exam/1"

            record_ai_failed_model_config(url, MODEL_CONFIG_A, file_path=exam_file)

            self.assertTrue(
                has_ai_failed_model_config(url, MODEL_CONFIG_A, file_path=exam_file)
            )
            self.assertFalse(
                has_ai_failed_model_config(
                    url,
                    MODEL_CONFIG_A_WITH_WEB,
                    file_path=exam_file,
                )
            )


if __name__ == "__main__":
    unittest.main()
