from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import os
import re
import sys
import sysconfig
from pathlib import Path


OCR_STATUS_PREFIX = "COURSE_AFK_OCR_STATUS:"
OCR_RESULT_PREFIX = "COURSE_AFK_OCR_RESULT:"
OCR_FILE_PREFIX = "COURSE_AFK_OCR_FILE:"
OCR_MODEL_NAME = "PP-OCRv6_medium"
OCR_FORMAT_MARKER = "<!-- course-afk-ocr-format: 2 -->"


class PdfOcrUnavailable(RuntimeError):
    """Raised when the optional PaddleOCR runtime is not installed."""


class PdfOcrRuntimeError(RuntimeError):
    """Raised when the PaddleOCR worker cannot finish conversion."""


def find_pdf_files(output_dir: Path) -> list[Path]:
    docs_dir = Path(output_dir) / "docs"
    if not docs_dir.is_dir():
        return []
    return sorted(
        path for path in docs_dir.iterdir() if path.is_file() and path.suffix.lower() == ".pdf"
    )


def ocr_document_dir_name(pdf_path: Path) -> str:
    digest = hashlib.sha1(pdf_path.name.encode("utf-8")).hexdigest()[:10]
    stem = re.sub(r'[\\/:*?"<>|\r\n]+', "_", pdf_path.stem)
    stem = re.sub(r"\s+", " ", stem).strip(" .") or "document"
    return f"{stem[:72]}__{digest}"


def expected_ocr_markdown_path(output_dir: Path, pdf_path: Path) -> Path:
    return Path(output_dir) / "ocr" / ocr_document_dir_name(pdf_path) / "内容.md"


def missing_ocr_dependencies() -> list[str]:
    return [
        package
        for package in ("paddle", "paddleocr")
        if importlib.util.find_spec(package) is None
    ]


def ensure_ocr_dependencies() -> None:
    missing = missing_ocr_dependencies()
    if missing:
        raise PdfOcrUnavailable(
            "缺少 PDF OCR 可选依赖："
            f"{', '.join(missing)}。请先按 README 的“PDF 转 Markdown”章节安装 "
            "requirements-ocr.txt 和适合本机的 PaddlePaddle CPU/GPU 后端。"
        )


def nvidia_dll_directories(site_packages: Path | None = None) -> list[str]:
    if os.name != "nt" and site_packages is None:
        return []
    if site_packages is None:
        site_packages = Path(sysconfig.get_paths()["purelib"])
    nvidia_root = Path(site_packages) / "nvidia"
    if not nvidia_root.is_dir():
        return []

    directories = {dll_path.parent for dll_path in nvidia_root.rglob("*.dll")}
    return [
        str(path)
        for path in sorted(
            directories,
            key=lambda path: (
                0 if any(part.lower().startswith("cu13") for part in path.parts) else 1,
                str(path).lower(),
            ),
        )
    ]


def _configure_worker_dll_path(worker_env: dict[str, str]) -> None:
    dll_directories = nvidia_dll_directories()
    if not dll_directories:
        return
    existing_path = worker_env.get("PATH", "")
    worker_env["PATH"] = os.pathsep.join(
        [*dll_directories, *([existing_path] if existing_path else [])]
    )


def _insert_ocr_links(course_text: str, output_dir: Path, pdf_files: list[Path]) -> str:
    updated = course_text
    for pdf_path in pdf_files:
        markdown_path = expected_ocr_markdown_path(output_dir, pdf_path)
        if not markdown_path.is_file():
            continue
        document_line = f"- 文档：[打开 {pdf_path.name}](<../docs/{pdf_path.name}>)"
        ocr_relative = markdown_path.relative_to(output_dir).as_posix()
        markdown_name = f"{pdf_path.stem}.md"
        markdown_line = f"- 文档：[打开 {markdown_name}](<../{ocr_relative}>)"
        if document_line in updated:
            document_position = updated.index(document_line)
            section_position = updated.rfind("\n### ", 0, document_position)
            if section_position < 0:
                section_position = 0
            section_prefix = updated[section_position:document_position]
            updated_section_prefix = section_prefix.replace(
                "- 文件类型：pdf", "- 文件类型：md", 1
            )
            if updated_section_prefix != section_prefix:
                updated = (
                    updated[:section_position]
                    + updated_section_prefix
                    + updated[document_position:]
                )
            pattern = re.escape(document_line) + r"(?:\n- 文件大小：\d+ 字节)?"
            updated = re.sub(pattern, markdown_line, updated, count=1)
    return updated


def update_course_markdown_ocr_links(
    output_dir: Path,
    pdf_files: list[Path] | None = None,
) -> int:
    output_dir = Path(output_dir)
    if pdf_files is None:
        pdf_files = find_pdf_files(output_dir)
    linked_count = 0
    courses_dir = output_dir / "courses"
    if not courses_dir.is_dir():
        return linked_count
    for course_path in sorted(courses_dir.glob("*.md")):
        original = course_path.read_text(encoding="utf-8")
        updated = _insert_ocr_links(original, output_dir, pdf_files)
        if updated == original:
            continue
        course_path.write_text(updated, encoding="utf-8")
        linked_count += sum(
            1
            for pdf_path in pdf_files
            if f"- 文档：[打开 {pdf_path.stem}.md]" in updated
            and f"- 文档：[打开 {pdf_path.stem}.md]" not in original
        )
    return linked_count


