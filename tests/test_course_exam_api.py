import unittest
from unittest.mock import AsyncMock, patch


COURSE_ID = "b2d0d9e9-311f-42e6-a050-6d9f8d34243e"
EXAM_ID = "e89b337f-b33a-47c7-b510-fa3cf5cfe3ed"


def course_payload():
    return {
        "courseChapters": [
            {
                "courseChapterSections": [
                    {
                        "sectionType": 6,
                        "resourceId": "video-id",
                        "name": "视频",
                    },
                    {
                        "sectionType": 9,
                        "resourceId": EXAM_ID,
                        "name": "课程考试",
                    },
                ]
            }
        ]
    }


class CourseExamApiParsingTests(unittest.TestCase):
    def test_extracts_section_type_9_resource_id_as_exam_url(self):
        from core.learning.exam_api import extract_course_exam_sections

        sections = extract_course_exam_sections(course_payload())

        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].exam_id, EXAM_ID)
        self.assertEqual(
            sections[0].url,
            f"https://kc.zhixueyun.com/#/exam/exam/answer-paper/{EXAM_ID}",
        )

    def test_scaled_score_is_recognized_as_passed(self):
        from core.learning.exam_api import evaluate_course_exam_state

        state = evaluate_course_exam_state(
            {
                "allowExamTimes": 2,
                "examedTimes": 1,
                "passScore": 3,
                "examRegist": {"topScore": 400},
                "paperClass": {"totalScore": 500},
            },
            {
                "allowExamTimes": 2,
                "examedTimes": 1,
                "examRecord": {"isFinished": 1, "score": 400},
                "paperClass": {"totalScore": 500},
                "passScore": 3,
            },
        )

        self.assertTrue(state.passed)
        self.assertEqual(state.remaining_attempts, 1)

    def test_unfinished_exam_reports_remaining_attempts(self):
        from core.learning.exam_api import evaluate_course_exam_state

        state = evaluate_course_exam_state(
            {"allowExamTimes": 3, "examedTimes": 1, "passScore": 60},
            {"allowExamTimes": 3, "examedTimes": 1, "examRecord": None},
        )

        self.assertFalse(state.passed)
        self.assertFalse(state.pending_grading)
        self.assertEqual(state.remaining_attempts, 2)


class CourseExamApiQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_exam_queues_direct_paper_url_by_remaining_attempts(self):
        from core.learning.exam_api import queue_course_exams_from_api

        page = type(
            "Page",
            (),
            {"url": f"https://kc.zhixueyun.com/#/study/course/detail/{COURSE_ID}"},
        )()
        basic = {
            "id": EXAM_ID,
            "allowExamTimes": 3,
            "examedTimes": 1,
            "passScore": 60,
        }
        user = {
            "id": EXAM_ID,
            "allowExamTimes": 3,
            "examedTimes": 1,
            "examRecord": None,
        }

        with (
            patch(
                "core.learning.exam_api.get_authorization_header",
                new=AsyncMock(return_value="Bearer__token"),
            ),
            patch(
                "core.learning.exam_api.fetch_json",
                new=AsyncMock(side_effect=[course_payload(), [basic], user]),
            ),
            patch(
                "core.learning.exam_api.queue_exam_url_by_attempt_text",
                return_value="ai",
            ) as mock_queue,
        ):
            result = await queue_course_exams_from_api(page)

        self.assertIsNotNone(result)
        self.assertEqual(result.discovered, 1)
        self.assertEqual(result.ai_queued, 1)
        mock_queue.assert_called_once_with(
            f"https://kc.zhixueyun.com/#/exam/exam/answer-paper/{EXAM_ID}",
            "剩余 2 次",
            threshold=1,
        )

    async def test_passed_exam_is_not_queued(self):
        from core.learning.exam_api import queue_course_exams_from_api

        page = type(
            "Page",
            (),
            {"url": f"https://kc.zhixueyun.com/#/study/course/detail/{COURSE_ID}"},
        )()
        basic = {
            "id": EXAM_ID,
            "allowExamTimes": 2,
            "examedTimes": 1,
            "passScore": 3,
            "examRegist": {"topScore": 400},
            "paperClass": {"totalScore": 500},
        }
        user = {
            "id": EXAM_ID,
            "allowExamTimes": 2,
            "examedTimes": 1,
            "passScore": 3,
            "examRecord": {"isFinished": 1, "score": 400},
            "paperClass": {"totalScore": 500},
        }

        with (
            patch(
                "core.learning.exam_api.get_authorization_header",
                new=AsyncMock(return_value="Bearer__token"),
            ),
            patch(
                "core.learning.exam_api.fetch_json",
                new=AsyncMock(side_effect=[course_payload(), [basic], user]),
            ),
            patch(
                "core.learning.exam_api.queue_exam_url_by_attempt_text"
            ) as mock_queue,
        ):
            result = await queue_course_exams_from_api(page)

        self.assertIsNotNone(result)
        self.assertEqual(result.completed, 1)
        mock_queue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
