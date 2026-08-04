from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from core.browser.session import create_browser_context
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
    if url.startswith(("http://", "https://")):
        hostname = urlparse(url).hostname
        if not is_trusted_preview_host(hostname):
            raise ValueError(f"预览文件地址域名不在白名单内: {hostname or url}")
        return url
    if url.startswith("/api/"):
        return f"https://kc.zhixueyun.com{url}"
    return f"https://dianxinsafecdn.zhixueyun.com{url}"


def decode_preview_bytes(raw: bytes, preferred_suffix: str) -> tuple[bytes, str, str]:
    head = raw[:20].decode("utf-8", errors="ignore").strip()
    if head.startswith("JVBER"):
        return base64.b64decode(re.sub(rb"\s+", b"", raw)), ".pdf", "decoded-base64-pdf"
    if raw.startswith(b"%PDF"):
        return raw, ".pdf", "pdf-binary"
    suffix = f".{preferred_suffix.lower().lstrip('.') or 'bin'}"
    return raw, suffix, f"raw-{suffix.lstrip('.')}"


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
                f"- 章节类型：{resource.section_type}",
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
                lines.extend([f"> 文档下载失败：{document.get('error') or ''}", ""])

        video = video_by_attachment.get(str(resource.attachment_id))
        if not video:
            continue
        lines.extend([f"- AI导学接口状态：{video.get('status')}", ""])
        if video.get("error"):
            lines.extend([f"> AI导学获取失败：{video['error']}", ""])
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
                f"- 错误：{failure.get('error') or ''}",
                "",
            ]
        )
    return "\n".join(lines)


def collect_section_resources(course_infos: list[dict]) -> list[SectionResource]:
    resources: list[SectionResource] = []
    for course_index, info in enumerate(course_infos, start=1):
        course_data = info.get("data") or {}
        course_name = normalize_resource_label(
            course_data.get("name") or info.get("title"),
            fallback=info["course_id"],
        )
        section_index = 0
        for chapter_index, chapter in enumerate(
            course_data.get("courseChapters") or [], start=1
        ):
            chapter_name = normalize_resource_label(chapter.get("name"))
            for section in chapter.get("courseChapterSections") or []:
                attachment_id = section.get("attachmentId") or section.get("resourceId")
                if not attachment_id:
                    continue
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
    except Exception:
        unique = {}

    if unique:
        return list(unique.values())

    # DOM 兜底：API 未取到课程时，从页面 studyBtn 提取
    await page.goto(subject_url, wait_until="load")
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
        except Exception as exc:
            failure = {
                "course_id": course.course_id,
                "title": course.title,
                "topic": course.topic,
                "error": str(exc),
            }
            if failure_results is not None:
                failure_results.append(failure)
            logging.warning(
                "课程详情读取失败，跳过 courseId=%s subjectId=%s: %s",
                course.course_id,
                subject_id,
                exc,
            )
            if status_callback:
                status_callback(
                    f"课程详情读取失败，已跳过 {index}/{len(courses)}："
                    f"{course.title}（{exc}）"
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
        f"{FILE_PREVIEW_API}?id={resource.attachment_id}",
        headers=headers,
    )
    response = await page.context.request.get(
        full_preview_url(preview["url"]),
        headers={
            "Authorization": auth_header,
            "Version": "12.1.1",
            "Accept": "*/*",
        },
    )
    if not response.ok:
        raise RuntimeError(f"下载失败 {response.status}: {preview['url']}")
    data, suffix, kind = decode_preview_bytes(
        await response.body(),
        preview.get("extention") or preview.get("type") or resource.file_type,
    )
    output_path = docs_dir / f"{build_resource_output_name(resource)}{suffix}"
    output_path.write_bytes(data)
    return {
        "ok": True,
        "course_index": resource.course_index,
        "course_id": resource.course_id,
        "section_index": resource.section_index,
        "course_name": resource.course_name,
        "section_name": resource.section_name,
        "attachment_id": resource.attachment_id,
        "file_type": resource.file_type,
        "preview": preview,
        "saved": {"path": str(output_path), "bytes": len(data), "kind": kind},
    }


