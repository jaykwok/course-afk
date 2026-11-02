"""External activities own fresh I/O and browser finally blocks on every entry."""

from __future__ import annotations

from core.workflow.contracts import TaskOutcome, Status


def _task(host, payload):
    host.authorize(payload)
    return host.repository.task(payload["task_id"])


async def learn(host, payload):
    from core.browser.session import create_browser_context, is_controller_window_closed
    from core.learning.afk_runner import _process_url
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
        result = await _process_url(
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
    from core.app.workflows import collect_learning_links_from_entry_urls
    from core.queues.learning import (
        read_learning_failures,
        remove_learning_failure,
        record_learning_failure,
    )
    from core.links import is_learning_zone_url

    task = _task(host, payload)
    details = {}
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
                    )
                finally:
                    await close_safely(page.close(), label="discovery class page")
            elif task["kind"] == "subject":
                details = await expand_and_append_subject_urls(
                    [task["url"]], context=context, status_callback=host.status_callback
                )
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
                else:
                    details = {
                        "learning_added": await collect_learning_links_from_learning_zone_urls(
                            [task["url"]],
                            context=context,
                            status_callback=host.status_callback,
                        )
                    }
            else:
                counts = await collect_learning_links_from_entry_urls(
                    [task["url"]], context=context, status_callback=host.status_callback
                )
                details = {
                    "manual_record_count": counts[1],
                    "manual_exam_record_count": counts[2],
                }
        remove_learning_failure(task["url"], reason="discovery_incomplete")
        failures = [e for e in read_learning_failures() if e.url == task["url"]]
        children = [r["task_id"] for r in host.repository.children(task["task_id"])]
        return TaskOutcome(
            task["task_id"],
            Status.WAITING_CHILDREN
            if children
            else Status.NEEDS_REVIEW
            if failures
            else Status.COMPLETED,
            [e.reason for e in failures],
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
    from core.exam.runner import (
        _build_exam_client,
        _build_ai_exam_model_config,
        classify_exam_entry_url,
        _run_course_ai_exam,
        _run_paper_ai_exam,
        _to_manual,
        _close_page_safely,
        create_browser_context,
        EXAM_URLS_FILE,
    )
    from core.queues.exam import has_ai_failed_model_config
    from core.runtime import close_safely

    task = _task(host, payload)
    kind = classify_exam_entry_url(task["url"])
    if kind not in {"course", "exam"}:
        _to_manual(task["url"], "unknown_url_type", "此入口需要人工展开或核对")
        return TaskOutcome(task["task_id"], Status.NEEDS_REVIEW, ["unknown_url_type"])
    client, model = _build_exam_client()
    page = None
    try:
        config = _build_ai_exam_model_config(model, client)
        allow_submit = payload["options"].get(
            "auto_submit", False
        ) and not has_ai_failed_model_config(
            task["url"], config, file_path=EXAM_URLS_FILE
        )
        async with create_browser_context() as (_, context):
            try:
                page = await context.new_page()
                await page.goto(task["url"], timeout=30000)
                result = await (
                    _run_course_ai_exam if kind == "course" else _run_paper_ai_exam
                )(page, task["url"], client, model, auto_submit=allow_submit)
            finally:
                await _close_page_safely(page)
        if isinstance(result, TaskOutcome):
            result.task_id = task["task_id"]
        else:
            raise TypeError("考试活动必须返回 TaskOutcome")
        return result
    finally:
        await close_safely(client.close(), label="AI client")


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
