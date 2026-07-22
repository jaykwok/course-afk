import base64
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from core.discovery.reference_collector import (
    SectionResource,
    build_resource_output_name,
    collect_reference_materials,
    decode_preview_bytes,
    full_preview_url,
    is_trusted_preview_host,
    render_video_guides_markdown,
    safe_filename,
)
from core.discovery.subject_parse import extract_subject_id


class ReferenceCollectorTests(unittest.IsolatedAsyncioTestCase):
    def test_extract_subject_id_from_subject_url(self):
        self.assertEqual(
            extract_subject_id(
                "https://kc.zhixueyun.com/#/study/subject/detail/"
                "5f617bf2-c8b5-422d-a133-4064d840135c"
            ),
            "5f617bf2-c8b5-422d-a133-4064d840135c",
        )

    def test_decode_base64_pdf_preview(self):
        raw_pdf = b"%PDF-1.7\nbody"
        decoded, suffix, kind = decode_preview_bytes(base64.b64encode(raw_pdf), "txt")
        self.assertEqual(decoded, raw_pdf)
        self.assertEqual(suffix, ".pdf")
        self.assertEqual(kind, "decoded-base64-pdf")

    def test_safe_filename_removes_windows_forbidden_chars(self):
        self.assertEqual(safe_filename('A:B/C*D?"E<>|'), "A_B_C_D_E_")

    def test_resource_output_name_includes_attachment_id_to_avoid_collisions(self):
        resource = SectionResource(
            course_index=1,
            course_id="course-id",
            course_name="课程",
            topic="",
            section_index=2,
            section_name="课件",
            attachment_id="attachment-123",
            file_type="pdf",
        )

        self.assertEqual(
            build_resource_output_name(resource),
            "01_课程__02_课件__attachment-123",
        )

    def test_full_preview_url_allows_trusted_preview_hosts(self):
        self.assertTrue(is_trusted_preview_host("zhixueyun.com"))
        self.assertTrue(is_trusted_preview_host("cdn.zhixueyun.com"))
        self.assertTrue(is_trusted_preview_host("mylearning.cn"))
        self.assertTrue(is_trusted_preview_host("assets.mylearning.cn"))
        self.assertEqual(
            full_preview_url("https://cdn.zhixueyun.com/file.pdf"),
            "https://cdn.zhixueyun.com/file.pdf",
        )
        self.assertEqual(
            full_preview_url("https://assets.mylearning.cn/file.pdf"),
            "https://assets.mylearning.cn/file.pdf",
        )

    def test_full_preview_url_rejects_untrusted_absolute_hosts(self):
        self.assertFalse(is_trusted_preview_host("evilzhixueyun.com"))
        self.assertFalse(is_trusted_preview_host("zhixueyun.com.evil.test"))

        with self.assertRaisesRegex(ValueError, "白名单"):
            full_preview_url("https://evil.example.com/file.pdf")

    def test_render_video_guides_markdown_contains_segment_titles(self):
        markdown = render_video_guides_markdown(
            [
                {
                    "course_index": 1,
                    "course_name": "课程",
                    "section_index": 2,
                    "section_name": "视频",
                    "topic": "主题",
                    "attachment_id": "att",
                    "status": 200,
                    "items": [
                        {
                            "name": "知识点",
                            "beginTime": 1000,
                            "endTime": 61000,
                            "content": "总结内容",
                        }
                    ],
                }
            ]
        )
        self.assertIn("01 课程 / 02 视频", markdown)
        self.assertIn("知识点（00:01-01:01）", markdown)
        self.assertIn("总结内容", markdown)

    async def test_collect_reference_materials_records_video_guide_errors(self):
        resource = SectionResource(
            course_index=1,
            course_id="course-id",
            course_name="课程",
            topic="主题",
            section_index=1,
            section_name="视频",
            attachment_id="video-attachment",
            file_type="mp4",
            guide_study_flag=True,
        )

        class FakePage:
            async def close(self):
                return None

        class FakeContext:
            async def new_page(self):
                return FakePage()

        class FakeBrowserContext:
            async def __aenter__(self):
                return object(), FakeContext()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with TemporaryDirectory() as tmp:
            with (
                patch(
                    "core.discovery.reference_collector.create_browser_context",
                    return_value=FakeBrowserContext(),
                ),
                patch(
                    "core.discovery.reference_collector._collect_courses_from_subject_page",
                    new=AsyncMock(return_value=[object()]),
                ),
                patch(
                    "core.discovery.reference_collector.get_authorization_header",
                    new=AsyncMock(return_value="Bearer__token"),
                ),
                patch(
                    "core.discovery.reference_collector._fetch_course_infos",
                    new=AsyncMock(return_value=[{"course_id": "course-id", "data": {}}]),
                ),
                patch(
                    "core.discovery.reference_collector.collect_section_resources",
                    return_value=[resource],
                ),
                patch(
                    "core.discovery.reference_collector._fetch_video_guide",
                    new=AsyncMock(side_effect=RuntimeError("guide failed")),
                ),
            ):
                result = await collect_reference_materials(
                    [
                        "https://kc.zhixueyun.com/#/study/subject/detail/"
                        "12345678-1234-1234-1234-123456789abc"
                    ],
                    output_root=Path(tmp),
                )

            self.assertEqual(result["video_count"], 1)
            summary_path = Path(result["output_dir"]) / "video_guides_all.json"
            self.assertIn("guide failed", summary_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
