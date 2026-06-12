import unittest

from core.file_ops import is_exam_url, normalize_url
from core.links import (
    extract_urls_from_text,
    is_learning_zone_url,
    split_manual_selection_urls,
)


class LinkParsingTests(unittest.TestCase):
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
            entry_urls,
        ) = split_manual_selection_urls(
            [
                "https://kc.zhixueyun.com/#/study/course/detail/12345678-1234-1234-1234-123456789abc",
                "https://kc.zhixueyun.com/#/exam/exam/answer-paper/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "https://kc.zhixueyun.com/#/topic/abc123",
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

    def test_normalize_url_removes_standard_detail_suffix(self):
        self.assertEqual(
            normalize_url(
                "https://kc.zhixueyun.com/#/study/course/detail/"
                "12345678-1234-1234-1234-123456789abc?from=manual"
            ),
            "https://kc.zhixueyun.com/#/study/course/detail/"
            "12345678-1234-1234-1234-123456789abc",
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


if __name__ == "__main__":
    unittest.main()
