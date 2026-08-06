from __future__ import annotations

import argparse
import base64
import re
import sys
import traceback
from pathlib import Path

from core.discovery.pdf_ocr import (
    OCR_FORMAT_MARKER,
    OCR_FILE_PREFIX,
    OCR_RESULT_PREFIX,
    OCR_STATUS_PREFIX,
    expected_ocr_markdown_path,
)


def _status(message: str) -> None:
    print(f"{OCR_STATUS_PREFIX}{message}", flush=True)


def _report_success(pdf_path: Path) -> None:
    encoded = base64.urlsafe_b64encode(pdf_path.name.encode("utf-8")).decode("ascii")
    print(f"{OCR_FILE_PREFIX}{encoded.rstrip('=')}", flush=True)


def _markdown_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, tuple) and value and isinstance(value[0], str):
        return value[0]
    if isinstance(value, dict):
        for key in ("markdown_texts", "markdown_text", "text"):
            if isinstance(value.get(key), str):
                return value[key]
    raise RuntimeError("PP-StructureV3 未返回可识别的 Markdown 文本")


def _safe_image_path(document_dir: Path, relative_path: object) -> Path | None:
    relative = Path(str(relative_path).replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        return None
    target = (document_dir / relative).resolve()
    root = document_dir.resolve()
    if target != root and root not in target.parents:
        return None
    return target


def _save_markdown_images(document_dir: Path, markdown_pages: list[dict]) -> None:
    for page in markdown_pages:
        images = page.get("markdown_images") or {}
        if not isinstance(images, dict):
            continue
        for relative_path, image in images.items():
            target = _safe_image_path(document_dir, relative_path)
            if target is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            image.save(target)


def _extract_ocr_lines(value: object) -> list[str]:
    candidates: list[list[str]] = []

    def visit(item: object) -> None:
        if isinstance(item, dict):
            texts = item.get("rec_texts")
            if isinstance(texts, list):
                normalized = [
                    str(text).replace("\r", " ").replace("\n", " ").strip()
                    for text in texts
                    if str(text).strip()
                ]
                if normalized:
                    candidates.append(normalized)
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    if not candidates:
        return []
    return max(candidates, key=lambda lines: sum(len(line) for line in lines))


def _visible_text_length(markdown: str) -> int:
    without_html = re.sub(r"<[^>]+>", " ", markdown)
    without_images = re.sub(r"!\[[^]]*]\([^)]*\)", " ", without_html)
    plain = re.sub(r"[`#>*_\[\]()-]+", " ", without_images)
    return len("".join(plain.split()))


def _append_ocr_text_layer(body: str, page_lines: list[list[str]]) -> str:
    ocr_length = sum(len(line) for lines in page_lines for line in lines)
    if not ocr_length or _visible_text_length(body) >= max(80, int(ocr_length * 0.6)):
        return body
    sections = [body.rstrip(), "", "---", "", "## OCR 文字层", ""]
    for page_index, lines in enumerate(page_lines, start=1):
        if not lines:
            continue
        sections.extend(
            [f"### 第 {page_index} 页", "", "\n\n".join(lines), ""]
        )
    return "\n".join(sections).strip()


def _convert_pdf(pipeline, pdf_path: Path, output_root: Path) -> Path:
    markdown_path = expected_ocr_markdown_path(output_root.parent, pdf_path)
    document_dir = markdown_path.parent
    document_dir.mkdir(parents=True, exist_ok=True)
    markdown_pages: list[dict] = []
    page_ocr_lines: list[list[str]] = []
    for page_index, result in enumerate(
        pipeline.predict(
            input=str(pdf_path),
            format_block_content=True,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            use_seal_recognition=False,
            use_table_recognition=True,
            use_formula_recognition=False,
            use_chart_recognition=False,
        ),
        start=1,
    ):
        if page_index == 1 or page_index % 5 == 0:
            _status(f"{pdf_path.name}：已处理 {page_index} 页")
        page = result.markdown
        if isinstance(page, dict):
            markdown_pages.append(page)
        page_ocr_lines.append(_extract_ocr_lines(getattr(result, "json", {})))
    if not markdown_pages:
        raise RuntimeError("PDF 没有生成任何页面结果")
    body = _markdown_text(pipeline.concatenate_markdown_pages(markdown_pages)).strip()
    body = _append_ocr_text_layer(body, page_ocr_lines)
    content = (
        f"{OCR_FORMAT_MARKER}\n\n"
        f"# {pdf_path.stem}\n\n"
        f"> 来源文件：{pdf_path.name}（转换成功后删除本地 PDF）  \n"
        "> 解析引擎：PP-StructureV3 + PP-OCRv6_medium\n\n"
        f"{body}\n"
    )
    markdown_path.write_text(content, encoding="utf-8")
    _save_markdown_images(document_dir, markdown_pages)
    return markdown_path


def _write_index(
    output_root: Path,
    successes: list[tuple[Path, Path]],
    failures: list[tuple[Path, str]],
) -> None:
    lines = [
        "# PDF OCR Markdown 索引",
        "",
        "解析引擎：PP-StructureV3 + PP-OCRv6_medium。",
        "",
        f"成功 {len(successes)} 个，失败 {len(failures)} 个。",
        "",
    ]
    for pdf_path, markdown_path in successes:
        relative = markdown_path.relative_to(output_root).as_posix()
        lines.append(f"- [{pdf_path.stem}.md](<{relative}>)")
    if failures:
        lines.extend(["", "## 转换失败", ""])
        for pdf_path, error in failures:
            lines.append(f"- {pdf_path.name}：{error}")
    (output_root / "OCR索引.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(input_dir: Path, output_root: Path, device: str) -> int:
    pdf_files = sorted(
        path
        for path in input_dir.iterdir()
        if path.is_file() and path.suffix.lower() == ".pdf"
    )
    output_root.mkdir(parents=True, exist_ok=True)
    pending: list[Path] = []
    reused: list[tuple[Path, Path]] = []
    for pdf_path in pdf_files:
        markdown_path = expected_ocr_markdown_path(output_root.parent, pdf_path)
        if (
            markdown_path.is_file()
            and markdown_path.stat().st_size > 0
            and markdown_path.stat().st_mtime >= pdf_path.stat().st_mtime
            and OCR_FORMAT_MARKER
            in markdown_path.read_text(encoding="utf-8", errors="ignore")[:200]
        ):
            reused.append((pdf_path, markdown_path))
        else:
            pending.append(pdf_path)

    if not pending:
        _write_index(output_root, reused, [])
        for pdf_path, _ in reused:
            _report_success(pdf_path)
        print(f"{OCR_RESULT_PREFIX}0|0|{len(reused)}", flush=True)
        return 0

    _status("正在加载 PP-StructureV3 和 PP-OCRv6_medium；首次运行会下载官方模型")
    from paddleocr import PPStructureV3

    pipeline_options = {
        "text_detection_model_name": "PP-OCRv6_medium_det",
        "text_recognition_model_name": "PP-OCRv6_medium_rec",
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
        "use_seal_recognition": False,
        "use_table_recognition": True,
        "use_formula_recognition": False,
        "use_chart_recognition": False,
        "format_block_content": True,
    }
    if device and device.lower() != "auto":
        pipeline_options["device"] = device
    pipeline = PPStructureV3(
        **pipeline_options,
    )
    successes: list[tuple[Path, Path]] = []
    failures: list[tuple[Path, str]] = []
    for index, pdf_path in enumerate(pending, start=1):
        _status(f"正在转换 PDF {index}/{len(pending)}：{pdf_path.name}")
        try:
            markdown_path = _convert_pdf(pipeline, pdf_path, output_root)
        except Exception as exc:
            failures.append((pdf_path, str(exc).replace("\n", " ")[:500]))
            continue
        successes.append((pdf_path, markdown_path))
    _write_index(output_root, reused + successes, failures)
    for pdf_path, _ in reused + successes:
        _report_success(pdf_path)
    print(
        f"{OCR_RESULT_PREFIX}{len(successes)}|{len(failures)}|{len(reused)}",
        flush=True,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert downloaded PDFs to Markdown")
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--device", default="auto")
    args = parser.parse_args()
    try:
        return run(args.input_dir.resolve(), args.output_dir.resolve(), args.device)
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
