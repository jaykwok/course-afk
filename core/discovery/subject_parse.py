from __future__ import annotations

from core.browser.bootstrap import navigate_platform_page

import logging
import re
from dataclasses import dataclass, field

from core.config import (
    ZHIXUEYUN_EXAM_PREFIX,
)
from core.file_ops import (
    is_compliant_url_regex,
    is_exam_url,
    is_subject_detail_url,
    normalize_url,
    build_subject_course_url,
)
from core.links import unique_urls
from core.browser.page_auth import fetch_json, wait_for_authorization_header
from core.browser.overlays import prepare_page_after_navigation_async
from core.abort import raise_for_batch_stop


# 实勘（subject 详情页）:
# - 鉴权同 class: Authorization: Bearer__{token}
# - GET .../subject/chapter-progress?courseId={subjectId}
# - 章节 courseChapterSections[].sectionType 分流——注意：
#   此处数字与「课程内」DOM data-sectiontype 不是同一套（课程内 3=文档，主题 3=外链）
#   * 10 → 课程 course/detail/{resourceId}/6/{required}/{subjectId} → 学习队列
#   * 9  → 考试 exam/answer-paper/{resourceId} → 考试队列
#   * 3  → URL（外链，字段 url）→ 不入队，保留 subject 残留由 DOM 处理
#   * 其它未知类型 → 保留 subject 残留
# - 非培训班 chapter-activity-list，不能复用 class 分页接口
_API_HOST = "https://kc.zhixueyun.com"
_CHAPTER_PROGRESS_API = (
    f"{_API_HOST}/api/v1/course-study/subject/chapter-progress"
    "?courseId={subject_id}&knowledgePaymentEnable=false"
)
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_SUBJECT_ID_FROM_DETAIL = re.compile(
    rf"/study/subject/detail/({_UUID})",
    re.IGNORECASE,
)

# 主题 chapter-progress.sectionType（仅实勘确认；新类型抓包后补）
# 10=课程  9=考试(resourceId=试卷UUID)  3=外链URL
COURSE_SECTION_TYPES: frozenset[int] = frozenset({10})
EXAM_SECTION_TYPES: frozenset[int] = frozenset({9})
URL_SECTION_TYPES: frozenset[int] = frozenset({3})


@dataclass(frozen=True)
class SubjectExpandResult:
    """主题 chapter-progress 按章节类型展开的结果。"""

    course_urls: list[str] = field(default_factory=list)
    exam_urls: list[str] = field(default_factory=list)
    # 存在 URL/未知 sectionType 或 API 失败时保留主题链接，供挂课 DOM 兜底
    residual_subject_url: str | None = None
    unknown_section_types: tuple[int, ...] = ()
    url_section_count: int = 0
    section_total: int = 0
    complete: bool = False


def extract_subject_id(url: str) -> str | None:
    """从主题详情链接提取 subject UUID。"""
    if not url:
        return None
    for candidate in (normalize_url(url), url):
        match = _SUBJECT_ID_FROM_DETAIL.search(candidate)
        if match:
            return match.group(1)
    return None


def partition_course_and_subject_urls(
    urls: list[str],
) -> tuple[list[str], list[str]]:
    """将合规学习链接拆成 course 与 subject 两组（去重保序）。"""
    courses: list[str] = []
    subjects: list[str] = []
    seen: set[str] = set()
    for raw in urls or []:
        url = normalize_url((raw or "").strip())
        if not url or url in seen:
            continue
        seen.add(url)
        if is_subject_detail_url(url):
            subjects.append(url)
        else:
            courses.append(url)
    return courses, subjects


