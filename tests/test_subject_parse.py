import json
import unittest
from pathlib import Path

from core.discovery.subject_parse import (
    COURSE_SECTION_TYPES,
    EXAM_SECTION_TYPES,
    URL_SECTION_TYPES,
    expand_chapter_progress,
    extract_course_links_from_chapter_progress,
    extract_subject_id,
    is_subject_detail_url,
    partition_course_and_subject_urls,
)


class SubjectParseUnitTests(unittest.TestCase):
    def test_extract_subject_id(self):
        url = (
            "https://kc.zhixueyun.com/#/study/subject/detail/"
            "1d40d4e0-a622-4535-8f02-ad108a930656"
        )
        self.assertEqual(
            extract_subject_id(url),
            "1d40d4e0-a622-4535-8f02-ad108a930656",
        )
        self.assertTrue(is_subject_detail_url(url))
        self.assertFalse(
            is_subject_detail_url(
                "https://kc.zhixueyun.com/#/study/course/detail/"
                "1d40d4e0-a622-4535-8f02-ad108a930656"
            )
        )

    def test_partition_course_and_subject_urls(self):
        course = (
            "https://kc.zhixueyun.com/#/study/course/detail/"
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        )
        subject = (
            "https://kc.zhixueyun.com/#/study/subject/detail/"
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        )
        courses, subjects = partition_course_and_subject_urls(
            [course, subject, course, subject]
        )
        self.assertEqual(courses, [course])
        self.assertEqual(subjects, [subject])

    def test_chapter_progress_maps_section_type_10_to_course(self):
        payload = [
            {
                "courseChapterSections": [
                    {
                        "id": "d5832449-44e7-41da-a593-c661f27842ed",
                        "name": "课A",
                        "sectionType": 10,
                    },
                    {
                        "id": "077a0c34-2c24-435e-8959-bc92a4e8f47a",
                        "name": "课B",
                        "sectionType": 10,
                    },
                ]
            }
        ]
        links = extract_course_links_from_chapter_progress(payload)
        self.assertEqual(
            links,
            [
                "https://kc.zhixueyun.com/#/study/course/detail/"
                "d5832449-44e7-41da-a593-c661f27842ed",
                "https://kc.zhixueyun.com/#/study/course/detail/"
                "077a0c34-2c24-435e-8959-bc92a4e8f47a",
            ],
        )
        result = expand_chapter_progress(
            payload,
            subject_url=(
                "https://kc.zhixueyun.com/#/study/subject/detail/"
                "1d40d4e0-a622-4535-8f02-ad108a930656"
            ),
        )
        self.assertEqual(result.course_urls, links)
        self.assertEqual(result.exam_urls, [])
        self.assertIsNone(result.residual_subject_url)
        self.assertEqual(result.unknown_section_types, ())

    def test_expand_keeps_subject_residual_for_unknown_section_type(self):
        subject_url = (
            "https://kc.zhixueyun.com/#/study/subject/detail/"
            "1d40d4e0-a622-4535-8f02-ad108a930656"
        )
        payload = [
            {
                "courseChapterSections": [
                    {
                        "id": "d5832449-44e7-41da-a593-c661f27842ed",
                        "sectionType": 10,
                    },
                    {
                        "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                        "sectionType": 99,
                        "name": "未知类型",
                    },
                ]
            }
        ]
        result = expand_chapter_progress(payload, subject_url=subject_url)
        self.assertEqual(
            result.course_urls,
            [
                "https://kc.zhixueyun.com/#/study/course/detail/"
                "d5832449-44e7-41da-a593-c661f27842ed"
            ],
        )
        self.assertEqual(result.exam_urls, [])
        self.assertEqual(result.residual_subject_url, subject_url)
        self.assertEqual(result.unknown_section_types, (99,))

    def test_expand_mixed_course_exam_url_from_capture_shapes(self):
        """对齐 b34e… 实勘：课+考+URL → 课/考入队，URL 触发残留。"""
        subject_url = (
            "https://kc.zhixueyun.com/#/study/subject/detail/"
            "b34ed305-1c02-4d55-8783-5e73184e0326"
        )
        payload = [
            {
                "courseChapterSections": [
                    {
                        "id": "24a514df-0e89-4630-82d2-1ba5db348afc",
                        "sectionType": 10,
                    },
                    {
                        "id": "1ce8472f-2fcf-403a-a92b-048b2496ec24",
                        "resourceId": "039d7490-4e00-4757-889b-2d277f565d55",
                        "sectionType": 9,
                    },
                    {
                        "id": "f3a83aa6-da80-4678-be46-467233a9a1c6",
                        "sectionType": 3,
                        "url": "https://example.com/survey",
                    },
                ]
            }
        ]
        result = expand_chapter_progress(payload, subject_url=subject_url)
        self.assertEqual(len(result.course_urls), 1)
        self.assertEqual(
            result.exam_urls,
            [
                "https://kc.zhixueyun.com/#/exam/exam/answer-paper/"
                "039d7490-4e00-4757-889b-2d277f565d55"
            ],
        )
        self.assertEqual(result.url_section_count, 1)
        self.assertEqual(result.residual_subject_url, subject_url)

    def test_expand_exam_section_type_9_uses_resource_id(self):
        """实勘：sectionType=9 考试，answer-paper 用 resourceId 而非小节 id。"""
        subject_url = (
            "https://kc.zhixueyun.com/#/study/subject/detail/"
            "b34ed305-1c02-4d55-8783-5e73184e0326"
        )
        payload = [
            {
                "courseChapterSections": [
                    {
                        "id": "1ce8472f-2fcf-403a-a92b-048b2496ec24",
                        "referenceId": "1ce8472f-2fcf-403a-a92b-048b2496ec24",
                        "resourceId": "039d7490-4e00-4757-889b-2d277f565d55",
                        "sectionType": 9,
                        "name": "正式考试",
                    },
                ]
            }
        ]
        result = expand_chapter_progress(payload, subject_url=subject_url)
        self.assertEqual(result.course_urls, [])
        self.assertEqual(
            result.exam_urls,
            [
                "https://kc.zhixueyun.com/#/exam/exam/answer-paper/"
                "039d7490-4e00-4757-889b-2d277f565d55"
            ],
        )
        self.assertIsNone(result.residual_subject_url)

    def test_expand_url_section_type_3_keeps_residual(self):
        subject_url = (
            "https://kc.zhixueyun.com/#/study/subject/detail/"
            "6c2f17b6-7194-41e0-afe6-a698f6c67c50"
        )
        payload = [
            {
                "courseChapterSections": [
                    {
                        "id": "d5832449-44e7-41da-a593-c661f27842ed",
                        "sectionType": 10,
                    },
                    {
                        "id": "2710af8e-1e65-47df-8bc4-0a7a2c9d1212",
                        "sectionType": 3,
                        "url": "https://docs.qq.com/doc/DS2VSZXpvWXpVZXJD",
                        "name": "外链通知",
                    },
                ]
            }
        ]
        result = expand_chapter_progress(payload, subject_url=subject_url)
        self.assertEqual(len(result.course_urls), 1)
        self.assertEqual(result.exam_urls, [])
        self.assertEqual(result.url_section_count, 1)
        self.assertEqual(result.residual_subject_url, subject_url)
        self.assertEqual(result.unknown_section_types, ())

    def test_section_type_constants_from_capture(self):
        self.assertEqual(COURSE_SECTION_TYPES, frozenset({10}))
        self.assertEqual(EXAM_SECTION_TYPES, frozenset({9}))
        self.assertEqual(URL_SECTION_TYPES, frozenset({3}))

    def test_missing_section_type_keeps_residual(self):
        subject_url = (
            "https://kc.zhixueyun.com/#/study/subject/detail/"
            "1d40d4e0-a622-4535-8f02-ad108a930656"
        )
        payload = [
            {
                "courseChapterSections": [
                    {"id": "d5832449-44e7-41da-a593-c661f27842ed"},
                ]
            }
        ]
        result = expand_chapter_progress(payload, subject_url=subject_url)
        self.assertEqual(result.course_urls, [])
        self.assertEqual(result.residual_subject_url, subject_url)
        self.assertEqual(result.unknown_section_types, (-1,))

    def test_empty_payload_keeps_residual(self):
        subject_url = (
            "https://kc.zhixueyun.com/#/study/subject/detail/"
            "1d40d4e0-a622-4535-8f02-ad108a930656"
        )
        result = expand_chapter_progress([], subject_url=subject_url)
        self.assertEqual(result.course_urls, [])
        self.assertEqual(result.residual_subject_url, subject_url)
        self.assertEqual(result.section_total, 0)

    def test_chapter_progress_from_capture_fixture(self):
        fixture = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "subject_chapter_progress.json"
        )
        if not fixture.exists():
            self.skipTest("capture fixture missing")
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        subject_url = (
            "https://kc.zhixueyun.com/#/study/subject/detail/"
            "1d40d4e0-a622-4535-8f02-ad108a930656"
        )
        result = expand_chapter_progress(payload, subject_url=subject_url)
        self.assertEqual(len(result.course_urls), 13)
        self.assertTrue(all("/study/course/detail/" in item for item in result.course_urls))
        self.assertEqual(result.exam_urls, [])
        # 实勘主题全为 sectionType=10，无残留
        self.assertIsNone(result.residual_subject_url)
        self.assertEqual(
            extract_course_links_from_chapter_progress(payload),
            result.course_urls,
        )


if __name__ == "__main__":
    unittest.main()