async def _fetch_video_guide(page, resource: SectionResource, *, auth_header: str) -> dict:
    headers = {
        "Authorization": auth_header,
        "Version": "12.1.1",
        "Accept": "application/json, text/plain, */*",
    }
    response = await page.context.request.get(
        f"{GUIDE_STUDY_API}?courseId={resource.course_id}"
        f"&attachmentId={resource.attachment_id}",
        headers=headers,
    )
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
        "status": response.status,
        "items": [],
    }
    if response.ok:
        record["items"] = await response.json()
    else:
        record["error"] = (await response.text())[:300]
    return record


def _write_document_index(output_dir: Path, docs_dir: Path) -> None:
    files = sorted(path for path in docs_dir.iterdir() if path.is_file())
    lines = ["# 文档索引", "", f"共 {len(files)} 个文档文件。", ""]
    for path in files:
        lines.append(
            f"- [{path.name}](<docs/{path.name}>) "
            f"({path.stat().st_size / 1024 / 1024:.1f} MB)"
        )
    (output_dir / "文档索引.md").write_text("\n".join(lines), encoding="utf-8")


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
        raise ValueError("未识别到有效的知学云学习专区链接")

    output_dir = output_root / f"知学云资料_{datetime.now():%Y%m%d_%H%M%S}"
    docs_dir = output_dir / "docs"
    courses_dir = output_dir / "courses"
    docs_dir.mkdir(parents=True, exist_ok=True)
    courses_dir.mkdir(parents=True, exist_ok=True)

    all_course_infos: list[dict] = []
    course_info_failures: list[dict] = []
    all_resources: list[SectionResource] = []
    document_results: list[dict] = []
    video_records: list[dict] = []

    async with create_browser_context(headless=False) as (_, context):
        page = await context.new_page()
        for subject_url, subject_id in subject_urls_with_ids:
            if status_callback:
                status_callback(f"正在打开学习专区：{subject_url}")
            try:
                courses = await _collect_courses_from_subject_page(page, subject_url)
                if status_callback:
                    status_callback(f"识别到 {len(courses)} 门课程")
                auth_header = await get_authorization_header(page)
                course_infos = await _fetch_course_infos(
                    page,
                    courses,
                    subject_id=subject_id,
                    auth_header=auth_header,
                    status_callback=status_callback,
                    failure_results=course_info_failures,
                )
            except Exception as exc:
                if status_callback:
                    status_callback(f"学习专区处理失败，已跳过：{exc}")
                logging.error(f"学习专区处理失败，跳过 {subject_url}: {exc}")
                continue
            resources = collect_section_resources(course_infos)
            all_course_infos.extend(course_infos)
            all_resources.extend(resources)

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
                try:
                    document_results.append(
                        await _download_document_resource(
                            page,
                            resource,
                            docs_dir=docs_dir,
                            auth_header=auth_header,
                        )
                    )
                except Exception as exc:
                    document_results.append(
                        {
                            "ok": False,
                            "course_index": resource.course_index,
                            "course_id": resource.course_id,
                            "section_index": resource.section_index,
                            "course_name": resource.course_name,
                            "section_name": resource.section_name,
                            "attachment_id": resource.attachment_id,
                            "error": str(exc),
                        }
                    )

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
                        "error": str(exc),
                    }
                video_records.append(record)
        await page.close()

    course_files: list[tuple[dict, str]] = []
    for course_index, course_info in enumerate(all_course_infos, start=1):
        course_id = str(course_info.get("course_id") or "")
        filename = build_course_markdown_name(course_index, course_info)
        course_files.append((course_info, filename))
        course_resources = [
            resource for resource in all_resources if resource.course_id == course_id
        ]
        (courses_dir / filename).write_text(
            render_course_markdown(
                course_info,
                course_resources,
                video_records,
                document_results,
            ),
            encoding="utf-8",
        )
    (output_dir / "课程目录.md").write_text(
        render_course_catalog_markdown(course_files, all_resources),
        encoding="utf-8",
    )
    (output_dir / "课程详情读取失败.md").write_text(
        render_course_failures_markdown(course_info_failures),
        encoding="utf-8",
    )
    _write_document_index(output_dir, docs_dir)
    document_count = sum(1 for result in document_results if result.get("ok"))
    video_with_items = sum(1 for record in video_records if record.get("items"))

    return {
        "output_dir": str(output_dir),
        "course_count": len(all_course_infos),
        "course_failed_count": len(course_info_failures),
        "section_count": len(all_resources),
        "document_count": document_count,
        "document_failed_count": sum(
            1 for result in document_results if not result.get("ok")
        ),
        "video_count": len(video_records),
        "video_with_items": video_with_items,
    }
