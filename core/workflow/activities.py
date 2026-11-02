"""External activities own fresh I/O and browser finally blocks on every entry."""

from __future__ import annotations

from core.workflow.contracts import TaskOutcome, Status


def _task(host, payload):
    host.authorize(payload)
    return host.repository.task(payload["task_id"])


async def learn(host, payload):
    from core.browser.session import create_browser_context, is_controller_window_closed
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
        if is_controller_window_closed(context):
            result.stop_scope = "batch"
            result.details["message"] = "心跳页已关闭，当前课程结束，剩余任务保留"
    return result


async def discover(host, payload):
    from core.browser.session import create_browser_context
    from core.discovery.train_class import (
        ClassEnrollmentError,
        collect_learning_links_from_class_page,
    )
    from core.discovery.subject_parse import (
        expand_and_append_subject_urls,
        enqueue_learning_links_with_subject_expand,
    )
    from core.learning.zone import collect_learning_links_from_learning_zone_urls
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
            if task["kind"] == "class":
                from core.runtime import close_safely

                page = await context.new_page()
                try:
                    links = await collect_learning_links_from_class_page(
                        page, task["url"], status_callback=host.status_callback
                    )
                    details = await enqueue_learning_links_with_subject_expand(
                        links,
                        page=page,
                        status_callback=host.status_callback,
                        source_label="培训班",
                        parent_url=task["url"],
                    )
                    discovery_complete = details.get("complete") is True
                finally:
                    await close_safely(page.close(), label="discovery class page")
            elif task["kind"] == "subject":
                details = await expand_and_append_subject_urls(
                    [task["url"]], context=context, status_callback=host.status_callback
                )
                discovery_complete = details.get("complete") is True
            elif is_learning_zone_url(task["url"]):
                if payload["options"].get("zone_mode", "auto") == "manual":
                    counts = await collect_learning_links_from_entry_urls(
                        [task["url"]],
                        context=context,
                        status_callback=host.status_callback,
                    )
                    details = {
                        "manual_record_count": counts[1],
                        "manual_exam_record_count": counts[2],
                    }
                    discovery_complete = True
                else:
                    result = await collect_learning_links_from_learning_zone_urls(
                        [task["url"]],
                        context=context,
                        status_callback=host.status_callback,
                        detailed=True,
                    )
                    from core.discovery.contracts import CollectionResult

                    discovery_complete = (
                        isinstance(result, CollectionResult) and result.complete
                    )
                    details = {
                        "learning_added": result.added
                        if isinstance(result, CollectionResult)
                        else result
                    }
            else:
                counts = await collect_learning_links_from_entry_urls(
                    [task["url"]], context=context, status_callback=host.status_callback
                )
                details = {
                    "manual_record_count": counts[1],
                    "manual_exam_record_count": counts[2],
                }
                discovery_complete = True
        details["discovery_complete"] = discovery_complete
        if discovery_complete:
            remove_learning_failure(task["url"], reason="discovery_incomplete")
            remove_learning_failure(task["url"], reason="enrollment_blocked")
        else:
            record_learning_failure(
                task["url"],
                reason="discovery_incomplete",
                reason_text="入口清单尚未完整确认，请重新解析；已发现的任务已保留",
            )
        failures = [e for e in read_learning_failures() if e.url == task["url"]]
        children = [r["task_id"] for r in host.repository.children(task["task_id"])]
        return TaskOutcome(
            task["task_id"],
            Status.NEEDS_REVIEW
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
