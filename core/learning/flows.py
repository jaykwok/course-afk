from __future__ import annotations

import asyncio
import logging
import traceback

from core.abort import (
    LearningFlowError,
    NoPermissionError,
    PartialCourseFailure,
    SectionActivationError,
)
from core.browser.session import is_page_browser_connected, is_target_closed_exception
from core.config import (
    PAPER_EXAM_ATTEMPT_THRESHOLD,
    URL_TYPE_WAIT,
)
from core.exam.routing import queue_exam_url_by_attempt_text
from core.browser.overlays import dismiss_topmost_overlays_async
from core.learning.common import (
    ensure_course_page_ready,
    get_course_url,
    is_learned,
    timer,
    wait_for_course_section_focus,
)
from core.learning.exam_bridge import check_exam_passed, handle_examination
from core.learning.exam_api import queue_course_exams_from_api
from core.learning.handlers import handle_document, handle_h5, handle_video
from core.queues.learning import record_learning_failure
from core.learning.popups import handle_archive_continue_popup, handle_rating_popup


async def _open_subject_item_popup(page, learn_item, *, attempts: int = 3):
    """点击主题小节操作区并等待 popup；手动能开但自动化偶发失败时重试。"""
    operation = learn_item.locator(".inline-block.operation")
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            await dismiss_topmost_overlays_async(page, max_count=2)
        except Exception:
            pass

        try:
            await operation.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass

        try:
            await operation.wait_for(state="visible", timeout=8000)
        except Exception as exc:
            last_error = exc
            logging.info(
                f"主题操作按钮不可见 (第{attempt}/{attempts}次): {exc}"
            )
            await page.wait_for_timeout(500)
            continue

        try:
            async with page.expect_popup(timeout=15000) as page_pop:
                try:
                    await operation.click(timeout=5000)
                except Exception:
                    # 被遮罩/动画挡住时强制点一次
                    await operation.click(timeout=5000, force=True)
            popup = await page_pop.value
            return popup
        except Exception as exc:
            last_error = exc
            logging.info(
                f"打开主题小节弹窗失败 (第{attempt}/{attempts}次): {exc}"
            )
            try:
                await dismiss_topmost_overlays_async(page, max_count=2)
            except Exception:
                pass
            await page.wait_for_timeout(800)

    assert last_error is not None
    raise last_error


async def _activate_course_section(page_detail, box) -> None:
    """点击课内章节并确认目标 box 已获得 focus；始终重试同一目标。"""
    wrapper = box.locator(".section-item-wrapper")
    last_error: Exception | None = None

    for attempt in range(1, 4):
        try:
            await handle_rating_popup(page_detail)
        except Exception:
            pass
        try:
            await dismiss_topmost_overlays_async(page_detail, max_count=2)
        except Exception:
            pass

        try:
            await wrapper.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass

        try:
            await wrapper.wait_for(state="visible", timeout=10000)
            try:
                await wrapper.click(timeout=5000)
            except Exception:
                await wrapper.click(timeout=5000, force=True)
            if await wait_for_course_section_focus(
                page_detail,
                box,
                timeout_ms=5000,
            ):
                return
            last_error = RuntimeError(
                "目标章节点击后未进入 focus 状态"
            )
            logging.info(
                f"章节切换未生效 (第{attempt}/3次)，重新点击同一目标"
            )
        except Exception as exc:
            if is_target_closed_exception(exc):
                raise
            last_error = exc
            logging.info(
                f"点击章节失败 (第{attempt}/3次): {exc}"
            )
        await page_detail.wait_for_timeout(600)

    assert last_error is not None
    detail: dict[str, object] = {
        "last_error": f"{type(last_error).__name__}: {str(last_error)[:300]}",
    }
    for attribute, key in (
        ("id", "target_id"),
        ("class", "target_class"),
        ("data-sectiontype", "section_type"),
    ):
        try:
            detail[key] = await box.get_attribute(attribute)
        except Exception:
            detail[key] = None
    raise SectionActivationError(detail=detail)


