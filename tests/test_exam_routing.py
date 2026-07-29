import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class ExamRoutingTests(unittest.TestCase):
    def test_last_attempt_moves_existing_ai_link_to_manual_queue(self):
        from core.exam.routing import queue_exam_url_by_attempt_text
        from core.queues.exam import append_exam_url, read_exam_urls
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

            self.assertEqual(destination, "manual")
            self.assertEqual(read_exam_urls(exam_file), [])
            entries = read_manual_exam_queue(manual_file)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].reason, "attempt_threshold")
            self.assertEqual(entries[0].remaining_attempts, 1)
            self.assertEqual(entries[0].threshold, 1)

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
                "开始考试",
                threshold=1,
                exam_file=exam_file,
                manual_exam_file=manual_file,
            )

            self.assertEqual(destination, "ai")
            self.assertEqual(read_exam_urls(exam_file), [url])
            self.assertEqual(read_manual_exam_urls(manual_file), [])

    def test_unparseable_remaining_count_is_routed_to_manual(self):
        from core.exam.routing import queue_exam_url_by_attempt_text
        from core.queues.manual_exam import read_manual_exam_queue

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

            self.assertEqual(destination, "manual")
            self.assertEqual(read_manual_exam_queue(manual_file)[0].reason, "attempt_unknown")


if __name__ == "__main__":
    unittest.main()
