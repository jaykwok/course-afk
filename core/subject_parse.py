from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from core.browser import create_browser_context
from core.config import (
    ZHIXUEYUN_COURSE_PREFIX,
    ZHIXUEYUN_EXAM_PREFIX,
)
from core.exam_queue import append_exam_urls
from core.file_ops import (
    is_compliant_url_regex,
    is_exam_url,
    is_subject_detail_url,
    normalize_url,
)
from core.learning_queue import append_learning_urls
from core.links import unique_urls
from core.page_auth import fetch_json, wait_for_authorization_header
from core.page_overlays import prepare_page_after_navigation_async


# 实勘（subject 详情页）:
# - 鉴权同 class: Authorization: Bearer__{token}
# - GET .../subject/chapter-progress?courseId={subjectId}
# - 章节 courseChapterSections[].sectionType 分流（方案 C）——注意：
#   此处数字与「课程内」DOM data-sectiontype 不是同一套（课程内 3=文档，主题 3=外链）
#   * 10 → 课程 course/detail/{id} → 学习队列
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
    """主题 chapter-progress 展开结果（方案 C）。"""

    course_urls: list[str] = field(default_factory=list)
    exam_urls: list[str] = field(default_factory=list)
    # 存在 URL/未知 sectionType 或 API 失败时保留主题链接，供挂课 DOM 兜底
    residual_subject_url: str | None = None
    unknown_section_types: tuple[int, ...] = ()
    url_section_count: int = 0
    section_total: int = 0


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
    """课程详情 UUID：优先 section.id（实勘与 course/detail 一致）。"""
    return _section_uuid(section, ("id", "referenceId"))


def _section_exam_id(section: dict) -> str | None:
    """
    考试试卷 UUID。

    实勘（sectionType=9）: resourceId 为试卷 id，写入 answer-paper；
    DOM 挂机同样用 data-resource-id。id/referenceId 是章节小节 id，勿混用。
    """
    return _section_uuid(section, ("resourceId", "id", "referenceId"))


def expand_chapter_progress(
    payload: object,
    *,
    subject_url: str | None = None,
) -> SubjectExpandResult:
    """
    按 sectionType 分流主题章节（方案 C）。

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

    for section in _iter_chapter_sections(payload):
        section_total += 1
        section_type = _normalize_section_type(section.get("sectionType"))

        if section_type in COURSE_SECTION_TYPES:
            course_id = _section_course_id(section)
            if course_id:
                candidate = f"{ZHIXUEYUN_COURSE_PREFIX}{course_id}"
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

    has_residual = (
        bool(unique_unknown) or url_section_count > 0 or section_total == 0
    )
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
    )


def extract_course_links_from_chapter_progress(payload: object) -> list[str]:
    """
    从 chapter-progress JSON 提取课程链接（仅 sectionType ∈ COURSE_SECTION_TYPES）。

    实勘: 顶层为章节数组；sectionType=10 的 courseChapterSections[].id → course/detail。
    """
    return list(expand_chapter_progress(payload).course_urls)


async def expand_subject_from_page(
    page,
    subject_url: str,
    status_callback=None,
) -> SubjectExpandResult:
    """打开主题详情页，经 chapter-progress API 按方案 C 展开。"""
    normalized = normalize_url(subject_url)
    subject_id = extract_subject_id(normalized)
    if not subject_id:
        if status_callback:
            status_callback(f"无法识别主题 subjectId: {subject_url}")
        return SubjectExpandResult(residual_subject_url=normalized or subject_url)

    await page.goto(normalized, wait_until="load")
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
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


def append_expand_result(result: SubjectExpandResult) -> tuple[int, int]:
    """将展开结果写入学习队列与考试队列，返回 (learning_added, exam_added)。"""
    learning_urls = list(result.course_urls)
    if result.residual_subject_url:
        learning_urls.append(result.residual_subject_url)
    learning_added = append_learning_urls(learning_urls)
    exam_added = append_exam_urls(result.exam_urls)
    return len(learning_added), len(exam_added)


async def enqueue_learning_links_with_subject_expand(
    learning_links: list[str],
    *,
    page=None,
    status_callback=None,
    source_label: str = "来源",
) -> dict[str, int]:
    """
    将混合课程/主题链接分流入队（课直接进学习队列，主题按方案 C 展开）。

    返回 course_links / subject_links / course_added / subject_learning_added /
    learning_added / exam_added。
    """
    course_links, subject_links = partition_course_and_subject_urls(learning_links)
    course_added_urls = append_learning_urls(course_links)
    course_added = len(course_added_urls)
    subject_learning_added = 0
    exam_added = 0
    if subject_links:
        if status_callback:
            status_callback(
                f"{source_label}含 {len(subject_links)} 个主题，按类型展开中"
            )
        expand_stats = await expand_and_append_subject_urls(
            subject_links,
            status_callback=status_callback,
            page=page,
        )
        subject_learning_added = expand_stats["learning_added"]
        exam_added = expand_stats["exam_added"]
    return {
        "course_links": len(course_links),
        "subject_links": len(subject_links),
        "course_added": course_added,
        "subject_learning_added": subject_learning_added,
        "learning_added": course_added + subject_learning_added,
        "exam_added": exam_added,
    }


async def expand_and_append_subject_urls(
    subject_urls: list[str],
    status_callback=None,
    before_close_callback=None,
    *,
    page=None,
    context=None,
) -> dict[str, int]:
    """
    批量展开主题并写入队列（方案 C）。

    可传入已有 page/context 复用浏览器；否则自建 context。
    返回 course_count / exam_count / residual_count / learning_added / exam_added。
    """
    stats = {
        "course_count": 0,
        "exam_count": 0,
        "residual_count": 0,
        "learning_added": 0,
        "exam_added": 0,
        "subject_count": 0,
    }
    if not subject_urls:
        if before_close_callback:
            before_close_callback(stats["learning_added"])
        return stats

    unique_subjects = unique_urls(
        [normalize_url(url.strip()) for url in subject_urls if url and url.strip()]
    )
    stats["subject_count"] = len(unique_subjects)

    async def _run_with_page(active_page) -> None:
        for index, url in enumerate(unique_subjects, start=1):
            if status_callback:
                status_callback(
                    f"正在展开主题链接 {index}/{len(unique_subjects)}"
                )
            try:
                result = await expand_subject_from_page(
                    active_page, url, status_callback=status_callback
                )
            except Exception as exc:
                logging.error(f"展开主题失败 {url}: {exc}")
                if status_callback:
                    status_callback(f"展开主题失败，保留原链接: {exc}")
                result = SubjectExpandResult(residual_subject_url=normalize_url(url))
            stats["course_count"] += len(result.course_urls)
            stats["exam_count"] += len(result.exam_urls)
            if result.residual_subject_url:
                stats["residual_count"] += 1
            learning_n, exam_n = append_expand_result(result)
            stats["learning_added"] += learning_n
            stats["exam_added"] += exam_n

    if page is not None:
        await _run_with_page(page)
    elif context is not None:
        active = await context.new_page()
        try:
            await _run_with_page(active)
        finally:
            await active.close()
    else:
        async with create_browser_context() as (_, new_context):
            active = await new_context.new_page()
            try:
                await _run_with_page(active)
            finally:
                await active.close()

    if status_callback:
        status_callback(
            "主题展开完成："
            f"{stats['course_count']} 课 / {stats['exam_count']} 考 / "
            f"{stats['residual_count']} 残留，"
            f"学习队列 +{stats['learning_added']}，考试队列 +{stats['exam_added']}"
        )
    if before_close_callback:
        before_close_callback(stats["learning_added"])
    return stats