def _normalize_section_type(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iter_chapter_sections(payload: object):
    """yield (section_dict) from chapter-progress payload."""
    if isinstance(payload, list):
        chapters = payload
    elif isinstance(payload, dict):
        chapters = (
            payload.get("items")
            or payload.get("datas")
            or payload.get("data")
            or payload.get("chapters")
            or []
        )
    else:
        chapters = []

    if not isinstance(chapters, list):
        return

    for chapter in chapters:
        if not isinstance(chapter, dict):
            continue
        sections = chapter.get("courseChapterSections") or []
        if not isinstance(sections, list):
            continue
        for section in sections:
            if isinstance(section, dict):
                yield section


def _section_uuid(section: dict, keys: tuple[str, ...]) -> str | None:
    """从 section 字段中取第一个合法 UUID。"""
    for key in keys:
        value = str(section.get(key) or "").strip()
        if re.fullmatch(_UUID, value, re.IGNORECASE):
            return value
    return None


def _section_course_id(section: dict) -> str | None:
    """课程详情 UUID：使用资源 UUID，不能使用主题章节记录 UUID。

    实勘 ``chapter-progress`` 中：``id/referenceId`` 是主题章节记录，直接
    拼到 ``course/detail`` 会令详情接口返回 422；真正课程 UUID 同时出现在
    ``attachmentId`` 与 ``resourceId``。缺少资源 UUID 时保留主题给 DOM 处理，
    不再生成一个看似合法、实际无效的课程链接。
    """
    return _section_uuid(section, ("attachmentId", "resourceId"))


def _section_exam_id(section: dict) -> str | None:
    """
    考试试卷 UUID。

    实勘（sectionType=9）: resourceId 为试卷 id，写入 answer-paper；
    DOM 挂机同样用 data-resource-id。id/referenceId 是章节小节 id，勿混用。
    """
    return _section_uuid(section, ("resourceId",))


def expand_chapter_progress(
    payload: object,
    *,
    subject_url: str | None = None,
) -> SubjectExpandResult:
    """
    按 sectionType 分流主题章节。

    - COURSE_SECTION_TYPES → 学习队列 course 链接
    - EXAM_SECTION_TYPES → 考试队列
    - URL_SECTION_TYPES → 不入队，触发 subject 残留（DOM 处理外链）
    - 其它 / 无法识别 → residual，保留 subject_url
    """
    course_urls: list[str] = []
    exam_urls: list[str] = []
    unknown_types: list[int] = []
    url_section_count = 0
    section_total = 0
    subject_id = extract_subject_id(subject_url or "")

    for section in _iter_chapter_sections(payload):
        section_total += 1
        section_type = _normalize_section_type(section.get("sectionType"))

        if section_type in COURSE_SECTION_TYPES:
            course_id = _section_course_id(section)
            if course_id:
                source_id = subject_id or _section_uuid(section, ("courseId",))
                try:
                    candidate = build_subject_course_url(
                        course_id, source_id or "", section.get("required")
                    )
                except ValueError:
                    # 来源/必修标记未知时保留专题，由平台原入口处理。
                    unknown_types.append(section_type)
                    continue
                if is_compliant_url_regex(candidate):
                    course_urls.append(candidate)
                    continue

        if section_type in EXAM_SECTION_TYPES:
            exam_id = _section_exam_id(section)
            if exam_id:
                candidate = f"{ZHIXUEYUN_EXAM_PREFIX}{exam_id}"
                if is_exam_url(candidate):
                    exam_urls.append(candidate)
                    continue

        if section_type in URL_SECTION_TYPES:
            url_section_count += 1
            continue

        # 未知类型、缺 type、或 id 非法 → 残留
        if section_type is not None:
            unknown_types.append(section_type)
        else:
            # 用 -1 标记「缺 sectionType」，便于日志/测试
            unknown_types.append(-1)

    course_urls = unique_urls(course_urls)
    exam_urls = unique_urls(exam_urls)
    # 去重未知类型，保序
    seen_types: set[int] = set()
    unique_unknown: list[int] = []
    for item in unknown_types:
        if item not in seen_types:
            seen_types.add(item)
            unique_unknown.append(item)

    has_residual = bool(unique_unknown) or url_section_count > 0 or section_total == 0
    residual = None
    if has_residual and subject_url:
        residual = normalize_url(subject_url)

    return SubjectExpandResult(
        course_urls=course_urls,
        exam_urls=exam_urls,
        residual_subject_url=residual,
        unknown_section_types=tuple(unique_unknown),
        url_section_count=url_section_count,
        section_total=section_total,
        complete=section_total > 0,
    )


async def expand_subject_from_page(
    page,
    subject_url: str,
    status_callback=None,
) -> SubjectExpandResult:
    """打开主题详情页，经 chapter-progress API 按章节类型展开。"""
    normalized = normalize_url(subject_url)
    subject_id = extract_subject_id(normalized)
    if not subject_id:
        if status_callback:
            status_callback(f"无法识别主题 subjectId: {subject_url}")
        return SubjectExpandResult(residual_subject_url=normalized or subject_url)

    await navigate_platform_page(page, normalized, wait_until="load")
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception as exc:
        raise_for_batch_stop(exc)
        await page.wait_for_timeout(1500)
    await prepare_page_after_navigation_async(page, status_callback=status_callback)
    auth_header = await wait_for_authorization_header(
        page,
        status_callback,
        empty_message="主题页面未拿到登录令牌，请确认 cookies 仍有效",
    )
    if not auth_header:
        return SubjectExpandResult(residual_subject_url=normalized)

    headers = {
        "Authorization": auth_header,
        "Accept": "application/json, text/plain, */*",
    }
    url = _CHAPTER_PROGRESS_API.format(subject_id=subject_id)
    try:
        payload = await fetch_json(page, url, headers=headers)
    except Exception as exc:
        raise_for_batch_stop(exc)
        logging.error(f"读取主题章节进度失败 subjectId={subject_id}: {exc}")
        if status_callback:
            status_callback(f"读取主题章节失败: {exc}")
        return SubjectExpandResult(residual_subject_url=normalized)

    result = expand_chapter_progress(payload, subject_url=normalized)
    if status_callback:
        parts: list[str] = []
        if result.course_urls:
            parts.append(f"{len(result.course_urls)} 门课程")
        if result.exam_urls:
            parts.append(f"{len(result.exam_urls)} 场考试")
        if result.url_section_count:
            parts.append(f"{result.url_section_count} 个 URL")
        if result.residual_subject_url:
            residual_bits: list[str] = []
            if result.url_section_count:
                residual_bits.append(f"URL×{result.url_section_count}")
            if result.unknown_section_types:
                residual_bits.append(f"未知类型 {list(result.unknown_section_types)}")
            if not residual_bits:
                residual_bits.append("无可用章节")
            parts.append(f"保留主题残留（{'；'.join(residual_bits)}）")
        if parts:
            status_callback("主题展开：" + "，".join(parts))
        else:
            status_callback("主题章节中未识别到可展开内容")
    if result.unknown_section_types or result.url_section_count:
        logging.info(
            "主题 %s 残留: url=%s unknown_sectionType=%s",
            subject_id,
            result.url_section_count,
            list(result.unknown_section_types),
        )
    return result


async def collect_course_links_from_subject_page(
    page,
    subject_url: str,
    status_callback=None,
) -> list[str]:
    """打开主题详情页，经 chapter-progress API 收集课程链接（兼容资料收集等只关心课程的调用方）。"""
    result = await expand_subject_from_page(
        page, subject_url, status_callback=status_callback
    )
    return list(result.course_urls)
