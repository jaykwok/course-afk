from __future__ import annotations

from core.browser.bootstrap import navigate_platform_page

import base64
import asyncio
import hashlib
import json
import httpx
import logging
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import urlparse, urljoin, quote

from core.browser.session import create_browser_context, is_target_closed_exception
from core.abort import UserAbortRequested, UserCancelRequested, WafBlockError, raise_for_batch_stop
from core.auth.credential import load_credential_metadata
from core.storage import write_json_atomic, write_text_atomic, write_bytes_atomic
from core.diagnostics import redact_text, redact
from core.runtime import close_safely
from core.config import REFERENCE_OUTPUT_DIR
from core.file_ops import normalize_url
from core.browser.page_auth import fetch_json, get_authorization_header
from core.browser.overlays import prepare_page_after_navigation_async
from core.discovery.subject_parse import (
    collect_course_links_from_subject_page,
    extract_subject_id,
)


GUIDE_STUDY_API = (
    "https://kc.zhixueyun.com/api/v1/course-study/guide-study/get-guide-study-info"
)
COURSE_INFO_API = "https://kc.zhixueyun.com/api/v1/course-study/course-front/info/{course_id}"
FILE_PREVIEW_API = "https://kc.zhixueyun.com/api/v1/tools-center-v2/file-cloud/preview"
DOCUMENT_FILE_TYPES = {"pdf", "doc", "docx", "ppt", "pptx"}
TRUSTED_PREVIEW_HOST_SUFFIXES = ("zhixueyun.com", "mylearning.cn")
DOCUMENT_DOWNLOAD_ATTEMPTS = 3
DOCUMENT_DOWNLOAD_TIMEOUT_MILLISECONDS = 120_000


@dataclass(frozen=True)
class SubjectCourse:
    course_id: str
    title: str
    topic: str = ""


@dataclass(frozen=True)
class SectionResource:
    course_index: int
    course_id: str
    course_name: str
    topic: str
    section_index: int
    section_name: str
    attachment_id: str
    file_type: str
    chapter_index: int | None = None
    chapter_name: str = ""
    resource_kind: str = "section"
    section_type: int | None = None
    guide_study_flag: bool = False
    total_time: int | None = None


def safe_filename(value: str, *, max_length: int = 120) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n]+', "_", value or "untitled")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return (cleaned or "untitled")[:max_length]


def normalize_resource_label(value: object, *, fallback: str = "") -> str:
    """Normalize platform labels before terminal display and filename generation."""
    normalized = re.sub(r"\s+", " ", str(value or fallback)).strip()
    return normalized or fallback


def sanitize_error_text(value: object) -> str:
    return redact_text(value)


def build_resource_output_name(resource: SectionResource) -> str:
    attachment_suffix = safe_filename(resource.attachment_id, max_length=36)
    return (
        f"{resource.course_index:02d}_"
        f"{safe_filename(resource.course_name, max_length=48)}"
        f"__{resource.section_index:02d}_"
        f"{safe_filename(resource.section_name, max_length=48)}"
        f"__{attachment_suffix}"
    )


def is_trusted_preview_host(hostname: str | None) -> bool:
    normalized = (hostname or "").strip().lower().rstrip(".")
    return any(
        normalized == suffix or normalized.endswith(f".{suffix}")
        for suffix in TRUSTED_PREVIEW_HOST_SUFFIXES
    )


def full_preview_url(url: str) -> str:
    value = str(url or "").strip()
    if not value or "\\" in value or any(ord(char) < 32 for char in value):
        raise ValueError("预览文件地址格式无效")
    if value.startswith("/") and not value.startswith("//"):
        value = ("https://kc.zhixueyun.com" if value.startswith("/api/") else "https://dianxinsafecdn.zhixueyun.com") + value
    parsed = urlparse(value)
    if parsed.scheme != "https" or not is_trusted_preview_host(parsed.hostname) or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443):
        raise ValueError("预览地址必须为允许站点的 HTTPS 地址，且不含用户信息或自定义端口")
    return value


MAX_DOCUMENT_BYTES = 128 * 1024 * 1024


