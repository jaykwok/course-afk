"""Resolve standalone exam entry from the platform API, independent of button text."""
from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlencode

from core.browser.page_auth import fetch_json, get_authorization_header
from core.config import ZHIXUEYUN_EXAM_PREFIX
from core.file_ops import is_exam_url, normalize_url
from core.learning.exam_api import CourseExamState, evaluate_course_exam_state


_USER_RECORD_API = "https://kc.zhixueyun.com/api/v1/exam/exam/front/user-record/"
# Spelling follows the platform's exam-detail entry validation endpoint.
_VALIDATE_API = "https://kc.zhixueyun.com/api/v1/exam/exam/front/valiate-exam"


class ExamEntryUnavailable(RuntimeError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ExamEntry:
    exam_id: str
    answer_url: str
    state: CourseExamState
    can_resume: bool
    blocked_reason: str = ""
    blocked_message: str = ""


def exam_id_from_url(url: str) -> str:
    normalized = normalize_url(url)
    if not is_exam_url(normalized):
        raise ExamEntryUnavailable("unknown_url_type", "无法从考试链接提取有效考试 ID")
    return normalized.rsplit("/", 1)[-1]


def _data(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ExamEntryUnavailable("state_unknown", "考试接口响应格式异常")
    return payload["data"] if isinstance(payload.get("data"), dict) else payload


async def _headers(page) -> dict[str, str]:
    auth = str(await get_authorization_header(page) or "").strip()
    if not auth:
        raise ExamEntryUnavailable("state_unknown", "缺少考试入口读取凭证，请更新登录凭证")
    return {"Authorization": auth, "Version": "12.1.1", "Accept": "application/json"}


def parse_exam_entry(payload: object, exam_id: str) -> ExamEntry:
    data = _data(payload)
    if data.get("id") != exam_id or not isinstance(data.get("examRecord"), dict):
        raise ExamEntryUnavailable("state_unknown", "考试接口返回的 ID 或考试记录不完整")
    state = evaluate_course_exam_state(data, data)
    record = data["examRecord"]
    # Platform prompt-help uses record ID/start time/status, not the button label.
    can_resume = bool(
        record.get("id") and record.get("startTime")
        and record.get("status") in {1, 2, 3, 4}
        and record.get("isFinished") != 1
        and not record.get("submitTime") and record.get("isReset") != 1
    )
    reason = message = ""
    now = record.get("currentTime")
    if not isinstance(now, (int, float)):
        now = time.time() * 1000
    start, end = data.get("startTime"), data.get("endTime")
    signup = data.get("signUp") or {}
    if not isinstance(signup, dict):
        raise ExamEntryUnavailable("state_unknown", "考试报名状态响应格式异常")
    if data.get("isSetAccessSecret") == 1 or data.get("isSetPersonalCode") == 1:
        reason, message = "entry_verification_required", "本考试需要考试密码或个人码，请在平台完成验证"
    elif data.get("clientType") == 2:
        reason, message = "entry_unavailable", "本考试要求使用平台 App 进入"
    elif data.get("status") in {1, 7} or data.get("type") not in {1, 3, 4}:
        reason, message = "entry_unavailable", "考试已撤销、正在更新或属于不支持的入口类型"
    elif not isinstance(data.get("paperClass"), dict) or not data["paperClass"]:
        reason, message = "state_unknown", "考试试卷信息尚未就绪"
    elif signup.get("status") == 3 or (
        data.get("needApplicant") == 1 and data.get("examedTimes") == 0
        and not record.get("id") and signup.get("status") != 2
    ):
        reason, message = "entry_unavailable", "考试尚未完成报名或报名审核未通过"
    elif isinstance(start, (int, float)) and now < start:
        reason, message = "entry_unavailable", "考试尚未到开始时间"
    elif isinstance(end, (int, float)) and now > end and not can_resume:
        reason, message = "entry_unavailable", "考试已结束且没有可继续的考试记录"
    elif record.get("status") == 5:
        reason, message = "pending_grading", "平台正在处理试卷，请稍后复查"
    elif state.remaining_attempts == 0 and not can_resume:
        reason, message = "attempt_limit", "平台没有剩余开考次数，保留复查"
    return ExamEntry(
        exam_id, f"{ZHIXUEYUN_EXAM_PREFIX}{exam_id}", state, can_resume, reason, message,
    )


async def read_exam_entry(page, url: str) -> ExamEntry:
    exam_id = exam_id_from_url(url)
    payload = await fetch_json(page, _USER_RECORD_API + exam_id, headers=await _headers(page))
    return parse_exam_entry(payload, exam_id)


async def validate_exam_entry(page, entry: ExamEntry) -> None:
    """Run the same eligibility check as the platform before navigating to a paper."""
    if entry.blocked_reason:
        raise ExamEntryUnavailable(entry.blocked_reason, entry.blocked_message)
    result = _data(await fetch_json(
        page, _VALIDATE_API,
        headers={**await _headers(page), "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        method="POST", data=urlencode({"examId": entry.exam_id}),
    ))
    status = result.get("status")
    if type(status) is not int:
        raise ExamEntryUnavailable("state_unknown", "平台未返回有效的考试资格校验状态")
    if status == 1:
        return
    reason, message = {
        0: ("entry_unavailable", "平台不允许已通过的考试重复开考"),
        2: ("entry_unavailable", "当前 IP 不在平台允许的考试范围内"),
        3: ("attempt_limit", "答卷次数已超过平台允许的最大次数"),
    }.get(status, ("state_unknown", "平台未明确确认考试进入资格，保留复查"))
    raise ExamEntryUnavailable(reason, message)
