"""One bounded traversal for catalogs, classes, subjects, courses and exams."""
from __future__ import annotations

import asyncio

from core.abort import UserAbortRequested, UserCancelRequested, WafBlockError, raise_for_batch_stop
from core.auth.access import CredentialUnavailableError
from core.discovery.contracts import CollectionResult, DiscoveryManifest
from core.file_ops import is_exam_url, is_subject_detail_url, is_train_class_url, is_course_detail_url
from core.links import is_learning_zone_url, unique_urls
from core.queues.learning import (
    append_learning_urls, remember_discovery_entries, record_learning_failure,
    remove_learning_failure, read_learning_failures,
)
from core.queues.exam import append_exam_urls
from core.runtime import close_safely
from core.tasks.identity import canonical_url
from core.tasks.relations import record_discovered_tasks, record_local_outcome


def _leaf(url):
    return is_course_detail_url(url) or is_exam_url(url)


async def read_entry_manifest(page, context, url, *, nested, status_callback=None):
    """Type-specific readers return children; only the traversal enqueues them."""
    if is_subject_detail_url(url):
        from core.discovery.subject_parse import expand_subject_from_page
        result = await expand_subject_from_page(page, url, status_callback=status_callback)
        return DiscoveryManifest(
            tuple(result.course_urls + result.exam_urls), result.complete, "subject_api",
            "" if result.complete else "主题章节清单尚未完整确认，请重新解析入口",
            result.residual_subject_url,
            {"section_total": result.section_total, "url_section_count": result.url_section_count,
             "unknown_section_types": list(result.unknown_section_types)},
        )
    if is_train_class_url(url):
        from core.discovery.train_class import collect_learning_links_from_class_page
        links = await collect_learning_links_from_class_page(
            page, url, status_callback=status_callback, allow_enrollment=not nested,
        )
        return DiscoveryManifest(tuple(links), True, "class_api")
    if is_learning_zone_url(url):
        from core.learning.zone import read_learning_zone_manifest
        return await read_learning_zone_manifest(page, context, url, status_callback)
    return DiscoveryManifest(message="无法自动识别该入口，请使用手动选择模式")


