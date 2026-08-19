from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from core.browser.page_auth import fetch_json, get_authorization_header
from core.config import (
    COURSE_EXAM_ATTEMPT_THRESHOLD,
    ZHIXUEYUN_EXAM_PREFIX,
)
from core.exam.routing import queue_exam_url_by_attempt_text


_API_HOST = "https://kc.zhixueyun.com"
_COURSE_INFO_API = f"{_API_HOST}/api/v1/course-study/course-front/info/{{course_id}}"
_EXAM_BASIC_API = f"{_API_HOST}/api/v1/exam/exam/basic-by-ids?ids={{exam_ids}}"
_EXAM_USER_RECORD_API = (
    f"{_API_HOST}/api/v1/exam/exam/front/user-record/{{exam_id}}"
)
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
        return int(value)
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
    score: int | None,
    pass_score: int | None,
    total: int | None,
) -> bool:
    if score is None or pass_score is None:
        return False
    comparable_score = float(score)
    # 部分考试把总分/得分放大 100 倍（如 500/400），通过线仍用显示分（3）。
    if total is not None and total > 100 and pass_score <= 100 and score > 100:
        comparable_score /= 100
    return comparable_score >= pass_score


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
    # 0 / null 通常表示不限制次数；只有正数总次数才计算剩余值。
    if allowed is not None and allowed > 0:
        remaining = max(0, allowed - max(0, used or 0))

    score = _first_int(
        user_regist.get("topScore"),
        basic_regist.get("topScore"),
        exam_record.get("score"),
    )
    pass_score = _first_int(
        user.get("passScore"),
        basic.get("passScore"),
        basic.get("standardScore"),
    )
    total_score = _first_int(
        user_paper.get("totalScore"),
        basic_paper.get("totalScore"),
    )
    passed = _score_reaches_pass_line(score, pass_score, total_score)
    is_finished = _as_int(exam_record.get("isFinished")) == 1
    pending_grading = is_finished and score is None

    return CourseExamState(
        passed=passed,
        pending_grading=pending_grading,
        allowed_attempts=allowed,
        used_attempts=used,
        remaining_attempts=remaining,
    )


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
        course_payload = await fetch_json(
            page,
            f"{_COURSE_INFO_API.format(course_id=course_id)}?type=1&sourceId=",
            headers=headers,
        )
        sections = extract_course_exam_sections(course_payload)
        if not sections:
            return CourseExamQueueResult(0, 0, 0, 0)

        basic_payload = await fetch_json(
            page,
            _EXAM_BASIC_API.format(
                exam_ids=",".join(section.exam_id for section in sections)
            ),
            headers=headers,
        )
        basic_items = basic_payload if isinstance(basic_payload, list) else []
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
            resolved.append(
                (section, evaluate_course_exam_state(basic, user_record))
            )
    except Exception as exc:
        logging.info(f"课程内考试接口读取失败，改用页面兜底: {exc}")
        return None

    ai_queued = 0
    manual_queued = 0
    completed = 0
    for section, state in resolved:
        if state.passed or state.pending_grading:
            completed += 1
            logging.info(
                f"课程内考试已完成/待评卷，跳过: {section.name or section.exam_id}"
            )
            continue
        attempt_text = (
            f"剩余 {state.remaining_attempts} 次"
            if state.remaining_attempts is not None
            else ""
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