async def _ensure_course_learning_view(page_detail) -> bool:
    """课程详情默认进入 AI 伴学时，切回可记录进度的课程学习视图。

    新版详情页的两个视图共用章节列表，但 AI 伴学视图不展示
    ``需学/需再学`` 时长，也不能可靠触发原课程学习的进度同步。
    旧版页面没有此切换标签，因此找不到时直接沿用原流程。
    """
    try:
        course_tabs = page_detail.locator(".guide-tab > span").filter(
            has_text="课程学习"
        )
        tab_count = await course_tabs.count()
    except Exception:
        return False

    last_error: Exception | None = None
    for index in range(tab_count):
        tab = course_tabs.nth(index)
        try:
            if not await tab.is_visible():
                continue
            tab_classes = (await tab.get_attribute("class") or "").split()
            if "guide-tab--selected" in tab_classes:
                logging.debug("课程学习页面已处于激活状态")
                return False
            try:
                await tab.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            try:
                await tab.click(timeout=5000)
            except Exception:
                await tab.click(timeout=5000, force=True)
            # 这是 SPA 内部视图切换，没有 load 事件；给章节进度文案一次渲染机会。
            await page_detail.wait_for_timeout(800)
            logging.info("已切换到课程学习页面")
            return True
        except Exception as exc:
            if is_target_closed_exception(exc):
                raise
            last_error = exc

    if last_error is not None:
        raise RuntimeError(f"无法切换到课程学习页面: {last_error}") from last_error
    return False


async def handle_subject_exam_item(learn_item) -> str | None:
    status_texts = [
        status.strip()
        for status in await learn_item.locator("span.finished-status").all_inner_texts()
        if status.strip()
    ]
    completion_status = next((status for status in status_texts if "已完成" in status), None)
    if completion_status == "已完成":
        logging.info("学习主题考试已完成, 跳过")
        return None

    exam_url = await get_course_url(learn_item, section_type="exam")
    attempt_texts = list(status_texts)
    for selector in (".inline-block.operation",):
        try:
            locator = learn_item.locator(selector)
            if await locator.count() > 0:
                text = (await locator.inner_text()).strip()
                if text:
                    attempt_texts.append(text)
        except Exception:
            pass
    try:
        item_text = (await learn_item.inner_text()).strip()
        if item_text:
            attempt_texts.append(item_text)
    except Exception:
        pass

    destination = queue_exam_url_by_attempt_text(
        exam_url,
        "\n".join(attempt_texts),
        threshold=PAPER_EXAM_ATTEMPT_THRESHOLD,
    )
    logging.info(f"学习主题考试类型, 已存入{'人工' if destination == 'manual' else 'AI'}考试队列")
    return exam_url


async def is_subject_item_completed(learn_item) -> bool:
    """
    主题小节是否已学完。

    实勘（subject/detail，含课/考/URL）：
    - 已完成课程/URL：操作区文案「重新学习」+ `.iconfont.m-right.icon-reload`
    - 已完成考试：操作区文案「考试记录」+ 同上 reload 图标；
      `span.finished-status` 常见「已完成」「成绩：xx」
    - 未完成：操作区「开始学习」/「继续学习」（无 reload 图标）

    优先看 reload 图标；再兜底操作文案与 finished-status。
    """
    if await learn_item.locator(".iconfont.m-right.icon-reload").count() > 0:
        return True

    try:
        status_texts = [
            status.strip()
            for status in await learn_item.locator(
                "span.finished-status"
            ).all_inner_texts()
            if status.strip()
        ]
        if any("已完成" in status for status in status_texts):
            return True
    except Exception:
        pass

    operation = learn_item.locator(".inline-block.operation")
    try:
        if await operation.count() == 0:
            return False
        text = (await operation.inner_text() or "").replace("\n", " ").strip()
    except Exception:
        return False
    # 课程/URL 完成；考试完成显示「考试记录」而非「重新学习」
    return "重新学习" in text or "考试记录" in text


def _record_structured_failure(url: str, exc: BaseException, *, detail: dict | None = None) -> None:
    """按异常类型写入失败链接。"""
    merged = dict(detail or {})
    if isinstance(exc, NoPermissionError):
        reason = getattr(exc, "reason", None) or "no_permission"
        reason_text = (
            getattr(exc, "reason_text", None)
            or str(exc)
            or "无权限访问该学习资源，已从课程链接清理"
        )
        record_learning_failure(
            url, reason=reason, reason_text=reason_text, detail=merged
        )
        return
    if isinstance(exc, LearningFlowError):
        merged.update(getattr(exc, "detail", None) or {})
        record_learning_failure(
            url,
            reason=exc.reason,
            reason_text=exc.reason_text,
            detail=merged,
        )
        return
    record_learning_failure(
        url,
        reason="retryable_error",
        reason_text=f"主题内课程处理失败，后续可重新加入课程链接: {exc}",
        detail=merged,
    )


