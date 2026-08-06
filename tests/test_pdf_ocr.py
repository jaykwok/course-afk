import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core.discovery.pdf_ocr import (
    expected_ocr_markdown_path,
    delete_converted_pdf_files,
    nvidia_dll_directories,
    ocr_document_dir_name,
    update_document_index_ocr_links,
    update_course_markdown_ocr_links,
)
from core.discovery.pdf_ocr_worker import (
    _append_ocr_text_layer,
    _convert_pdf,
    _extract_ocr_lines,
    _markdown_text,
)


class PdfOcrTests(unittest.TestCase):
    def test_nvidia_cuda_13_dll_directory_has_priority(self):
        with TemporaryDirectory() as tmp:
            site_packages = Path(tmp)
            cuda_13_dir = site_packages / "nvidia" / "cu13" / "bin" / "x86_64"
            common_dir = site_packages / "nvidia" / "cudnn" / "bin"
            cuda_13_dir.mkdir(parents=True)
            common_dir.mkdir(parents=True)
            (cuda_13_dir / "cublas64_13.dll").write_bytes(b"dll")
            (common_dir / "cudnn64_9.dll").write_bytes(b"dll")

            directories = nvidia_dll_directories(site_packages)

            self.assertEqual(directories[0], str(cuda_13_dir))
            self.assertIn(str(common_dir), directories)

    def test_ocr_document_dir_name_is_stable_and_bounded(self):
        pdf_path = Path(("很长的课程名称" * 30) + ".pdf")

        first = ocr_document_dir_name(pdf_path)
        second = ocr_document_dir_name(pdf_path)

        self.assertEqual(first, second)
        self.assertLessEqual(len(first), 84)
        self.assertRegex(first, r"__[0-9a-f]{10}$")

    def test_update_course_markdown_adds_link_for_converted_pdf_only(self):
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            docs_dir = output_dir / "docs"
            courses_dir = output_dir / "courses"
            docs_dir.mkdir()
            courses_dir.mkdir()
            pdf_path = docs_dir / "课件.pdf"
            docx_path = docs_dir / "讲义.docx"
            pdf_path.write_bytes(b"%PDF-test")
            docx_path.write_bytes(b"docx")
            markdown_path = expected_ocr_markdown_path(output_dir, pdf_path)
            markdown_path.parent.mkdir(parents=True)
            markdown_path.write_text("# OCR", encoding="utf-8")
            course_path = courses_dir / "课程.md"
            course_path.write_text(
                "### 01 课件\n\n- 文件类型：pdf\n"
                "- 文档：[打开 课件.pdf](<../docs/课件.pdf>)\n"
                "- 文件大小：123 字节\n"
                "### 02 讲义\n\n- 文件类型：docx\n"
                "- 文档：[打开 讲义.docx](<../docs/讲义.docx>)\n",
                encoding="utf-8",
            )
            index_path = output_dir / "文档索引.md"
            index_path.write_text(
                "# 文档索引\n\n"
                "- [课件.pdf](<docs/课件.pdf>) (0.1 MB)\n"
                "- [讲义.docx](<docs/讲义.docx>) (0.1 MB)\n",
                encoding="utf-8",
            )

            linked = update_course_markdown_ocr_links(output_dir)
            content = course_path.read_text(encoding="utf-8")

            self.assertEqual(linked, 1)
            self.assertIn("- 文档：[打开 课件.md](<../ocr/", content)
            self.assertNotIn("打开 课件.pdf", content)
            self.assertIn("- 文件类型：md", content)
            self.assertNotIn("- 文件大小：123 字节", content)
            self.assertIn("打开 讲义.docx", content)

            index_updates = update_document_index_ocr_links(output_dir, [pdf_path])
            index_content = index_path.read_text(encoding="utf-8")
            self.assertEqual(index_updates, 1)
            self.assertIn("[课件.md](<ocr/", index_content)
            self.assertNotIn("[课件.pdf]", index_content)
            self.assertIn("[讲义.docx]", index_content)

            deleted = delete_converted_pdf_files(output_dir, [pdf_path])
            self.assertEqual(deleted, 1)
            self.assertFalse(pdf_path.exists())

    def test_convert_pdf_writes_single_llm_markdown(self):
        class FakeResult:
            markdown = {
                "markdown_texts": "页面内容",
                "markdown_images": {},
            }
            json = {"overall_ocr_res": {"rec_texts": ["标题", "正文内容"]}}

        class FakePipeline:
            def predict(self, **_kwargs):
                return [FakeResult()]

            def concatenate_markdown_pages(self, pages):
                self.pages = pages
                return "## 第一页\n\n页面内容"

        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            docs_dir = output_dir / "docs"
            ocr_dir = output_dir / "ocr"
            docs_dir.mkdir()
            pdf_path = docs_dir / "课件.pdf"
            pdf_path.write_bytes(b"%PDF-test")

            markdown_path = _convert_pdf(FakePipeline(), pdf_path, ocr_dir)
            content = markdown_path.read_text(encoding="utf-8")

            self.assertIn("# 课件", content)
            self.assertIn("PP-StructureV3 + PP-OCRv6_medium", content)
            self.assertIn("## 第一页", content)
            self.assertNotIn("{", content)

    def test_sparse_structured_markdown_gets_ocr_text_layer(self):
        payload = {
            "overall_ocr_res": {
                "rec_texts": ["互联网政务应用安全管理规定", "第一章 总则"]
            }
        }
        lines = _extract_ocr_lines(payload)
        body = _append_ocr_text_layer(
            '<img src="imgs/page.jpg" alt="Image" />',
            [lines],
        )

        self.assertEqual(lines[0], "互联网政务应用安全管理规定")
        self.assertIn("## OCR 文字层", body)
        self.assertIn("### 第 1 页", body)
        self.assertIn("第一章 总则", body)

    def test_markdown_text_accepts_current_and_tuple_return_shapes(self):
        self.assertEqual(_markdown_text("正文"), "正文")
        self.assertEqual(_markdown_text(("正文", [])), "正文")
        self.assertEqual(_markdown_text({"markdown_texts": "正文"}), "正文")


if __name__ == "__main__":
    unittest.main()