def decode_preview_bytes(raw: bytes, preferred_suffix: str) -> tuple[bytes, str, str]:
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ValueError("课件超过 128 MiB 上限")
    if raw.lstrip().startswith(b"JVBER"):
        raw = base64.b64decode(re.sub(rb"\s+", b"", raw), validate=True)
        if not raw.startswith(b"%PDF-"):
            raise ValueError("Base64 内容不是 PDF")
        return raw, ".pdf", "decoded-base64-pdf"
    if raw.startswith(b"%PDF-"):
        return raw, ".pdf", "pdf-binary"
    suffix = str(preferred_suffix or "").lower().lstrip(".")
    if suffix not in DOCUMENT_FILE_TYPES:
        raise ValueError("附件扩展名不受支持")
    if suffix in {"docx", "pptx"} and raw.startswith(b"PK\x03\x04"):
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            root = "word/document.xml" if suffix == "docx" else "ppt/presentation.xml"
            if root not in archive.namelist():
                raise ValueError("Office 文件内容与扩展名不一致")
        return raw, "." + suffix, "office-zip"
    if suffix in {"doc", "ppt"} and raw.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
        return raw, "." + suffix, "office-ole"
    raise ValueError("下载内容不是受支持的课件；拒绝 HTML/登录页面或伪 PDF")


async def _download_preview(url: str, auth_header: str) -> bytes:
    current = full_preview_url(url)
    async with asyncio.timeout(DOCUMENT_DOWNLOAD_TIMEOUT_MILLISECONDS / 1000):
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            for _ in range(6):
                client.cookies.clear()
                headers = {"Accept": "*/*"}
                if urlparse(current).hostname == "kc.zhixueyun.com":
                    headers.update(Authorization=auth_header, Version="12.1.1")
                async with client.stream("GET", current, headers=headers) as response:
                    if response.is_redirect:
                        target = response.headers.get("location")
                        if not target:
                            raise ValueError("下载重定向缺少目标")
                        current = full_preview_url(urljoin(current, target))
                        continue
                    response.raise_for_status()
                    length = response.headers.get("content-length")
                    if length and int(length) > MAX_DOCUMENT_BYTES:
                        raise ValueError("课件超过 128 MiB 上限")
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_DOCUMENT_BYTES:
                            raise ValueError("课件超过 128 MiB 上限")
                        chunks.append(chunk)
                    return b"".join(chunks)
    raise ValueError("下载重定向次数超过上限")


