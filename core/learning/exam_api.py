from __future__ import annotations

from core.abort import raise_for_batch_stop

import logging
import re
import math
from dataclasses import dataclass

from core.browser.page_auth import fetch_json, get_authorization_header
from core.config import (
    COURSE_EXAM_ATTEMPT_THRESHOLD,
    ZHIXUEYUN_EXAM_PREFIX,
)
from core.exam.routing import queue_exam_url_by_attempt_text
from core.queues.manual_exam import append_manual_exam_entry
from core.abort import UserAbortRequested, UserCancelRequested, WafBlockError


_API_HOST = "https://kc.zhixueyun.com"
_COURSE_INFO_API = f"{_API_HOST}/api/v1/course-study/course-front/info/{{course_id}}"
_EXAM_BASIC_API = f"{_API_HOST}/api/v1/exam/exam/basic-by-ids?ids={{exam_ids}}"
_EXAM_USER_RECORD_API = f"{_API_HOST}/api/v1/exam/exam/front/user-record/{{exam_id}}"
_COURSE_ID_PATTERN = re.compile(
    r"/study/course/detail/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CourseExamSection:
    exam_id: str
    name: str = ""

    @property
    def url(self) -> str:
        return f"{ZHIXUEYUN_EXAM_PREFIX}{self.exam_id}"


@dataclass(frozen=True)
class CourseExamState:
    passed: bool
    pending_grading: bool
    allowed_attempts: int | None
    used_attempts: int | None
    remaining_attempts: int | None
    submitted: bool = False
    record_id: str = ""

    @property
    def outcome(self) -> str:
        if self.passed:
            return "passed"
        if self.pending_grading:
            return "pending_grading"
        return "failed" if self.submitted else "unknown"


@dataclass(frozen=True)
class CourseExamQueueResult:
    discovered: int
    ai_queued: int
    manual_queued: int
    completed: int


def extract_course_id(url: str) -> str | None:
    match = _COURSE_ID_PATTERN.search(str(url or ""))
    return match.group(1) if match else None


def _payload_data(payload: object) -> dict:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def extract_course_exam_sections(payload: object) -> list[CourseExamSection]:
    data = _payload_data(payload)
    sections: list[CourseExamSection] = []
    seen: set[str] = set()
    chapters = data.get("courseChapters") or []
    for chapter in chapters if isinstance(chapters, list) else []:
        if not isinstance(chapter, dict):
            continue
        chapter_sections = chapter.get("courseChapterSections") or []
        for section in chapter_sections if isinstance(chapter_sections, list) else []:
            if not isinstance(section, dict):
                continue
            try:
                section_type = int(section.get("sectionType"))
            except (TypeError, ValueError):
                continue
            if section_type != 9:
                continue
            exam_id = str(section.get("resourceId") or "").strip()
            if not exam_id or exam_id in seen:
                continue
            seen.add(exam_id)
            sections.append(
                CourseExamSection(
                    exam_id=exam_id,
                    name=str(section.get("name") or "").strip(),
                )
            )
    return sections


def _as_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
        return (
            int(number)
            if math.isfinite(number) and number.is_integer() and number >= 0
            else None
        )
    except (TypeError, ValueError):
        return None


def _nested_dict(data: dict, key: str) -> dict:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _first_int(*values: object) -> int | None:
    for value in values:
        parsed = _as_int(value)
        if parsed is not None:
            return parsed
    return None


def _score_reaches_pass_line(
    score: float | None,
    pass_score: float | None,
    total: float | None,
) -> bool:
    if score is None or pass_score is None:
        return False
    comparable_score = float(score)
    # 部分考试把总分/得分放大 100 倍（如 500/400），通过线仍用显示分（3）。
    if total is not None and total > 100 and pass_score <= 100:
        comparable_score /= 100
    return comparable_score >= pass_score


def _first_number(*values: object) -> float | None:
    for value in values:
        try:
            number = float(value)
        except (ValueError, TypeError):
            continue
        if math.isfinite(number) and number >= 0:
            return number
    return None


def evaluate_course_exam_state(
    basic_payload: object,
    user_record_payload: object,
) -> CourseExamState:
    basic = _payload_data(basic_payload)
    user = _payload_data(user_record_payload)
    basic_regist = _nested_dict(basic, "examRegist")
    user_regist = _nested_dict(user, "examRegist")
    exam_record = _nested_dict(user, "examRecord")
    basic_paper = _nested_dict(basic, "paperClass")
    user_paper = _nested_dict(user, "paperClass")

    allowed = _first_int(user.get("allowExamTimes"), basic.get("allowExamTimes"))
    used = _first_int(
        user.get("examedTimes"),
        basic.get("examedTimes"),
        user_regist.get("examTimes"),
        basic_regist.get("examTimes"),
    )
    remaining = None
    # 仅显式 0 表示不限次数；缺失字段保持未知，不能当成可自动开考。
    if allowed is not None and allowed > 0 and used is not None:
        remaining = max(0, allowed - used)

    score = _first_number(
        user_regist.get("topScore"),
        basic_regist.get("topScore"),
        exam_record.get("score"),
    )
    pass_score = _first_number(
        user.get("passScore"),
        basic.get("passScore"),
        basic.get("standardScore"),
    )
    total_score = _first_number(
        user_paper.get("totalScore"),
        basic_paper.get("totalScore"),
    )
    passed = _score_reaches_pass_line(score, pass_score, total_score)
    is_finished = _as_int(exam_record.get("isFinished")) == 1
    pending_grading = (
        is_finished and not passed and _first_number(exam_record.get("score")) is None
    )

    return CourseExamState(
        passed=passed,
        pending_grading=pending_grading,
        allowed_attempts=allowed,
        used_attempts=used,
        remaining_attempts=remaining,
        submitted=is_finished,
        record_id=str(exam_record.get("id") or ""),
    )


async def read_exam_state(page, exam_id: str) -> CourseExamState:
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", exam_id):
        raise ValueError("试卷 ID 无效")
    auth = str(await get_authorization_header(page) or "").strip()
    if not auth:
        raise ValueError("缺少考试状态读取凭证")
    headers = {"Authorization": auth, "Version": "12.1.1", "Accept": "application/json"}
    items = await fetch_json(
        page, _EXAM_BASIC_API.format(exam_ids=exam_id), headers=headers
    )
    if isinstance(items, dict):
        items = items.get("data")
    if not isinstance(items, list):
        raise ValueError("考试基本信息响应缺少列表")
    basic = next(
        (
            item
            for item in items
            if isinstance(item, dict) and str(item.get("id")) == exam_id
        ),
        None,
    )
    user = await fetch_json(
        page, _EXAM_USER_RECORD_API.format(exam_id=exam_id), headers=headers
    )
    if basic is None or not isinstance(user, dict) or not _payload_data(user):
        raise ValueError("考试状态响应不完整")
    return evaluate_course_exam_state(basic, user)


async def queue_course_exams_from_api(page) -> CourseExamQueueResult | None:
    """通过课程/考试接口发现并分流课程内考试；失败返回 None 供 DOM 兜底。"""

    course_id = extract_course_id(getattr(page, "url", ""))
    if not course_id:
        return None
    try:
        auth = str(await get_authorization_header(page) or "").strip()
        if not auth:
            return None
        headers = {
            "Authorization": auth,
            "Version": "12.1.1",
            "Accept": "application/json, text/plain, */*",
        }
        from core.tasks.identity import parse_entry
        from urllib.parse import urlencode

        source = parse_entry(str(page.url), "probe").key
        from core.browser.course_links import observed_course_source

        query = (
            {"type": source.source_type, "sourceId": source.source_id}
            if source.source_type
            else observed_course_source(page, course_id) or {}
        )
        course_payload = await fetch_json(
            page,
            f"{_COURSE_INFO_API.format(course_id=course_id)}?{urlencode(query)}",
            headers=headers,
        )
        sections = extract_course_exam_sections(course_payload)
        if not isinstance(_payload_data(course_payload).get("courseChapters"), list):
            return None
        if not sections:
            return CourseExamQueueResult(0, 0, 0, 0)

        basic_payload = await fetch_json(
            page,
            _EXAM_BASIC_API.format(
                exam_ids=",".join(section.exam_id for section in sections)
            ),
            headers=headers,
        )
        basic_items = (
            basic_payload
            if isinstance(basic_payload, list)
            else basic_payload.get("data", [])
            if isinstance(basic_payload, dict)
            else []
        )
        basic_by_id = {
            str(item.get("id")): item
            for item in basic_items
            if isinstance(item, dict) and item.get("id")
        }

        # 先读完全部状态；任何一项失败都不做部分入队，统一回退 DOM，避免重复。
        resolved: list[tuple[CourseExamSection, CourseExamState]] = []
        for section in sections:
            basic = basic_by_id.get(section.exam_id)
            if basic is None:
                return None
            user_record = await fetch_json(
                page,
                _EXAM_USER_RECORD_API.format(exam_id=section.exam_id),
                headers=headers,
            )
            if not isinstance(user_record, dict) or not _payload_data(user_record):
                return None
            resolved.append((section, evaluate_course_exam_state(basic, user_record)))
    except (UserAbortRequested, UserCancelRequested, WafBlockError):
        raise
    except Exception as exc:
        raise_for_batch_stop(exc)
        logging.info(f"课程内考试接口读取失败，改用页面兜底: {exc}")
        return None

    ai_queued = 0
    manual_queued = 0
    completed = 0
    for section, state in resolved:
        from core.tasks.relations import record_discovered_tasks

        record_discovered_tasks(
            [section.url],
            parent_url=str(page.url),
            source="course_exam_api",
            course_exam=True,
            completed_urls=[section.url] if state.passed else [],
        )
        if state.passed:
            completed += 1
            logging.info(f"课程内考试已通过，跳过: {section.name or section.exam_id}")
            continue
        if state.pending_grading:
            append_manual_exam_entry(
                section.url,
                reason="pending_grading",
                reason_text="已提交，等待服务端评卷后复查",
            )
            manual_queued += 1
            continue
        attempt_text = (
            f"剩余 {state.remaining_attempts} 次"
            if state.remaining_attempts is not None
            else "不限次数"
            if state.allowed_attempts == 0
            else "次数未知"
        )
        destination = queue_exam_url_by_attempt_text(
            section.url,
            attempt_text,
            threshold=COURSE_EXAM_ATTEMPT_THRESHOLD,
        )
        if destination == "ai":
            ai_queued += 1
        elif destination == "manual":
            manual_queued += 1
        logging.info(
            f"课程内考试接口分流: {section.name or section.exam_id} -> {destination}"
        )

    return CourseExamQueueResult(
        discovered=len(sections),
        ai_queued=ai_queued,
        manual_queued=manual_queued,
        completed=completed,
    )
