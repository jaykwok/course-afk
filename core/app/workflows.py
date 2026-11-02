from __future__ import annotations

from typing import Callable

from core.queues.learning import remember_discovery_entries
from core.config import (
    EXAM_URLS_FILE,
    LEARNING_URLS_FILE,
)
from core.auth.credential import AccountProfile
from core.queues.exam import append_exam_urls, read_exam_urls
from core.links import extract_urls_from_text, split_manual_selection_urls
from core.queues.learning import append_learning_urls, read_learning_urls
from core.auth.login import login_and_save_credential
from core.state import collect_project_state
from core.discovery.subject_parse import (
    partition_course_and_subject_urls,
)


StatusCallback = Callable[[str], None]


def parse_manual_selection_input(text: str) -> list[str]:
    return extract_urls_from_text(text)


async def refresh_credential(
    status_callback: StatusCallback | None = None,
) -> AccountProfile:
    if status_callback:
        status_callback("正在打开浏览器，请完成登录")
    return await login_and_save_credential()


async def run_manual_course_selection(
    input_text,
    learning_zone_mode="manual",
    status_callback=None,
    result_ready_callback=None,
):
    from core.auth.access import require_task_account
    from core.app.service import ApplicationService

    require_task_account()
    urls = parse_manual_selection_input(input_text)
    learning, exams, zones, classes, entries = split_manual_selection_urls(urls)
    courses, subjects = partition_course_and_subject_urls(learning)
    added_learning = append_learning_urls(courses, file_path=LEARNING_URLS_FILE)
    added_exams = append_exam_urls(exams, file_path=EXAM_URLS_FILE)
    discoveries = subjects + zones + classes + entries
    remember_discovery_entries(discoveries)
    before_learning = len(read_learning_urls(LEARNING_URLS_FILE))
    outcomes = (
        await ApplicationService().batch(
            discoveries,
            "discover",
            options={"zone_mode": learning_zone_mode},
            status_callback=status_callback,
        )
        if discoveries
        else []
    )
    result = {
        "input_url_count": len(urls),
        "direct_learning_count": len(added_learning),
        "direct_exam_count": len(added_exams),
        "direct_subject_count": len(subjects),
        "learning_zone_url_count": len(zones),
        "learning_zone_parsed_count": 0,
        "train_class_url_count": len(classes),
        "train_class_parsed_count": 0,
        "entry_url_count": len(entries),
        "manual_record_count": 0,
        "manual_exam_record_count": 0,
        "learning_total": len(read_learning_urls(LEARNING_URLS_FILE)),
        "exam_total": len(read_exam_urls(EXAM_URLS_FILE)),
        "blocked_count": sum(o["status"] == "BLOCKED" for o in outcomes),
    }
    for outcome in outcomes:
        details = outcome.get("details", {})
        result["manual_record_count"] += details.get("manual_record_count", 0)
        result["manual_exam_record_count"] += details.get("manual_exam_record_count", 0)
    result["learning_zone_parsed_count"] = max(
        0, result["learning_total"] - before_learning
    )
    if result_ready_callback:
        result_ready_callback(result)
    return result


async def run_afk_workflow(status_callback: StatusCallback | None = None) -> bool:
    if status_callback:
        status_callback("开始挂课")
    from core.app.service import ApplicationService

    await ApplicationService().learn(status_callback=status_callback)
    state = collect_project_state()
    if status_callback:
        status_callback(
            f"本轮挂课结束：学习待办 {state.learning_count}，失败/复查 {state.learning_failure_count}，AI 考试 {state.exam_count}"
        )
    return state.exam_count > 0


async def run_ai_exam_workflow(
    status_callback: StatusCallback | None = None,
    *,
    auto_submit: bool = False,
) -> int:
    state = collect_project_state()
    if state.exam_count == 0 and state.manual_exam_count == 0:
        if status_callback:
            status_callback("未检测到考试链接，本次流程结束")
        return 0

    if status_callback:
        status_callback(f"开始 AI 自动考试，共 {state.exam_count} 条考试链接")
    from core.app.service import ApplicationService
    from core.queues.manual_exam import read_manual_exam_queue

    await ApplicationService().exams(
        status_callback=status_callback, auto_submit=auto_submit
    )
    manual_count = len(read_manual_exam_queue())
    if status_callback:
        status_callback(f"AI 自动考试结束，待复查 {manual_count} 条")
    return manual_count


async def run_reference_collection_workflow(
    subject_urls: list[str],
    status_callback: StatusCallback | None = None,
) -> dict:
    from core.app.service import ApplicationService

    outcomes = await ApplicationService().references(
        subject_urls, status_callback=status_callback
    )
    details = [o.get("details", {}) for o in outcomes]
    from core.config import REFERENCE_OUTPUT_DIR

    return {
        "complete": all(o["status"] == "COMPLETED" for o in outcomes),
        "output_dir": str(REFERENCE_OUTPUT_DIR),
        "course_count": sum(d.get("course_count", 0) for d in details),
        "section_count": sum(d.get("section_count", 0) for d in details),
        "document_count": sum(d.get("document_count", 0) for d in details),
        "pdf_count": sum(d.get("pdf_count", 0) for d in details),
        "document_failed_count": sum(
            d.get("document_failed_count", 0) for d in details
        ),
        "course_failed_count": sum(d.get("course_failed_count", 0) for d in details),
        "video_count": sum(d.get("video_count", 0) for d in details),
        "video_with_items": sum(d.get("video_with_items", 0) for d in details),
        "downloaded_count": sum(d.get("downloaded_count", 0) for d in details),
        "summary_count": sum(d.get("summary_count", 0) for d in details),
        "failed_count": sum(d.get("failed_count", 0) for d in details),
        "outcomes": outcomes,
    }