def _format_time(milliseconds: int | None) -> str:
    if not milliseconds:
        return "00:00"
    seconds = max(0, int(milliseconds) // 1000)
    minutes, second = divmod(seconds, 60)
    return f"{minutes:02d}:{second:02d}"


def _markdown_cell(value: object) -> str:
    return normalize_resource_label(value).replace("|", "\\|")


def build_course_markdown_name(course_index: int, course_info: dict) -> str:
    data = course_info.get("data") or {}
    course_id = str(course_info.get("course_id") or "unknown")
    course_name = normalize_resource_label(
        data.get("name") or course_info.get("title"),
        fallback=course_id,
    )
    return (
        f"{course_index:02d}_{safe_filename(course_name, max_length=64)}"
        f"__{safe_filename(course_id, max_length=36)}.md"
    )


def render_course_markdown(
    course_info: dict,
    resources: list[SectionResource],
    video_records: list[dict],
    document_results: list[dict],
) -> str:
    data = course_info.get("data") or {}
    course_id = str(course_info.get("course_id") or "")
    course_name = normalize_resource_label(
        data.get("name") or course_info.get("title"),
        fallback=course_id,
    )
    lines = [
        f"# {course_name}",
        "",
        f"- 课程ID：{course_id}",
        f"- 主题：{course_info.get('topic') or ''}",
        f"- 讲师：{data.get('lecturer') or ''}",
        f"- 章节资源：{len(resources)} 个",
        "",
    ]
    if not resources:
        lines.extend(["暂无可导出的章节资源。", ""])
        return "\n".join(lines)

    video_by_attachment = {
        str(record.get("attachment_id")): record
        for record in video_records
        if record.get("course_id") == course_id
    }
    document_by_attachment = {
        str(result.get("attachment_id")): result
        for result in document_results
        if result.get("course_id") == course_id
    }
    current_chapter: tuple[int | None, str] | None = None
    for resource in resources:
        chapter_key = (resource.chapter_index, resource.chapter_name)
        if chapter_key != current_chapter:
            current_chapter = chapter_key
            chapter_label = resource.chapter_name or "课程章节"
            prefix = (
                f"{resource.chapter_index:02d} "
                if resource.chapter_index is not None
                else ""
            )
            lines.extend([f"## {prefix}{chapter_label}", ""])

        lines.extend(
            [
                f"### {resource.section_index:02d} {resource.section_name}",
                "",
                f"- 文件类型：{resource.file_type or '未知'}",
                f"- 附件ID：{resource.attachment_id}",
                f"- 资源来源：{'课程附件' if resource.resource_kind == 'course_attachment' else '课程章节'}",
                f"- 章节类型：{resource.section_type if resource.section_type is not None else '无'}",
                "",
            ]
        )

        document = document_by_attachment.get(str(resource.attachment_id))
        if document:
            if document.get("ok"):
                saved = document.get("saved") or {}
                filename = Path(saved.get("path") or "").name
                lines.extend(
                    [
                        f"- 文档：[打开 {filename}](<../docs/{filename}>)",
                        f"- 文件大小：{saved.get('bytes') or 0} 字节",
                        "",
                    ]
                )
            else:
                lines.extend(
                    [f"> 文档下载失败：{sanitize_error_text(document.get('error'))}", ""]
                )

        video = video_by_attachment.get(str(resource.attachment_id))
        if not video:
            continue
        lines.extend([f"- AI导学接口状态：{video.get('status')}", ""])
        if video.get("error"):
            lines.extend([f"> AI导学获取失败：{sanitize_error_text(video['error'])}", ""])
        items = video.get("items") or []
        if not items:
            lines.extend(["暂无导学总结内容。", ""])
            continue
        for item in items:
            title = item.get("name") or "总述"
            if item.get("beginTime") or item.get("endTime"):
                title = (
                    f"{title}（{_format_time(item.get('beginTime'))}-"
                    f"{_format_time(item.get('endTime'))}）"
                )
            lines.extend(
                [f"#### {title}", "", (item.get("content") or "").strip(), ""]
            )
    return "\n".join(lines)


def render_course_catalog_markdown(
    course_files: list[tuple[dict, str]],
    resources: list[SectionResource],
) -> str:
    resource_counts: dict[str, int] = {}
    for resource in resources:
        resource_counts[resource.course_id] = (
            resource_counts.get(resource.course_id, 0) + 1
        )
    lines = [
        "# 课程目录",
        "",
        f"共 {len(course_files)} 门可读取课程。",
        "",
        "| 序号 | 课程名称 | 课程ID | 章节资源 | 课程文件 |",
        "| ---: | --- | --- | ---: | --- |",
    ]
    for index, (info, filename) in enumerate(course_files, start=1):
        data = info.get("data") or {}
        course_id = str(info.get("course_id") or "")
        course_name = data.get("name") or info.get("title") or course_id
        lines.append(
            f"| {index} | {_markdown_cell(course_name)} | {_markdown_cell(course_id)} | "
            f"{resource_counts.get(course_id, 0)} | [打开](<courses/{filename}>) |"
        )
    return "\n".join(lines) + "\n"


def render_course_failures_markdown(failures: list[dict]) -> str:
    lines = ["# 课程详情读取失败", "", f"共 {len(failures)} 门课程无法读取。", ""]
    for index, failure in enumerate(failures, start=1):
        lines.extend(
            [
                f"## {index:02d} {failure.get('title') or failure.get('course_id')}",
                "",
                f"- 课程ID：{failure.get('course_id') or ''}",
                f"- 主题：{failure.get('topic') or ''}",
                f"- 错误：{sanitize_error_text(failure.get('error'))}",
                "",
            ]
        )
    return "\n".join(lines)


def collect_section_resources(course_infos: list[dict], *, start_index: int = 1) -> list[SectionResource]:
    resources: list[SectionResource] = []
    for course_index, info in enumerate(course_infos, start=start_index):
        course_data = info.get("data") or {}
        course_name = normalize_resource_label(
            course_data.get("name") or info.get("title"),
            fallback=info["course_id"],
        )
        section_index = 0
        seen_attachment_ids: set[str] = set()
        for chapter_index, chapter in enumerate(
            course_data.get("courseChapters") or [], start=1
        ):
            chapter_name = normalize_resource_label(chapter.get("name"))
            for section in chapter.get("courseChapterSections") or []:
                attachment_id = section.get("attachmentId") or section.get("resourceId")
                if not attachment_id:
                    continue
                attachment_id = str(attachment_id)
                seen_attachment_ids.add(attachment_id)
                section_index += 1
                resources.append(
                    SectionResource(
                        course_index=course_index,
                        course_id=info["course_id"],
                        course_name=course_name,
                        topic=normalize_resource_label(info.get("topic")),
                        section_index=section_index,
                        section_name=normalize_resource_label(
                            section.get("name") or chapter.get("name")
                        ),
                        attachment_id=attachment_id,
                        file_type=str(section.get("fileType") or "").lower(),
                        chapter_index=chapter_index,
                        chapter_name=chapter_name,
                        section_type=section.get("sectionType"),
                        guide_study_flag=bool(section.get("guideStudyFlag")),
                        total_time=section.get("totalTime"),
                    )
                )
        for attachment in course_data.get("courseAttachments") or []:
            if not isinstance(attachment, dict):
                continue
            attachment_id = str(
                attachment.get("attachmentId") or attachment.get("id") or ""
            ).strip()
            if not attachment_id or attachment_id in seen_attachment_ids:
                continue
            seen_attachment_ids.add(attachment_id)
            attachment_name = normalize_resource_label(
                attachment.get("name"), fallback=attachment_id
            )
            file_type = Path(attachment_name).suffix.lower().lstrip(".")
            section_index += 1
            resources.append(
                SectionResource(
                    course_index=course_index,
                    course_id=info["course_id"],
                    course_name=course_name,
                    topic=normalize_resource_label(info.get("topic")),
                    section_index=section_index,
                    section_name=attachment_name,
                    attachment_id=attachment_id,
                    file_type=file_type,
                    chapter_name="课程附件",
                    resource_kind="course_attachment",
                )
            )
    return resources


async def _collect_courses_from_subject_page(page, subject_url: str) -> list[SubjectCourse]:
    """优先 chapter-progress API（与 class 同款鉴权+请求），失败再扫 DOM studyBtn。"""
    unique: dict[str, SubjectCourse] = {}
    try:
        course_links = await collect_course_links_from_subject_page(page, subject_url)
        for link in course_links:
            match = re.search(
                r"/study/course/detail/([0-9a-fA-F-]{36})",
                link,
                re.IGNORECASE,
            )
            if not match:
                continue
            course_id = match.group(1)
            unique.setdefault(
                course_id,
                SubjectCourse(course_id=course_id, title=course_id, topic=""),
            )
    except (UserAbortRequested, UserCancelRequested, WafBlockError):
        raise
    except Exception:
        unique = {}

    if unique:
        return list(unique.values())

    # DOM 兜底：API 未取到课程时，从页面 studyBtn 提取
    await navigate_platform_page(page, subject_url, wait_until="load")
    await prepare_page_after_navigation_async(page)
    await page.wait_for_timeout(1500)
    courses = await page.evaluate(
        """() => Array.from(document.querySelectorAll('[id*="studyBtn-"]'))
            .map((element) => {
                const courseId = String(
                    element.getAttribute("data-resource-id") || ""
                ).trim();
                const text = (element.innerText || "")
                    .replace(/课程|\\[必修\\]|开始学习/g, " ")
                    .replace(/\\s+/g, " ")
                    .trim();
                return /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(courseId)
                    ? {course_id: courseId, title: text, topic: ""}
                    : null;
            })
            .filter(Boolean)"""
    )
    for course in courses or []:
        unique.setdefault(
            course["course_id"],
            SubjectCourse(
                course_id=course["course_id"],
                title=course.get("title") or course["course_id"],
                topic=course.get("topic") or "",
            ),
        )
    return list(unique.values())


async def _fetch_course_infos(
    page,
    courses: list[SubjectCourse],
    *,
    subject_id: str,
    auth_header: str,
    status_callback=None,
    failure_results: list[dict] | None = None,
) -> list[dict]:
    headers = {
        "Authorization": auth_header,
        "Version": "12.1.1",
        "Accept": "application/json, text/plain, */*",
    }
    infos: list[dict] = []
    for index, course in enumerate(courses, start=1):
        if status_callback:
            status_callback(f"正在读取课程详情 {index}/{len(courses)}：{course.title}")
        try:
            data = await fetch_json(
                page,
                f"{COURSE_INFO_API.format(course_id=course.course_id)}"
                f"?type=6&sourceId={subject_id}",
                headers=headers,
            )
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                data = data["data"]
            if not isinstance(data, dict) or not isinstance(data.get("courseChapters"), list):
                raise ValueError("课程详情响应缺少章节清单")
        except (UserAbortRequested, UserCancelRequested, WafBlockError):
            raise
        except Exception as exc:
            error_text = sanitize_error_text(exc)
            failure = {
                "course_id": course.course_id,
                "title": course.title,
                "topic": course.topic,
                "error": error_text,
            }
            if failure_results is not None:
                failure_results.append(failure)
            logging.warning(
                "课程详情读取失败，跳过 courseId=%s subjectId=%s: %s",
                course.course_id,
                subject_id,
                error_text,
            )
            if status_callback:
                status_callback(
                    f"课程详情读取失败，已跳过 {index}/{len(courses)}："
                    f"{course.title}（{error_text}）"
                )
            continue
        infos.append(
            {
                "course_id": course.course_id,
                "title": course.title,
                "topic": course.topic,
                "data": data,
            }
        )
    return infos


async def _download_document_resource(
    page,
    resource: SectionResource,
    *,
    docs_dir: Path,
    auth_header: str,
) -> dict:
    headers = {
        "Authorization": auth_header,
        "Version": "12.1.1",
        "Accept": "application/json, text/plain, */*",
    }
    preview = await fetch_json(
        page,
        f"{FILE_PREVIEW_API}?id={quote(resource.attachment_id, safe='')}",
        headers=headers,
    )
    if not isinstance(preview, dict) or not isinstance(preview.get("url"), str):
        raise ValueError("预览 API 响应缺少文件地址")
    data, suffix, kind = decode_preview_bytes(
        await _download_preview(preview["url"], auth_header),
        preview.get("extention") or preview.get("type") or resource.file_type,
    )
    output_path = docs_dir / f"{build_resource_output_name(resource)}{suffix}"
    write_bytes_atomic(output_path, data)
    return {
        "ok": True,
        "course_index": resource.course_index,
        "course_id": resource.course_id,
        "section_index": resource.section_index,
        "course_name": resource.course_name,
        "section_name": resource.section_name,
        "attachment_id": resource.attachment_id,
        "file_type": resource.file_type,
        "preview": {key: preview[key] for key in ("type", "extention") if key in preview},
        "saved": {"path": str(output_path), "bytes": len(data), "kind": kind, "sha256": hashlib.sha256(data).hexdigest()},
    }


async def _fetch_video_guide(page, resource: SectionResource, *, auth_header: str) -> dict:
    headers = {
        "Authorization": auth_header,
        "Version": "12.1.1",
        "Accept": "application/json, text/plain, */*",
    }
    items = await fetch_json(page, f"{GUIDE_STUDY_API}?courseId={quote(resource.course_id)}&attachmentId={quote(resource.attachment_id)}", headers=headers)
    if isinstance(items, dict):
        items = items.get("data")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ValueError("视频导学响应缺少列表")
    record = {
        "course_index": resource.course_index,
        "course_id": resource.course_id,
        "course_name": resource.course_name,
        "topic": resource.topic,
        "section_index": resource.section_index,
        "section_name": resource.section_name,
        "attachment_id": resource.attachment_id,
        "guide_study_flag": resource.guide_study_flag,
        "total_time": resource.total_time,
        "status": 200,
        "items": [],
    }
    record["items"] = items
    return record


def _write_document_index(output_dir: Path, docs_dir: Path) -> None:
    files = sorted(path for path in docs_dir.iterdir() if path.is_file())
    lines = ["# 文档索引", "", f"共 {len(files)} 个文档文件。", ""]
    for path in files:
        lines.append(
            f"- [{path.name}](<docs/{path.name}>) "
            f"({path.stat().st_size / 1024 / 1024:.1f} MB)"
        )
    write_text_atomic(output_dir / "文档索引.md", "\n".join(lines))


async def collect_reference_materials(
    subject_urls: list[str],
    *,
    output_root: Path = REFERENCE_OUTPUT_DIR,
    status_callback=None,
) -> dict:
    subject_urls_with_ids = [
        (url, subject_id)
        for url in (normalize_url(url) for url in subject_urls if url.strip())
        if (subject_id := extract_subject_id(url))
    ]
    if not subject_urls_with_ids:
        raise ValueError("未识别到有效的知学云主题详情链接")

    metadata = load_credential_metadata()
    account = (metadata.account_name or metadata.account_label) if metadata else "unbound"
    source_key = hashlib.sha256(json.dumps([account, sorted({sid for _, sid in subject_urls_with_ids})]).encode()).hexdigest()[:16]
    output_dir = output_root / f"知学云资料_{source_key}"
    docs_dir, courses_dir = output_dir / "docs", output_dir / "courses"
    docs_dir.mkdir(parents=True, exist_ok=True)
    courses_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(previous, dict) or previous.get("version") != 1:
            raise ValueError("资料清单版本不受支持")
    except FileNotFoundError:
        previous = {}
    for field in ("courses", "resources", "documents", "videos", "failures"):
        if not isinstance(previous.get(field, []), list) or any(not isinstance(item, dict) for item in previous.get(field, [])):
            raise ValueError("资料清单格式损坏，停止写入以保护已保存的结果")

    def resource_key(item):
        return item.get("course_id"), item.get("section_index"), item.get("attachment_id")

    def merge_records(previous_records, current_records, key):
        return list({**{key(item): item for item in previous_records}, **{key(item): item for item in current_records}}.values())

    cached_documents = {resource_key(item): item for item in previous.get("documents", []) if item.get("ok")}

    all_course_infos: list[dict] = []
    course_info_failures: list[dict] = []
    all_resources: list[SectionResource] = []
    document_results: list[dict] = []
    video_records: list[dict] = []

    def checkpoint(state="incomplete"):
        # A retry interrupted before visiting an old resource must retain its commit.
        committed = {**cached_documents, **{resource_key(item): item for item in document_results}}
        courses = merge_records(previous.get("courses", []), all_course_infos, lambda item: item.get("course_id"))
        resources = merge_records(previous.get("resources", []), [asdict(item) for item in all_resources], resource_key)
        videos = merge_records(previous.get("videos", []), video_records, resource_key)
        write_json_atomic(manifest_path, redact({"version": 1, "state": state,
            "subjects": [url for url, _ in subject_urls_with_ids], "courses": courses,
            "resources": resources, "documents": list(committed.values()),
            "videos": videos, "failures": course_info_failures}))
        _write_document_index(output_dir, docs_dir)
        course_files = []
        typed_resources = [SectionResource(**item) for item in resources]
        for course_index, course_info in enumerate(courses, start=1):
            filename = build_course_markdown_name(course_index, course_info)
            course_resources = [item for item in typed_resources if item.course_id == course_info["course_id"]]
            write_text_atomic(courses_dir / filename, render_course_markdown(course_info, course_resources, videos, list(committed.values())))
            course_files.append((course_info, filename))
        write_text_atomic(output_dir / "课程目录.md", render_course_catalog_markdown(course_files, typed_resources))
        write_text_atomic(output_dir / "课程详情读取失败.md", render_course_failures_markdown(course_info_failures))

    checkpoint()
    try:
        async with create_browser_context(headless=False) as (_, context):
            page = await context.new_page()
            for subject_url, subject_id in subject_urls_with_ids:
                if status_callback:
                    status_callback(f"正在打开主题详情：{subject_url}")
                try:
                    courses = await _collect_courses_from_subject_page(page, subject_url)
                    if not courses:
                        raise ValueError("未取得可导出的课程清单，需核对主题内容是否完整加载")
                    if status_callback:
                        status_callback(f"识别到 {len(courses)} 门课程")
                    auth_header = await get_authorization_header(page)
                    if not auth_header:
                        raise ValueError("未取得资料读取凭证")
                    known_courses = {item["course_id"] for item in all_course_infos}
                    courses = [course for course in courses if course.course_id not in known_courses]
                    course_infos = await _fetch_course_infos(
                        page,
                        courses,
                        subject_id=subject_id,
                        auth_header=auth_header,
                        status_callback=status_callback,
                        failure_results=course_info_failures,
                    )
                except Exception as exc:
                    raise_for_batch_stop(exc)
                    if status_callback:
                        status_callback(f"主题资料处理失败：{sanitize_error_text(exc)}")
                    if isinstance(exc, (UserCancelRequested, UserAbortRequested, WafBlockError)) or is_target_closed_exception(exc):
                        raise
                    logging.error("主题资料读取失败（%s）", type(exc).__name__)
                    course_info_failures.append({"title": subject_url, "error": sanitize_error_text(exc)})
                    checkpoint()
                    continue
                resources = collect_section_resources(course_infos, start_index=len(all_course_infos) + 1)
                all_course_infos.extend(course_infos)
                all_resources.extend(resources)
                checkpoint()

                document_resources = [
                    resource
                    for resource in resources
                    if resource.file_type in DOCUMENT_FILE_TYPES
                ]
                video_resources = [
                    resource for resource in resources if resource.file_type == "mp4"
                ]

                for index, resource in enumerate(document_resources, start=1):
                    if status_callback:
                        status_callback(
                            f"正在保存文档 {index}/{len(document_resources)}："
                            f"{resource.course_name} / {resource.section_name}"
                        )
                    result = None
                    cached = next((item for item in [*cached_documents.values(), *document_results] if item.get("ok") and item.get("attachment_id") == resource.attachment_id), None)
                    if cached:
                        saved = cached.get("saved") or {}
                        path = Path(saved.get("path") or "")
                        if path.is_file() and path.resolve().parent == docs_dir.resolve() and path.stat().st_size == saved.get("bytes"):
                            if hashlib.sha256(path.read_bytes()).hexdigest() == saved.get("sha256"):
                                document_results.append({**cached, **{key: getattr(resource, key) for key in ("course_index", "course_id", "course_name", "section_index", "section_name", "attachment_id", "file_type")}})
                                checkpoint()
                                continue
                    last_error = ""
                    for attempt in range(1, DOCUMENT_DOWNLOAD_ATTEMPTS + 1):
                        try:
                            result = await _download_document_resource(
                                page,
                                resource,
                                docs_dir=docs_dir,
                                auth_header=auth_header,
                            )
                            break
                        except Exception as exc:
                            if isinstance(exc, (UserCancelRequested, UserAbortRequested, WafBlockError)) or is_target_closed_exception(exc):
                                raise
                            last_error = sanitize_error_text(exc)
                            if attempt < DOCUMENT_DOWNLOAD_ATTEMPTS:
                                if status_callback:
                                    status_callback(
                                        f"文档下载超时或失败，准备重试 "
                                        f"{attempt + 1}/{DOCUMENT_DOWNLOAD_ATTEMPTS}："
                                        f"{resource.course_name} / {resource.section_name}"
                                    )
                                await page.wait_for_timeout(1000)
                    document_results.append(
                        result
                        or {
                            "ok": False,
                            "course_index": resource.course_index,
                            "course_id": resource.course_id,
                            "section_index": resource.section_index,
                            "course_name": resource.course_name,
                            "section_name": resource.section_name,
                            "attachment_id": resource.attachment_id,
                            "error": last_error,
                        }
                    )

                    checkpoint()

                for index, resource in enumerate(video_resources, start=1):
                    if status_callback:
                        status_callback(
                            f"正在保存视频AI导学 {index}/{len(video_resources)}："
                            f"{resource.course_name} / {resource.section_name}"
                        )
                    try:
                        record = await _fetch_video_guide(
                            page,
                            resource,
                            auth_header=auth_header,
                        )
                    except (UserAbortRequested, UserCancelRequested, WafBlockError):
                        raise
                    except Exception as exc:
                        record = {
                            "course_index": resource.course_index,
                            "course_id": resource.course_id,
                            "course_name": resource.course_name,
                            "topic": resource.topic,
                            "section_index": resource.section_index,
                            "section_name": resource.section_name,
                            "attachment_id": resource.attachment_id,
                            "guide_study_flag": resource.guide_study_flag,
                            "total_time": resource.total_time,
                            "status": None,
                            "items": [],
                            "error": sanitize_error_text(exc),
                        }
                    video_records.append(record)
                    checkpoint()
            await close_safely(page.close(), label="reference page")

    except BaseException:
        checkpoint("interrupted")
        raise

    complete = not course_info_failures and all(item.get("ok") for item in document_results) and not any(item.get("error") for item in video_records)
    checkpoint("complete" if complete else "incomplete")
    document_count = sum(1 for result in document_results if result.get("ok"))
    pdf_count = sum(
        1
        for result in document_results
        if result.get("ok")
        and Path((result.get("saved") or {}).get("path") or "").suffix.lower()
        == ".pdf"
    )
    video_with_items = sum(1 for record in video_records if record.get("items"))

    return {
        "output_dir": str(output_dir),
        "complete": complete,
        "course_count": len(all_course_infos),
        "course_failed_count": len(course_info_failures),
        "section_count": len(all_resources),
        "document_count": document_count,
        "pdf_count": pdf_count,
        "document_failed_count": sum(
            1 for result in document_results if not result.get("ok")
        ),
        "video_count": len(video_records),
        "video_with_items": video_with_items,
    }