async def collect_discovery_entries(urls, *, context, status_callback=None, max_entries=100, max_depth=12):
    """Traverse once per canonical URL, with one borrowed context and owned tabs."""
    from core.discovery.train_class import ClassEnrollmentError
    from core.browser.session import is_target_closed_exception
    from core.diagnostics import redact_text

    roots = unique_urls([canonical_url(u) for u in urls])
    # Retain every pending container before attempting any browser operation.
    remember_discovery_entries([u for u in roots if not _leaf(u)])
    visited = {}
    local_complete = {}
    active = set()
    issues = []
    expanded = 0
    stats = dict(learning_added=0, exam_added=0, course_count=0, exam_count=0,
                 subject_count=0, class_count=0, entry_count=0, residual_count=0)

    def report(message):
        if status_callback:
            status_callback(message)

    def fail(url, message, *, reason="discovery_incomplete", detail=None):
        info = {"url": url, "reason": reason, "message": message, **(detail or {})}
        issues.append(info)
        record_learning_failure(url, reason=reason, reason_text=message, detail=detail or {})
        report(message)
        return info

    async def visit(url, depth=0):
        nonlocal expanded
        if url in visited:
            return visited[url]
        if is_course_detail_url(url):
            stats["course_count"] += 1
            stats["learning_added"] += len(append_learning_urls([url]))
            visited[url] = True
            return True
        if is_exam_url(url):
            stats["exam_count"] += 1
            stats["exam_added"] += len(append_exam_urls([url]))
            visited[url] = True
            return True
        if depth > max_depth or expanded >= max_entries:
            fail(url, "子入口层级或数量达到解析上限，请单独添加该入口",
                 detail={"depth": depth, "max_depth": max_depth, "max_entries": max_entries})
            visited[url] = False
            return False
        expanded += 1
        active.add(url)
        report(f"正在解析入口：{url}")
        page = None
        manifest = None
        try:
            page = await context.new_page()
            manifest = await read_entry_manifest(page, context, url, nested=depth > 0, status_callback=status_callback)
        except (UserAbortRequested, UserCancelRequested, WafBlockError, CredentialUnavailableError, asyncio.CancelledError):
            raise
        except ClassEnrollmentError as exc:
            fail(url, str(exc), reason="enrollment_blocked", detail={"source": "class_api"})
            remove_learning_failure(url, reason="discovery_incomplete", keep_file=True)
            record_local_outcome(url, "BLOCKED", source="entry_discovery", reason_codes=["enrollment_blocked"],
                                 details={"discovery_complete": False, "message": str(exc)})
            visited[url] = False
            return False
        except Exception as exc:
            raise_for_batch_stop(exc)
            if is_target_closed_exception(exc):
                raise UserCancelRequested("收集页面已关闭，入口已保留供重试") from None
            message = f"入口解析失败（{type(exc).__name__}）：{redact_text(str(exc))[:300]}"
            fail(url, message, detail={"exception_type": type(exc).__name__})
            # A browser startup failure should propagate, not loop over all inputs.
            if page is None:
                raise
            record_local_outcome(url, "NEEDS_REVIEW", source="entry_discovery", reason_codes=["discovery_incomplete"],
                                 details={"discovery_complete": False, "message": message})
            visited[url] = False
            return False
        finally:
            if page is not None:
                await close_safely(page.close(), label="discovery page")
            if manifest is None:
                active.discard(url)

        try:
            kind = "subject" if is_subject_detail_url(url) else "class" if is_train_class_url(url) else "entry"
            stats[kind + "_count"] += 1
            children = unique_urls([canonical_url(child) for child in manifest.links])
            # Ignore back-links as navigation; don't add cyclic task dependencies.
            children = [child for child in children if child not in active]
            accepted = []
            for child in children:
                try:
                    record_discovered_tasks([child], parent_url=url, source=manifest.source)
                except ValueError as exc:
                    if str(exc) != "任务关系不能形成循环":
                        raise
                    continue  # A back-link can also point into a previously saved graph.
                accepted.append(child)
            remember_discovery_entries([child for child in accepted if not _leaf(child) and child not in visited])
            # Save known leaf work before a later container can fail or cancel.
            for child in accepted:
                if _leaf(child):
                    await visit(child, depth + 1)
            child_issues = []
            for child in accepted:
                if not await visit(child, depth + 1):
                    child_issues.append(next((item for item in issues if item["url"] == child),
                                             {"url": child, "message": "子入口尚未完整解析"}))
            complete = manifest.complete and not child_issues
            if manifest.residual_subject_url:
                stats["residual_count"] += 1
                stats["learning_added"] += len(append_learning_urls([manifest.residual_subject_url]))
            details = {**manifest.details, "discovery_complete": complete,
                       "local_work_complete": complete and not manifest.residual_subject_url,
                       "residual_count": int(bool(manifest.residual_subject_url)),
                       "child_issues": child_issues}
            local_complete[url] = details["local_work_complete"]
            if complete:
                remove_learning_failure(url, reason="discovery_incomplete", keep_file=True)
                remove_learning_failure(url, reason="enrollment_blocked", keep_file=True)
            else:
                details["message"] = manifest.message or (
                    "子入口尚未完成：" + child_issues[0]["message"] if child_issues else "入口清单尚未完整确认"
                )
                fail(url, details["message"], detail=details)
            failures = [f for f in read_learning_failures() if f.url == url]
            record_local_outcome(
                url, "NEEDS_REVIEW" if failures else "WAITING_CHILDREN" if manifest.residual_subject_url else "COMPLETED",
                source=manifest.source,
                reason_codes=[f.reason for f in failures] or (["subject_residual"] if manifest.residual_subject_url else []),
                evidence_refs=[manifest.source] if manifest.complete else [], details=details,
            )
            visited[url] = complete
            return complete
        finally:
            active.discard(url)

    complete = True
    for root in roots:
        if _leaf(root):
            await visit(root)
    for root in roots:
        complete = await visit(root) and complete
    report(f"入口解析结束：{stats['course_count']} 条课程、{stats['exam_count']} 场考试，学习队列 +{stats['learning_added']}")
    root_issue = next((i for i in issues if i["url"] in roots), None)
    return CollectionResult(stats["learning_added"], complete,
                            tuple(dict.fromkeys(i["reason"] for i in issues)),
                            {**stats, "issues": issues, "message": root_issue["message"] if root_issue else "",
                             "local_work_complete": complete and all(local_complete.get(u, True) for u in roots)})