async def subject_learning(page):
    """
    主题内容学习（DOM 文案分流：课程 / URL / 考试 / 调研…）。

    注意：主题页 section-type 文案与课程内 data-sectiontype 数字是两套体系，
    切勿混用。主题 API 侧（chapter-progress）见 core.discovery.subject_parse：
    10=课、9=考、3=外链 URL。收集链接优先 API；挂机进度仍以 DOM 为准。

    已学完小节（「重新学习」）直接跳过；残留主题走 DOM 时，先前已挂完的课/考
    通常已是「重新学习」，不会二次全挂。课内 100% 也会在 course_learning 快跳。
    """
    await page.wait_for_load_state("networkidle")

    await ensure_course_page_ready(page)

    await page.locator(".item.current-hover").last.wait_for()
    await page.locator(".item.current-hover").locator(".section-type").last.wait_for()

    learn_locator = page.locator(".item.current-hover")
    learn_count = await learn_locator.count()

    has_failed_course = False
    subject_course_failures: list[dict[str, object]] = []
    for i in range(learn_count):
        learn_item = learn_locator.nth(i)
        if await is_subject_item_completed(learn_item):
            logging.info("主题小节已完成（重新学习），跳过")
            continue

        section_type = await learn_item.locator(".section-type").inner_text()

        if section_type == "课程":
            page_detail = await _open_subject_item_popup(page, learn_item)
            try:
                await course_learning(page_detail, learn_item)
            except Exception as exc:
                if is_target_closed_exception(exc):
                    if is_page_browser_connected(page_detail):
                        logging.info("当前课程标签页已关闭，跳过该课程")
                        has_failed_course = True
                        subject_course_failures.append(
                            {
                                "subject_item_index": i,
                                "reason": "target_closed",
                                "reason_text": "课程标签页被关闭",
                            }
                        )
                        continue
                    raise
                if isinstance(exc, NoPermissionError):
                    logging.warning(f"主题内课程不可访问: {exc}")
                elif isinstance(exc, LearningFlowError):
                    logging.error(
                        f"主题内课程失败 [{exc.reason}]: {exc.reason_text}"
                    )
                else:
                    logging.error(f"发生错误: {str(exc)}")
                    logging.error(traceback.format_exc())
                course_url = await get_course_url(learn_item)
                _record_structured_failure(
                    course_url, exc, detail={"source": "subject_course"}
                )
                if isinstance(exc, NoPermissionError):
                    logging.info(
                        f"主题内课程不可访问，已记入失败链接并跳过: {course_url}"
                    )
                else:
                    # 可恢复失败：记失败后继续后续小节，结束时再统一抛错
                    has_failed_course = True
                    if isinstance(exc, LearningFlowError):
                        reason = exc.reason
                        reason_text = exc.reason_text
                        error_detail = getattr(exc, "detail", None) or {}
                    else:
                        reason = "retryable_error"
                        reason_text = str(exc)[:500]
                        error_detail = {"error_type": type(exc).__name__}
                    subject_course_failures.append(
                        {
                            "subject_item_index": i,
                            "course_url": course_url,
                            "reason": reason,
                            "reason_text": reason_text,
                            "detail": error_detail,
                        }
                    )
            finally:
                await page_detail.close()

        elif section_type == "URL":
            logging.info("URL学习类型, 记录为待复查")
            # record_learning_failure 按 URL 合并，重复调用会刷新文案，不堆多条
            record_learning_failure(
                page.url,
                reason="url_type_pending",
                reason_text="URL 类型学习等待后续复查（打开外链停留后复查是否「重新学习」）",
                detail={"source": "subject", "section_type": section_type},
            )
            page_detail = await _open_subject_item_popup(page, learn_item)
            timeout_task = asyncio.create_task(
                page_detail.wait_for_timeout(URL_TYPE_WAIT * 1000)
            )
            timer_task = asyncio.create_task(
                timer(URL_TYPE_WAIT, description="URL 类型学习等待")
            )
            try:
                await asyncio.gather(timeout_task, timer_task)
            finally:
                for task in (timeout_task, timer_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(timeout_task, timer_task, return_exceptions=True)
                try:
                    await page_detail.close()
                except Exception:
                    pass

        elif section_type == "考试":
            await handle_subject_exam_item(learn_item)

        elif section_type == "调研":
            logging.info("调研学习类型, 记录为需要人工处理")
            record_learning_failure(
                await get_course_url(learn_item),
                reason="survey_manual_required",
                reason_text="调研类型学习需要人工处理",
                detail={"source": "subject", "section_type": section_type},
            )

        else:
            logging.info("非课程及考试类学习类型, 记录为需要人工处理")
            record_learning_failure(
                page.url,
                reason="other_learning_type",
                reason_text=f"非课程及考试类学习类型: {section_type}",
                detail={"source": "subject", "section_type": section_type},
            )

    if has_failed_course:
        raise PartialCourseFailure(
            "部分主题课程学习失败",
            reason="partial_course_failure",
            reason_text="部分主题课程学习失败，后续可重新加入课程链接",
            detail={"course_failures": subject_course_failures},
        )


async def _collect_chapter_boxes(page_detail) -> list[tuple[str, object]]:
    """章节列表：先必修后选修。返回 [(track, box), ...]。"""
    # 先等任意章节列表出现（归档/限流已在外层处理）
    try:
        await page_detail.locator("dl.chapter-list-box").first.wait_for(timeout=15000)
    except Exception:
        # 兼容仅有 required 类的旧页
        await page_detail.locator("dl.chapter-list-box.required").last.wait_for(
            timeout=5000
        )

    ordered: list[tuple[str, object]] = []
    required = page_detail.locator("dl.chapter-list-box.required")
    n_req = await required.count()
    for i in range(n_req):
        ordered.append(("required", required.nth(i)))

    elective = page_detail.locator("dl.chapter-list-box.elective")
    n_elec = await elective.count()
    if n_elec == 0:
        # 无 .elective 类时：非 required 的 chapter-list-box 视为选修
        others = page_detail.locator(
            "dl.chapter-list-box:not(.required)"
        )
        n_elec = await others.count()
        for i in range(n_elec):
            ordered.append(("elective", others.nth(i)))
    else:
        for i in range(n_elec):
            ordered.append(("elective", elective.nth(i)))

    return ordered


async def course_learning(page_detail, learn_item=None):
    """课程内容学习：必修章节优先，再挂选修。"""
    await page_detail.wait_for_load_state("load")

    # 归档课弹窗会挡住章节列表，须在任何章节 wait 之前点「继续学习」
    if await handle_archive_continue_popup(page_detail):
        logging.info("已确认继续学习归档课程")

    # 访问权限 / 资源不存在 / 并发限流：须在等进度条与章节列表之前
    await ensure_course_page_ready(page_detail)

    # 新版详情页可能默认落在 AI 伴学；必须先回到原课程学习视图，
    # 后续章节文案才包含剩余时长，且播放进度会进入原同步链路。
    await _ensure_course_learning_view(page_detail)

    if await handle_rating_popup(page_detail):
        logging.info("五星评价完成")

    if await _is_course_completed(page_detail):
        try:
            title = await page_detail.locator("span.course-title-text").inner_text(
                timeout=3000
            )
        except Exception:
            title = page_detail.url
        logging.info(f"<{title}>已学习完毕, 跳过该课程\n")
        return

    chapters = await _collect_chapter_boxes(page_detail)
    if not chapters:
        logging.warning("未找到章节列表（必修/选修）")
        return

    course_exam_api_result = await queue_course_exams_from_api(page_detail)
    course_exams_handled_by_api = bool(
        course_exam_api_result and course_exam_api_result.discovered > 0
    )
    if course_exams_handled_by_api:
        logging.info(
            "课程内考试已通过接口处理: "
            f"发现 {course_exam_api_result.discovered}，"
            f"AI +{course_exam_api_result.ai_queued}，"
            f"人工 +{course_exam_api_result.manual_queued}，"
            f"已完成 {course_exam_api_result.completed}"
        )

    # 预检：可判定类型是否全部已学
    all_learned = True
    has_non_detectable_types = False
    for track, box in chapters:
        section_type = await box.get_attribute("data-sectiontype")
        if section_type in ["1", "2", "3", "5", "6"]:
            progress_text = await box.locator(".section-item-wrapper").inner_text()
            if not is_learned(progress_text):
                all_learned = False
                break
        else:
            has_non_detectable_types = True

    if all_learned and not has_non_detectable_types:
        logging.info("所有章节（含选修）已学习完毕, 跳过该课程")
        return

    # 课程内 data-sectiontype（与主题 chapter-progress 的 sectionType 数字含义不同）:
    # 1/2/3=文档网页, 4=H5, 5/6=视频, 9=课程内考试。主题外链 URL 的 API 类型 3 不在此列。
    has_failed_box = False
    chapter_failures: list[dict[str, object]] = []
    for index, (track, box) in enumerate(chapters):
        section_type = await box.get_attribute("data-sectiontype")
        box_text = await box.locator(".text-overflow").inner_text()
        track_label = "必修" if track == "required" else "选修"
        logging.info(f"课程信息[{track_label}]: \n{box_text}\n")

        if section_type in ["1", "2", "3", "5", "6"]:
            progress_text = await box.locator(".section-item-wrapper").inner_text()
            if is_learned(progress_text):
                logging.info(f"课程第{index + 1}节({track_label})已学习, 跳过该节\n")
                continue

        if section_type == "9" and course_exams_handled_by_api:
            logging.info("课程内考试已由接口完成状态判断与入队，跳过页面解析")
            continue

        try:
            await _activate_course_section(page_detail, box)
            if section_type in ["5", "6"]:
                logging.info("该课程为视频类型")
                await handle_video(box, page_detail)
            elif section_type in ["1", "2", "3"]:
                logging.info("该课程为文档、网页类型")
                await handle_document(page_detail, box)
            elif section_type == "4":
                logging.info("该课程为h5类型")
                await handle_h5(page_detail, learn_item)
            elif section_type == "9":
                logging.info("该课程为考试类型")
                exam_passed = await check_exam_passed(page_detail)
                if exam_passed:
                    logging.info("考试已通过, 跳过该节")
                    continue
                if learn_item:
                    await handle_examination(
                        page_detail,
                        learn_item,
                        exam_passed=exam_passed,
                    )
                else:
                    await handle_examination(page_detail, exam_passed=exam_passed)
            else:
                logging.info("未知课程学习类型, 记录为需要人工处理")
                failure_url = (
                    await get_course_url(learn_item) if learn_item else page_detail.url
                )
                record_learning_failure(
                    failure_url,
                    reason="unknown_learning_type",
                    reason_text=f"未知课程学习类型: {section_type}",
                    detail={
                        "source": "course_chapter",
                        "section_type": section_type,
                        "track": track,
                    },
                )
                continue
        except Exception as exc:
            if is_target_closed_exception(exc):
                raise
            if isinstance(exc, NoPermissionError):
                raise
            if isinstance(exc, LearningFlowError) and not isinstance(
                exc, PartialCourseFailure
            ):
                # 同步超时等：记入失败后继续其它节，避免一节拖死整课
                logging.error(f"课程第{index + 1}节({track_label})学习失败: {exc}")
                failure_url = (
                    await get_course_url(learn_item) if learn_item else page_detail.url
                )
                failure_detail = {
                    "source": "course_chapter",
                    "section_index": index,
                    "section_number": index + 1,
                    "section_type": section_type,
                    "track": track,
                }
                _record_structured_failure(
                    failure_url,
                    exc,
                    detail=failure_detail,
                )
                chapter_failures.append(
                    {
                        **failure_detail,
                        "reason": exc.reason,
                        "reason_text": exc.reason_text,
                        "detail": getattr(exc, "detail", None) or {},
                    }
                )
                has_failed_box = True
                continue
            logging.error(f"课程第{index + 1}节({track_label})学习失败: {str(exc)}")
            logging.error(traceback.format_exc())
            chapter_failures.append(
                {
                    "source": "course_chapter",
                    "section_index": index,
                    "section_number": index + 1,
                    "section_type": section_type,
                    "track": track,
                    "reason": "retryable_error",
                    "reason_text": str(exc)[:500],
                    "error_type": type(exc).__name__,
                }
            )
            has_failed_box = True
            continue
        logging.info(f"课程第{index + 1}节({track_label})学习完毕")

    if has_failed_box:
        raise PartialCourseFailure(
            "部分章节学习失败",
            detail={"chapter_failures": chapter_failures},
        )


async def _is_course_completed(page) -> bool:
    """课程进度 100% 则视为整课完成。

    超时故意短于 Playwright 默认 30s，避免错误页/限流页空等；
    又略放宽以照顾云电脑/弱单核（渲染进度条偏慢）。读不到则当未完成。
    """
    progress_element = page.locator("div.course-progress div.progress")
    try:
        # 可见：12s；读文案：5s（合计仍远小于默认 30s）
        await progress_element.first.wait_for(state="visible", timeout=12000)
        progress_text = await progress_element.inner_text(timeout=5000)
    except Exception:
        return False
    return "100%" in (progress_text or "")
