"""External activities own fresh I/O and browser finally blocks on every entry."""

from __future__ import annotations

from core.workflow.contracts import TaskOutcome, Status


def _task(host, payload):
    host.authorize(payload)
    return host.repository.task(payload["task_id"])


async def learn(host, payload):
    from core.browser.session import create_browser_context
    from core.learning.afk_runner import execute_learning
    from core.learning.flows import course_learning, subject_learning
    from core.file_ops import is_subject_detail_url

    task = _task(host, payload)
    from core.file_ops import is_compliant_url_regex

    if not is_compliant_url_regex(task["url"]):
        from core.queues.learning import record_learning_failure

        record_learning_failure(
            task["url"],
            reason="non_compliant_url",
            reason_text="学习入口格式未识别，请重新解析来源",
        )
        return TaskOutcome(task["task_id"], Status.NEEDS_REVIEW, ["non_compliant_url"])
    async with create_browser_context() as (_, context):
        result = await execute_learning(
            context,
            task["url"],
            subject_learning if is_subject_detail_url(task["url"]) else course_learning,
        )
    return result


async def discover(host, payload):
    from core.browser.session import create_browser_context
    from core.discovery.train_class import ClassEnrollmentError
    from core.discovery.recursive import collect_discovery_entries
    from core.discovery.manual import collect_learning_links_from_entry_urls
    from core.queues.learning import (
        read_learning_failures,
        remove_learning_failure,
        record_learning_failure,
    )
    from core.links import is_learning_zone_url

    task = _task(host, payload)
    details = {}
    discovery_complete = False
    try:
        async with create_browser_context() as (_, context):
            automatic = task["kind"] in {"class", "subject", "course", "exam"} or is_learning_zone_url(task["url"])
            manual = is_learning_zone_url(task["url"]) and payload["options"].get("zone_mode", "auto") == "manual"
            if automatic and not manual:
                result = await collect_discovery_entries(
                    [task["url"]], context=context, status_callback=host.status_callback,
                )
                discovery_complete = result.complete
                details = {**result.details, "learning_added": result.added}
            else:
                counts = await collect_learning_links_from_entry_urls(
                    [task["url"]], context=context, status_callback=host.status_callback,
                )
                details = {"manual_record_count": counts[1], "manual_exam_record_count": counts[2]}
                discovery_complete = True
        details["discovery_complete"] = discovery_complete
        if discovery_complete:
            remove_learning_failure(task["url"], reason="discovery_incomplete")
            remove_learning_failure(task["url"], reason="enrollment_blocked")
        else:
            # Keep the collector's concrete diagnosis instead of replacing it
            # with a generic message and an empty detail object.
            existing = [e for e in read_learning_failures()
                        if e.url == task["url"] and e.reason in {"discovery_incomplete", "enrollment_blocked"}]
            if not existing:
                record_learning_failure(
                    task["url"], reason="discovery_incomplete",
                    reason_text=details.get("message") or "入口清单尚未完整确认，请重新解析；已发现的任务已保留",
                    detail=details,
                )
            elif not details.get("message"):
                details["message"] = existing[0].reason_text
        failures = [e for e in read_learning_failures() if e.url == task["url"]]
        children = [r["task_id"] for r in host.repository.children(task["task_id"])]
        return TaskOutcome(
            task["task_id"],
            Status.BLOCKED
            if any(e.reason == "enrollment_blocked" for e in failures)
            else Status.NEEDS_REVIEW
            if failures
            else Status.WAITING_CHILDREN
            if details.get("residual_count")
            else Status.COMPLETED,
            [e.reason for e in failures]
            or (["subject_residual"] if details.get("residual_count") else []),
            child_refs=children,
            details=details,
        )
    except ClassEnrollmentError as exc:
        record_learning_failure(
            task["url"], reason="enrollment_blocked", reason_text=str(exc)
        )
        return TaskOutcome(
            task["task_id"],
            Status.BLOCKED,
            ["enrollment_blocked"],
            details={"message": str(exc)},
        )


async def exam(host, payload):
    from core.exam.runner import execute_exam

    task = _task(host, payload)
    result = await execute_exam(
        task["url"], auto_submit=payload["options"].get("auto_submit", False)
    )
    result.task_id = task["task_id"]
    return result


async def references(host, payload):
    from core.discovery.reference_collector import collect_reference_materials

    task = _task(host, payload)
    result = await collect_reference_materials(
        [task["url"]], status_callback=host.status_callback
    )
    return TaskOutcome(
        task["task_id"],
        Status.COMPLETED if result.get("complete") else Status.FAILED,
        [] if result.get("complete") else ["references_incomplete"],
        details=result,
    )


ACTIVITIES = {
    "learn": learn,
    "discover": discover,
    "exam": exam,
    "references": references,
}
