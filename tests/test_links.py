import unittest

from core.file_ops import (
    is_course_detail_url,
    is_exam_url,
    is_subject_detail_url,
    normalize_url,
)
from core.links import (
    extract_urls_from_text,
    is_ctexpert_case_pool_url,
    is_learning_zone_url,
    normalize_urls,
    split_manual_selection_urls,
    unique_urls,
)


class LinkParsingTests(unittest.TestCase):
    def test_unique_urls_preserves_order_and_skips_empty(self):
        self.assertEqual(
            unique_urls(["a", "", "b", "a", "  ", "c"]),
            ["a", "b", "c"],
        )

    def test_normalize_urls_normalizes_and_dedupes(self):
        course = (
            "https://kc.zhixueyun.com/#/study/course/detail/"
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        )
        self.assertEqual(
            normalize_urls([course + "?x=1", "  " + course + "  ", course, ""]),
            [course],
        )

    def test_extract_urls_from_mixed_text(self):
        text = (
            "A https://a.example.com/x，https://b.example.com/y;"
            "\nhttps://a.example.com/x"
        )
        self.assertEqual(
            extract_urls_from_text(text),
            ["https://a.example.com/x", "https://b.example.com/y"],
        )

    def test_is_learning_zone_url_detects_topic_link(self):
        self.assertTrue(
            is_learning_zone_url("https://kc.zhixueyun.com/#/topic/abc123")
        )
        self.assertTrue(
            is_learning_zone_url(
                "https://cms.mylearning.cn/safe/topic/resource/2025/zycp/pc.html"
            )
        )
        self.assertFalse(
            is_learning_zone_url(
                "https://kc.zhixueyun.com/#/study/course/detail/12345678-1234-1234-1234-123456789abc"
            )
        )

    def test_split_manual_selection_urls_separates_learning_zone_urls(self):
        (
            learning_urls,
            exam_urls,
            learning_zone_urls,
            train_class_urls,
            entry_urls,
        ) = split_manual_selection_urls(
            [
                "https://kc.zhixueyun.com/#/study/course/detail/12345678-1234-1234-1234-123456789abc",
                "https://kc.zhixueyun.com/#/exam/exam/answer-paper/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "https://kc.zhixueyun.com/#/topic/abc123",
                "https://kc.zhixueyun.com/#/train-new/class-detail/"
                "e8d1e9b6-f9cf-4960-bf0f-57cd34dc0ca9",
                "https://example.com/entry",
            ]
        )

        self.assertEqual(
            learning_urls,
            [
                "https://kc.zhixueyun.com/#/study/course/detail/12345678-1234-1234-1234-123456789abc"
            ],
        )
        self.assertEqual(
            exam_urls,
            [
                "https://kc.zhixueyun.com/#/exam/exam/answer-paper/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
            ],
        )
        self.assertEqual(
            learning_zone_urls,
            ["https://kc.zhixueyun.com/#/topic/abc123"],
        )
        self.assertEqual(
            train_class_urls,
            [
                "https://kc.zhixueyun.com/#/train-new/class-detail/"
                "e8d1e9b6-f9cf-4960-bf0f-57cd34dc0ca9"
            ],
        )
        self.assertEqual(entry_urls, ["https://example.com/entry"])

    def test_is_exam_url_detects_answer_paper_and_normalizes_suffix(self):
        raw_url = (
            "https://kc.zhixueyun.com/#/exam/exam/answer-paper/"
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa?from=manual"
        )

        self.assertTrue(is_exam_url(raw_url))
        self.assertEqual(
            normalize_url(raw_url),
            "https://kc.zhixueyun.com/#/exam/exam/answer-paper/"
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        )

    def test_course_and_subject_detail_url_helpers(self):
        course = (
            "https://kc.zhixueyun.com/#/study/course/detail/"
            "12345678-1234-1234-1234-123456789abc"
        )
        subject = (
            "https://kc.zhixueyun.com/#/study/subject/detail/"
            "12345678-1234-1234-1234-123456789abc"
        )
        self.assertTrue(is_course_detail_url(course))
        self.assertFalse(is_course_detail_url(subject))
        self.assertTrue(is_subject_detail_url(subject))
        self.assertFalse(is_subject_detail_url(course))
        self.assertTrue(is_course_detail_url(course + "?from=x"))
        self.assertTrue(is_subject_detail_url(subject + "?from=x"))

    def test_normalize_url_removes_standard_detail_suffix(self):
        self.assertEqual(
            normalize_url(
                "https://kc.zhixueyun.com/#/study/course/detail/"
                "12345678-1234-1234-1234-123456789abc?from=manual"
            ),
            "https://kc.zhixueyun.com/#/study/course/detail/"
            "12345678-1234-1234-1234-123456789abc",
        )

    def test_case_pool_is_routed_as_learning_collection_page(self):
        case_pool = "https://www.ctexpert.cn/expert-assist-web/casePool"
        case_pool_with_code = case_pool + "?code=temporary-code"

        self.assertTrue(is_ctexpert_case_pool_url(case_pool))
        self.assertTrue(is_learning_zone_url(case_pool_with_code))
        self.assertFalse(
            is_ctexpert_case_pool_url(
                "https://www.ctexpert.cn/expert-assist-web/coursePool"
            )
        )

        parts = split_manual_selection_urls([case_pool_with_code])
        self.assertEqual(parts, ([], [], [case_pool_with_code], [], []))

    def test_normalize_url_keeps_final_uuid_after_numeric_detail_marker(self):
        subject_raw_url = (
            "https://kc.zhixueyun.com/#/study/subject/detail/"
            "99@@b6e4e78b-78ed-4a15-8706-9b70e3667b7d"
        )
        subject_expected = (
            "https://kc.zhixueyun.com/#/study/subject/detail/"
            "b6e4e78b-78ed-4a15-8706-9b70e3667b7d"
        )
        course_raw_url = (
            "https://kc.zhixueyun.com/#/study/course/detail/"
            "99@@12345678-1234-1234-1234-123456789abc"
        )
        course_expected = (
            "https://kc.zhixueyun.com/#/study/course/detail/"
            "12345678-1234-1234-1234-123456789abc"
        )

        self.assertEqual(normalize_url(subject_raw_url), subject_expected)
        self.assertTrue(is_subject_detail_url(subject_raw_url))
        self.assertEqual(normalize_url(course_raw_url), course_expected)
        self.assertTrue(is_course_detail_url(course_raw_url))
        self.assertEqual(
            split_manual_selection_urls([subject_raw_url, course_raw_url]),
            ([subject_expected, course_expected], [], [], [], []),
        )

    def test_normalize_url_decodes_business_parameters(self):
        self.assertEqual(
            normalize_url(
                "https://kc.zhixueyun.com/#/paas-container?"
                "redirect=resource%3FbusinessType%3D2%26businessId%3D"
                "12345678-1234-1234-1234-123456789abc"
            ),
            "https://kc.zhixueyun.com/#/study/subject/detail/"
            "12345678-1234-1234-1234-123456789abc",
        )

    def test_normalize_url_maps_training_class_business_type(self):
        raw_url = (
            "https://kc.zhixueyun.com/app/wechat/#/qrScan?businessType=6&"
            "businessId=e8d1e9b6-f9cf-4960-bf0f-57cd34dc0ca9&"
            "organization=f32e65d0-fe3b-40d3-a025-4480a1808746&isThirdUrl=1"
        )

        self.assertEqual(
            normalize_url(raw_url),
            "https://kc.zhixueyun.com/#/train-new/class-detail/"
            "e8d1e9b6-f9cf-4960-bf0f-57cd34dc0ca9",
        )

    def test_normalize_url_maps_paas_container_class_id(self):
        raw_url = (
            "https://kc.zhixueyun.com/#/paas-container?"
            "paasurl=website%2F1645614858578771970%2Fdefault%3F"
            "screen%3Ddesktop%26type%3D4%26isPreview%3D0%26"
            "classId%3De8d1e9b6-f9cf-4960-bf0f-57cd34dc0ca9"
        )

        self.assertEqual(
            normalize_url(raw_url),
            "https://kc.zhixueyun.com/#/train-new/class-detail/"
            "e8d1e9b6-f9cf-4960-bf0f-57cd34dc0ca9",
        )

    def test_training_class_link_is_routed_as_train_class(self):
        training_urls = [
            (
                "https://kc.zhixueyun.com/app/wechat/#/qrScan?businessType=6&"
                "businessId=e8d1e9b6-f9cf-4960-bf0f-57cd34dc0ca9"
            ),
            (
                "https://kc.zhixueyun.com/#/paas-container?"
                "paasurl=website%2F1645614858578771970%2Fdefault%3F"
                "screen%3Ddesktop%26type%3D4%26isPreview%3D0%26"
                "classId%3De8d1e9b6-f9cf-4960-bf0f-57cd34dc0ca9"
            ),
        ]
        expected = (
            "https://kc.zhixueyun.com/#/train-new/class-detail/"
            "e8d1e9b6-f9cf-4960-bf0f-57cd34dc0ca9"
        )

        (
            learning_urls,
            exam_urls,
            learning_zone_urls,
            train_class_urls,
            entry_urls,
        ) = split_manual_selection_urls(training_urls)

        self.assertEqual(learning_urls, [])
        self.assertEqual(exam_urls, [])
        self.assertEqual(learning_zone_urls, [])
        self.assertEqual(train_class_urls, [expected])
        self.assertEqual(entry_urls, [])


if __name__ == "__main__":
    unittest.main()
