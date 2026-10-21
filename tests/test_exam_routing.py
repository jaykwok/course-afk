import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


class ExamRoutingTests(unittest.TestCase):
    def test_auto_submit_policy_uses_threshold_but_always_preserves_last_attempt(self):
        from core.exam.routing import allows_auto_submit
        for text, threshold, expected in (
            ("剩余 1 次", 0, False), ("剩余 1 次", 1, False),
            ("剩余 3 次", 3, False), ("剩余 4 次", 3, True),
            ("剩余 0 次", 1, False), ("次数未知", 1, False),
            ("不限次数", 1, True), ("已达考试次数限制", 1, False),
        ):
            with self.subTest(text=text, threshold=threshold):
                self.assertEqual(allows_auto_submit(text, threshold), expected)

    def test_old_reviews_are_restored_without_losing_protection_or_history(self):
        from core.exam.routing import requeue_ai_exam_reviews
        from core.queues.exam import read_exam_queue, write_exam_urls, record_ai_failed_model_config, has_ai_failed_model_config
        from core.queues.manual_exam import append_manual_exam_entry, read_manual_exam_queue
        with TemporaryDirectory() as tmp:
            exam_file, manual_file = Path(tmp)/"exam.json", Path(tmp)/"manual.json"
            for reason in ("ai_no_answer", "submission_unverified", "attempt_threshold", "pending_grading", "attempt_limit"):
                append_manual_exam_entry(reason, reason=reason, reason_text=reason, file_path=manual_file)
            self.assertEqual(requeue_ai_exam_reviews(exam_file=exam_file, manual_exam_file=manual_file), 5)
            self.assertEqual([e.reason for e in read_manual_exam_queue(manual_file)], [])
            entries = read_exam_queue(exam_file)
            self.assertEqual([e.url for e in entries], ["ai_no_answer", "submission_unverified", "attempt_threshold", "pending_grading", "attempt_limit"])
            self.assertTrue(all(e.requires_manual_submit for e in entries))
            write_exam_urls([e.url for e in entries], file_path=exam_file)
            record_ai_failed_model_config(entries[0].url, {"model": "test"}, file_path=exam_file)
            self.assertTrue(all(e.requires_manual_submit for e in read_exam_queue(exam_file)))
            self.assertTrue(has_ai_failed_model_config(entries[0].url, {"model": "test"}, file_path=exam_file))
            self.assertEqual(requeue_ai_exam_reviews(exam_file=exam_file, manual_exam_file=manual_file), 0)

    def test_review_migration_keeps_source_when_target_write_fails(self):
        from core.exam.routing import requeue_ai_exam_reviews
        from core.queues.manual_exam import append_manual_exam_entry, read_manual_exam_queue
        with TemporaryDirectory() as tmp:
            exam_file, manual_file = Path(tmp)/"exam.json", Path(tmp)/"manual.json"
            append_manual_exam_entry("exam", reason="ai_no_answer", reason_text="test", file_path=manual_file)
            with patch("core.exam.routing.append_exam_urls", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    requeue_ai_exam_reviews(exam_file=exam_file, manual_exam_file=manual_file)
            self.assertEqual(len(read_manual_exam_queue(manual_file)), 1)

    def test_review_migration_source_write_failure_leaves_protected_duplicate(self):
        from core.exam.routing import requeue_ai_exam_reviews
        from core.queues.exam import read_exam_queue
        from core.queues.manual_exam import append_manual_exam_entry, read_manual_exam_queue
        with TemporaryDirectory() as tmp:
            exam_file, manual_file = Path(tmp)/"exam.json", Path(tmp)/"manual.json"
            append_manual_exam_entry("exam", reason="submission_unverified", reason_text="test", file_path=manual_file)
            with patch("core.exam.routing.write_manual_exam_queue", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    requeue_ai_exam_reviews(exam_file=exam_file, manual_exam_file=manual_file)
            self.assertEqual(len(read_manual_exam_queue(manual_file)), 1)
            self.assertTrue(read_exam_queue(exam_file)[0].requires_manual_submit)
            self.assertEqual(requeue_ai_exam_reviews(exam_file=exam_file, manual_exam_file=manual_file), 1)
            self.assertEqual(read_manual_exam_queue(manual_file), [])

    def test_last_attempt_keeps_ai_link_and_disables_auto_submit(self):
        from core.exam.routing import queue_exam_url_by_attempt_text
        from core.queues.exam import append_exam_url, read_exam_urls, requires_manual_submission
        from core.queues.manual_exam import read_manual_exam_queue

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            exam_file = root / "exam.json"
            manual_file = root / "manual.json"
            url = "https://kc.zhixueyun.com/#/exam/exam/answer-paper/a"
            append_exam_url(url, file_path=exam_file)

            destination = queue_exam_url_by_attempt_text(
                url,
                "继续考试\n剩余 1 次",
                threshold=1,
                exam_file=exam_file,
                manual_exam_file=manual_file,
            )

            self.assertEqual(destination, "ai")
            self.assertEqual(read_exam_urls(exam_file), [url])
            self.assertTrue(requires_manual_submission(url, file_path=exam_file))
            entries = read_manual_exam_queue(manual_file)
            self.assertEqual(entries, [])

    def test_attempt_limit_is_recorded_as_zero_remaining(self):
        from core.exam.routing import queue_exam_url_by_attempt_text
        from core.queues.exam import read_exam_urls
        from core.queues.manual_exam import read_manual_exam_queue

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            exam_file = root / "exam.json"
            manual_file = root / "manual.json"
            url = "https://kc.zhixueyun.com/#/exam/exam/answer-paper/a"

            destination = queue_exam_url_by_attempt_text(
                url,
                "已达到了考试次数限制，因不能再次进入考试详情页",
                threshold=1,
                exam_file=exam_file,
                manual_exam_file=manual_file,
            )

            self.assertEqual(destination, "manual")
            self.assertEqual(read_exam_urls(exam_file), [])
            entries = read_manual_exam_queue(manual_file)
            self.assertEqual(entries[0].reason, "attempt_limit")
            self.assertEqual(entries[0].remaining_attempts, 0)
            self.assertEqual(entries[0].threshold, 1)

    def test_unlimited_exam_stays_in_ai_queue(self):
        from core.exam.routing import queue_exam_url_by_attempt_text
        from core.queues.exam import read_exam_urls
        from core.queues.manual_exam import read_manual_exam_urls

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            exam_file = root / "exam.json"
            manual_file = root / "manual.json"
            url = "https://kc.zhixueyun.com/#/exam/exam/answer-paper/a"

            destination = queue_exam_url_by_attempt_text(
                url,
                "开始考试 不限次数",
                threshold=1,
                exam_file=exam_file,
                manual_exam_file=manual_file,
            )

            self.assertEqual(destination, "ai")
            self.assertEqual(read_exam_urls(exam_file), [url])
            self.assertEqual(read_manual_exam_urls(manual_file), [])

    def test_unparseable_remaining_count_keeps_ai_and_disables_auto_submit(self):
        from core.exam.routing import queue_exam_url_by_attempt_text
        from core.queues.manual_exam import read_manual_exam_queue
        from core.queues.exam import read_exam_urls, requires_manual_submission

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            exam_file = root / "exam.json"
            manual_file = root / "manual.json"
            url = "https://kc.zhixueyun.com/#/exam/exam/answer-paper/a"

            destination = queue_exam_url_by_attempt_text(
                url,
                "继续考试（剩余次数未知）",
                threshold=1,
                exam_file=exam_file,
                manual_exam_file=manual_file,
            )

            self.assertEqual(destination, "ai")
            self.assertEqual(read_exam_urls(exam_file), [url])
            self.assertTrue(requires_manual_submission(url, file_path=exam_file))
            self.assertEqual(read_manual_exam_queue(manual_file), [])


if __name__ == "__main__":
    unittest.main()
