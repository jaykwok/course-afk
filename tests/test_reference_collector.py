import base64
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from core.discovery.reference_collector import (
    SectionResource,
    SubjectCourse,
    _fetch_course_infos,
    build_course_markdown_name,
    build_resource_output_name,
    collect_reference_materials,
    collect_section_resources,
    decode_preview_bytes,
    full_preview_url,
    is_trusted_preview_host,
    render_course_catalog_markdown,
    render_course_failures_markdown,
    render_course_markdown,
    sanitize_error_text,
    safe_filename,
)
from core.discovery.subject_parse import extract_subject_id


class ReferenceCollectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_course_infos_skips_invalid_course_and_continues(self):
        courses = [
            SubjectCourse(course_id="invalid-course", title="已下架课程"),
            SubjectCourse(course_id="valid-course", title="有效课程"),
        ]
        failures = []
        statuses = []

        with patch(
            "core.discovery.reference_collector.fetch_json",
            new=AsyncMock(
                side_effect=[
                    RuntimeError("请求失败 422: Invalid input."),
                    {"name": "有效课程", "courseChapters": []},
                ]
            ),
        ):
            infos = await _fetch_course_infos(
                object(),
                courses,
                subject_id="subject-id",
                auth_header="Bearer__token",
                status_callback=statuses.append,
                failure_results=failures,
            )

        self.assertEqual([item["course_id"] for item in infos], ["valid-course"])
        self.assertEqual(failures[0]["course_id"], "invalid-course")
        self.assertIn("Invalid input", failures[0]["error"])
        self.assertTrue(any("已跳过" in status for status in statuses))

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

    def test_sanitize_error_text_redacts_authorization_and_signed_url(self):
        error = (
            "Authorization: Bearer__secret-token "
            "https://cdn.test/file?auth_key=timestamp-signature&x=1"
        )
        sanitized = sanitize_error_text(error)

        self.assertNotIn("secret-token", sanitized)
        self.assertNotIn("timestamp-signature", sanitized)
        self.assertIn("Bearer__<redacted>", sanitized)
        self.assertIn("auth_key=<redacted>", sanitized)

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

    def test_resource_labels_normalize_nonbreaking_whitespace(self):
        resources = collect_section_resources(
            [
                {
                    "course_id": "course-id",
                    "data": {
                        "name": "课程\u00a0名称",
                        "courseChapters": [
                            {
                                "courseChapterSections": [
                                    {
                                        "name": "章节\u00a0标题",
                                        "attachmentId": "attachment-id",
                                        "fileType": "mp4",
                                    }
                                ]
                            }
                        ],
                    },
                }
            ]
        )

        self.assertEqual(resources[0].course_name, "课程 名称")
        self.assertEqual(resources[0].section_name, "章节 标题")
        self.assertEqual(resources[0].chapter_index, 1)

    def test_collect_section_resources_includes_course_attachments(self):
        resources = collect_section_resources(
            [
                {
                    "course_id": "course-id",
                    "data": {
                        "name": "课程",
                        "courseChapters": [],
                        "courseAttachments": [
                            {
                                "attachmentId": "attachment-id",
                                "attachmentType": "1",
                                "name": "课程讲义.pptx",
                            }
                        ],
                    },
                }
            ]
        )

        self.assertEqual(len(resources), 1)
        self.assertEqual(resources[0].attachment_id, "attachment-id")
        self.assertEqual(resources[0].file_type, "pptx")
        self.assertEqual(resources[0].chapter_name, "课程附件")
        self.assertEqual(resources[0].resource_kind, "course_attachment")

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

    def test_course_markdown_groups_chapters_sections_and_guides(self):
        video = SectionResource(
            course_index=1,
            course_id="course-id",
            course_name="课程",
            topic="主题",
            section_index=1,
            section_name="视频章节",
            attachment_id="video-att",
            file_type="mp4",
            chapter_index=1,
            chapter_name="第一章",
        )
        document = SectionResource(
            course_index=1,
            course_id="course-id",
            course_name="课程",
            topic="主题",
            section_index=2,
            section_name="课件",
            attachment_id="doc-att",
            file_type="pdf",
            chapter_index=1,
            chapter_name="第一章",
        )
        markdown = render_course_markdown(
            {
                "course_id": "course-id",
                "topic": "主题",
                "data": {"name": "课程", "lecturer": "讲师"},
            },
            [video, document],
            [
                {
                    "course_id": "course-id",
                    "course_name": "课程",
                    "section_index": 1,
                    "section_name": "视频章节",
                    "attachment_id": "video-att",
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
            ],
            [
                {
                    "ok": True,
                    "course_id": "course-id",
                    "attachment_id": "doc-att",
                    "saved": {"path": "C:/output/课件.pdf", "bytes": 123},
                }
            ],
        )
        self.assertIn("# 课程", markdown)
        self.assertIn("## 01 第一章", markdown)
        self.assertIn("### 01 视频章节", markdown)
        self.assertIn("### 02 课件", markdown)
        self.assertIn("#### 知识点（00:01-01:01）", markdown)
        self.assertIn("知识点（00:01-01:01）", markdown)
        self.assertIn("总结内容", markdown)
        self.assertIn("[打开 课件.pdf](<../docs/课件.pdf>)", markdown)

    def test_course_catalog_and_failure_markdown(self):
        info = {
            "course_id": "course-id",
            "data": {"name": "课程|名称"},
        }
        filename = build_course_markdown_name(1, info)
        catalog = render_course_catalog_markdown(
            [(info, filename)],
            [],
        )
        self.assertIn("课程\\|名称", catalog)
        self.assertIn(f"[打开](<courses/{filename}>)", catalog)
        self.assertIn(
            "Invalid input",
            render_course_failures_markdown(
                [{"course_id": "bad", "error": "Invalid input"}]
            ),
        )

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
            output_dir = Path(result["output_dir"])
            course_files = list((output_dir / "courses").glob("*.md"))
            self.assertEqual(len(course_files), 1)
            self.assertIn("guide failed", course_files[0].read_text(encoding="utf-8"))
            self.assertFalse((output_dir / "README.md").exists())
            self.assertEqual(list(output_dir.rglob("*.json")), [])
            self.assertFalse((output_dir / "video_guides").exists())
            for name in (
                "课程目录.md",
                "课程详情读取失败.md",
                "文档索引.md",
            ):
                self.assertTrue((output_dir / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