def update_document_index_ocr_links(output_dir: Path, pdf_files: list[Path]) -> int:
    index_path = Path(output_dir) / "文档索引.md"
    if not index_path.is_file():
        return 0
    original = index_path.read_text(encoding="utf-8")
    lines = original.splitlines()
    updated_count = 0
    for index, line in enumerate(lines):
        for pdf_path in pdf_files:
            markdown_path = expected_ocr_markdown_path(output_dir, pdf_path)
            prefix = f"- [{pdf_path.name}](<docs/{pdf_path.name}>)"
            if not markdown_path.is_file() or not line.startswith(prefix):
                continue
            relative = markdown_path.relative_to(output_dir).as_posix()
            lines[index] = f"- [{pdf_path.stem}.md](<{relative}>) (OCR Markdown)"
            updated_count += 1
            break
    if updated_count:
        index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return updated_count


def delete_converted_pdf_files(output_dir: Path, pdf_files: list[Path]) -> int:
    deleted_count = 0
    for pdf_path in pdf_files:
        markdown_path = expected_ocr_markdown_path(output_dir, pdf_path)
        if not markdown_path.is_file() or markdown_path.stat().st_size == 0:
            continue
        pdf_path.unlink()
        deleted_count += 1
    return deleted_count


def _parse_worker_result(line: str) -> tuple[int, int, int] | None:
    if not line.startswith(OCR_RESULT_PREFIX):
        return None
    try:
        converted, failed, reused = line.removeprefix(OCR_RESULT_PREFIX).split("|")
        return int(converted), int(failed), int(reused)
    except (TypeError, ValueError):
        return None


def _parse_worker_file(line: str) -> str | None:
    if not line.startswith(OCR_FILE_PREFIX):
        return None
    encoded = line.removeprefix(OCR_FILE_PREFIX)
    try:
        padding = "=" * (-len(encoded) % 4)
        return base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


async def convert_downloaded_pdfs_to_markdown(
    output_dir: Path | str,
    *,
    status_callback=None,
) -> dict:
    output_dir = Path(output_dir).resolve()
    pdf_files = find_pdf_files(output_dir)
    if not pdf_files:
        return {
            "ocr_output_dir": str(output_dir / "ocr"),
            "pdf_count": 0,
            "ocr_converted_count": 0,
            "ocr_failed_count": 0,
            "ocr_reused_count": 0,
            "ocr_linked_count": 0,
            "pdf_deleted_count": 0,
        }

    ensure_ocr_dependencies()
    if status_callback:
        status_callback(
            f"检测到 {len(pdf_files)} 个 PDF，准备使用 PP-StructureV3 + "
            f"{OCR_MODEL_NAME} 转换为 Markdown"
        )

    project_root = Path(__file__).resolve().parents[2]
    command = [
        sys.executable,
        "-u",
        "-m",
        "core.discovery.pdf_ocr_worker",
        "--input-dir",
        str(output_dir / "docs"),
        "--output-dir",
        str(output_dir / "ocr"),
        "--device",
        os.getenv("COURSE_AFK_OCR_DEVICE", "auto"),
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0)
    worker_env = os.environ.copy()
    worker_env["PYTHONUTF8"] = "1"
    worker_env["PYTHONIOENCODING"] = "utf-8"
    _configure_worker_dll_path(worker_env)
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(project_root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        creationflags=creationflags,
        env=worker_env,
    )
    output_tail: list[str] = []
    worker_result: tuple[int, int, int] | None = None
    successful_pdf_names: set[str] = set()
    assert process.stdout is not None
    while True:
        raw_line = await process.stdout.readline()
        if not raw_line:
            break
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        parsed = _parse_worker_result(line)
        if parsed is not None:
            worker_result = parsed
            continue
        successful_file = _parse_worker_file(line)
        if successful_file is not None:
            successful_pdf_names.add(successful_file)
            continue
        if line.startswith(OCR_STATUS_PREFIX):
            if status_callback:
                status_callback(line.removeprefix(OCR_STATUS_PREFIX))
            continue
        output_tail.append(line)
        output_tail = output_tail[-20:]

    return_code = await process.wait()
    if return_code != 0:
        detail = "\n".join(output_tail[-8:]) or f"子进程退出码 {return_code}"
        raise PdfOcrRuntimeError(f"PDF OCR 转换失败：{detail}")

    if worker_result is None:
        converted = sum(
            expected_ocr_markdown_path(output_dir, path).is_file() for path in pdf_files
        )
        failed = len(pdf_files) - converted
        reused = 0
    else:
        converted, failed, reused = worker_result
    successful_pdf_files = [
        path for path in pdf_files if path.name in successful_pdf_names
    ]
    linked = update_course_markdown_ocr_links(output_dir, successful_pdf_files)
    update_document_index_ocr_links(output_dir, successful_pdf_files)
    deleted = delete_converted_pdf_files(output_dir, successful_pdf_files)
    return {
        "ocr_output_dir": str(output_dir / "ocr"),
        "pdf_count": len(pdf_files),
        "ocr_converted_count": converted,
        "ocr_failed_count": failed,
        "ocr_reused_count": reused,
        "ocr_linked_count": linked,
        "pdf_deleted_count": deleted,
    }
