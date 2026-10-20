from __future__ import annotations

from core.abort import UserCancelRequested
from core.config import run_async


_AI_EXAM_URL_INPUT_PROMPTS = [
    "请粘贴考试链接或包含考试链接的文本，一行一个链接。",
    "识别到的链接会去重写入 考试链接.json，并立即开始 AI 自动考试。",
]

_REFERENCE_COLLECTION_PROMPTS = [
    "请粘贴知学云主题详情链接，一行一个。",
    "程序会读取课程列表，保存 PDF/文档课件，并跳过 MP4 视频本体。",
    "视频类资源会保存平台提供的 AI 导学/总结文本。",
    "下载完成后，如检测到 PDF，可选择使用 PP-OCRv6 转换为 Markdown。",
]


def _manual_selection_result_rows(result: dict[str, int]) -> list[tuple[str, str]]:
    return [
        ("识别到的输入链接", str(result["input_url_count"])),
        ("直接写入的学习链接", str(result["direct_learning_count"])),
        ("直接写入的考试链接", str(result["direct_exam_count"])),
        ("课程集合页链接数量", str(result["learning_zone_url_count"])),
        ("课程集合页自动解析数量", str(result["learning_zone_parsed_count"])),
        ("培训班链接数量", str(result.get("train_class_url_count", 0))),
        ("培训班自动解析数量", str(result.get("train_class_parsed_count", 0))),
        ("需要手动打开的入口链接", str(result["entry_url_count"])),
        ("浏览器记录的学习链接", str(result["manual_record_count"])),
        ("浏览器记录的考试链接", str(result["manual_exam_record_count"])),
        ("当前学习链接总数", str(result["learning_total"])),
        ("当前考试链接总数", str(result["exam_total"])),
    ]


def _begin_operation(ui, title: str, message: str) -> None:
    begin = getattr(ui, "begin_operation", None)
    if callable(begin):
        begin(title, message)


def _prompt_ai_exam_auto_submit(ui) -> bool:
    return ui.prompt_yes_no("AI考试是否自动交卷？", default="N")


def _ensure_exam_urls_for_ai_exam(ui) -> bool:
    from core.config import EXAM_URLS_FILE
    from core.queues.exam import append_exam_url, read_exam_urls
    from core.links import extract_urls_from_text

    existing_urls = read_exam_urls(EXAM_URLS_FILE)
    if existing_urls:
        return True

    ui.show_warning("当前未检测到考试链接")
    if not ui.prompt_yes_no("是否现在粘贴考试链接？", default="Y"):
        return False

    try:
        input_text = ui.prompt_multiline_input(
            _AI_EXAM_URL_INPUT_PROMPTS,
            title="粘贴考试链接",
            cancel_message="已取消粘贴考试链接",
        )
    except UserCancelRequested as exc:
        ui.show_warning(str(exc) or "已取消粘贴考试链接")
        return False

    urls = extract_urls_from_text(input_text)
    if not urls:
        ui.show_warning("输入内容中未识别到有效的 HTTP/HTTPS 链接")
        return False

    existing_url_set = set(existing_urls)
    for url in urls:
        append_exam_url(url, file_path=EXAM_URLS_FILE)

    added_count = sum(url not in existing_url_set for url in urls)
    total_count = len(read_exam_urls(EXAM_URLS_FILE))
    ui.show_success(
        f"已写入 {added_count} 条考试链接，当前共 {total_count} 条，准备开始 AI 自动考试"
    )
    return total_count > 0


def _maybe_delete_empty_learning_queue_file(ui) -> None:
    from core.config import LEARNING_URLS_FILE
    from core.file_ops import del_file
    from core.queues.learning import read_learning_urls

    if not LEARNING_URLS_FILE.exists():
        return
    if read_learning_urls(LEARNING_URLS_FILE):
        return

    del_file(LEARNING_URLS_FILE)
    ui.show_success("已删除空的课程链接.json")


def _maybe_delete_empty_exam_queue_file(ui) -> None:
    from core.config import EXAM_URLS_FILE
    from core.queues.exam import read_exam_urls
    from core.file_ops import del_file

    if not EXAM_URLS_FILE.exists():
        return
    if read_exam_urls(EXAM_URLS_FILE):
        return

    del_file(EXAM_URLS_FILE)
    ui.show_success("已删除空的考试链接.json")


def choose_learning_zone_mode(learning_zone_urls, prompt_choice_func) -> str:
    if not learning_zone_urls:
        return "manual"

    choice = prompt_choice_func(
        "检测到课程集合页（学习专区 / 案例库），请选择处理方式",
        [
            "全部学习：自动解析并写入学习链接",
            "手动选择学习模块：打开页面后自己点击课程",
        ],
    )
    return "auto" if choice == 1 else "manual"


_FLOW_RESULT_LABELS = {
    "credential": "凭证不可用，请更新登录凭证",
    "manual-selection": "未检测到学习链接，请手动选择课程或录入链接",
    "afk-only": "挂课完成，未检测到考试链接",
    "ai-not-configured": "未填写 AI 配置，已跳过 AI 自动考试（可改用人工考试）",
    "manual-exam-pending": "仍有人工考试或评卷结果待复查",
    "done": "全部流程完成",
    "blocked": "网站防护拦截，本轮停止；剩余待办已保留",
    "learning-pending": "本轮结束，仍有学习、失败复查或考试待办",
}


def handle_recommended_flow(ui) -> None:
    from core.exam.answers import ExamAiConfigurationError
    from core.app.workflows import run_recommended_flow

    _begin_operation(ui, "推荐流程", "正在检查登录状态与待处理任务")
    try:
        result = run_async(
            run_recommended_flow(
                status_callback=ui.show_info,
                ask_auto_submit=lambda: _prompt_ai_exam_auto_submit(ui),
            )
        )
    except ExamAiConfigurationError as exc:
        ui.show_error(str(exc))
        ui.pause()
        return

    _maybe_delete_empty_learning_queue_file(ui)
    _maybe_delete_empty_exam_queue_file(ui)
    label = _FLOW_RESULT_LABELS.get(result, result)
    ui.show_summary("推荐流程结果", [("流程状态", label)])
    ui.pause()


def handle_refresh_credential(state, ui, *, switch_account: bool = False) -> None:
    from core.auth.login import LoginNotCompletedError
    from core.app.workflows import refresh_credential

    if state.has_credential and not state.credential_expired:
        ui.show_warning("当前登录凭证仍有效，继续将覆盖现有登录状态")
    title = "更换登录账号" if switch_account else "更新登录凭证"
    _begin_operation(ui, title, "正在打开新的登录窗口，请完成登录")
    try:
        profile = run_async(refresh_credential(status_callback=ui.show_info, confirm_same_account=lambda old, new: ui.prompt_yes_no(f"已有 {old} 的待办；请确认当前登录的 {new} 是同一账号，或明确接管这些待办。", default="N")))
    except LoginNotCompletedError as exc:
        ui.show_warning(str(exc))
        ui.pause()
        return
    ui.show_success(f"登录凭证已更新，当前账号：{profile.label}")
    ui.pause()


def handle_account_management(state, ui) -> None:
    from core.app.menus import ACCOUNT_MENU_OPTIONS

    choice = ui.prompt_choice("账号与登录", ACCOUNT_MENU_OPTIONS)
    if choice == 1:
        handle_refresh_credential(state, ui, switch_account=True)
    elif choice == 2:
        handle_refresh_credential(state, ui)
    elif choice == 3:
        handle_show_login_state(state, ui)
    elif choice == 4:
        handle_clear_credentials(state, ui)


def handle_show_login_state(state, ui) -> None:
    from core.auth.credential import load_credential_metadata, load_pending_owner_metadata

    metadata = load_credential_metadata()
    owner = load_pending_owner_metadata()
    status = "未登录" if not state.has_credential else "已过期，请重新登录" if state.credential_expired else "有效"
    rows = [("登录状态", status), ("当前账号", metadata.account_label if metadata and state.has_credential else "未登录")]
    if metadata and state.has_credential:
        rows.extend([("保存时间", metadata.saved_at), ("到期时间", metadata.expires_at)])
    if owner:
        rows.append(("待办归属账号", owner.account_label or owner.account_name or owner.account_display_name))
    ui.show_summary("本地登录状态", rows)
    ui.pause()


def handle_clear_credentials(state, ui) -> None:
    from core.config import COOKIES_FILE, CREDENTIAL_META_FILE
    from core.auth.credential import clear_local_credentials, load_credential_metadata

    if not COOKIES_FILE.exists() and not CREDENTIAL_META_FILE.exists():
        ui.show_info("当前没有本地登录凭据")
        ui.pause()
        return
    metadata = load_credential_metadata()
    pending = any((state.learning_count, state.learning_failure_count, state.exam_count, state.manual_exam_count))
    rows = [("当前账号", metadata.account_label if metadata else "未登录或凭据异常"), ("清空内容", "本地 Cookie、登录信息与遗留登录元数据"), ("保留内容", "课程、失败记录、考试待办、考试历史及资料")]
    if pending:
        rows.append(("待办归属", "保留所属账号，重新登录仍会核对归属"))
    if not ui.prompt_summary_confirmation("清空本地登录凭据", rows, "确认清空？下次操作需要重新登录。", default="N"):
        ui.show_info("已取消清空登录凭据")
        return
    try:
        clear_local_credentials(preserve_pending_owner=pending)
    except OSError as exc:
        ui.show_error(f"清理登录凭据未完整完成，请重试：{exc}")
        ui.pause()
        return
    ui.show_success("本地登录凭据已清空，请重新登录")
    ui.pause()


def handle_show_learning_links(learning_urls_file, ui) -> None:
    from core.queues.learning import read_learning_urls

    links = read_learning_urls(learning_urls_file)
    if not links:
        ui.show_warning("课程链接.json 当前为空")
    else:
        ui.show_summary(
            "课程链接状态",
            [("课程链接总数", str(len(links))), ("首条课程链接", links[0])],
        )
    ui.pause()


def handle_clear_links(ui, *, include_exams: bool = False) -> None:
    from core.queues.manage import clear_link_queues, link_queue_paths, queue_record_count, snapshot_link_queues

    try:
        snapshot = snapshot_link_queues(include_exams)
    except OSError as exc:
        ui.show_error(f"无法读取待办，尚未清空：{exc}")
        ui.pause()
        return
    counts = {label: queue_record_count(snapshot[path]) for label, path in link_queue_paths(include_exams).items()}
    if all(count == 0 for count in counts.values()):
        ui.show_info("所选范围内没有待办或失败记录，无需清空")
        ui.pause()
        return
    title = "清空全部待办" if include_exams else "清空课程及失败记录"
    rows = [(label, str(count) if count is not None else "文件格式异常，仍可备份并清空") for label, count in counts.items()]
    rows.append(("保留内容", "登录凭证、考试提交历史、已保存的课程资料"))
    if not ui.prompt_summary_confirmation(title, rows, "确认清空以上记录？清空前会自动保存备份。", default="N"):
        ui.show_info("已取消清空，待办保持不变")
        return
    try:
        result = clear_link_queues(include_exams, expected_snapshot=snapshot)
    except (OSError, RuntimeError) as exc:
        ui.show_error(str(exc))
        ui.pause()
        return
    ui.show_summary("清空完成", [(label, "已清空") for label in result.counts] + [("备份位置", str(result.backup_dir))])
    ui.pause()


def handle_task_management(exam_urls_file, learning_urls_file, manual_exam_file, ui) -> None:
    from core.app.menus import TASK_MENU_OPTIONS

    choice = ui.prompt_choice("任务管理", TASK_MENU_OPTIONS)
    if choice == 1:
        handle_show_output_state(exam_urls_file, learning_urls_file, manual_exam_file, ui)
    elif choice == 2:
        handle_show_learning_links(learning_urls_file, ui)
    elif choice == 3:
        handle_clear_links(ui)
    elif choice == 4:
        handle_clear_links(ui, include_exams=True)


def handle_manual_selection(prompts, ui) -> None:
    from core.links import split_manual_selection_urls
    from core.app.workflows import parse_manual_selection_input, run_manual_course_selection

    try:
        input_text = ui.prompt_multiline_input(prompts)
    except UserCancelRequested as exc:
        ui.show_warning(str(exc) or "已取消手动选择课程 / 录入链接")
        ui.pause()
        return

    parsed_urls = parse_manual_selection_input(input_text)
    if not parsed_urls:
        ui.show_warning("输入内容中未识别到有效的 HTTP/HTTPS 链接")
        ui.pause()
        return

    (
        direct_learning_urls,
        direct_exam_urls,
        learning_zone_urls,
        train_class_urls,
        entry_urls,
    ) = split_manual_selection_urls(parsed_urls)
    confirmation_rows = [
        ("有效链接（去重）", str(len(parsed_urls))),
        ("课程 / 主题链接", str(len(direct_learning_urls))),
        ("考试链接", str(len(direct_exam_urls))),
        ("课程集合页（学习专区 / 案例库）", str(len(learning_zone_urls))),
        ("培训班链接（自动解析）", str(len(train_class_urls))),
        ("其他入口链接", str(len(entry_urls))),
    ]
    try:
        confirmed = ui.prompt_summary_confirmation(
            "链接解析确认",
            confirmation_rows,
            "确认按以上分类继续处理？",
            default="Y",
        )
    except UserCancelRequested as exc:
        ui.show_warning(str(exc) or "已取消处理本次链接")
        return
    if not confirmed:
        ui.show_warning("已取消处理本次链接")
        return

    learning_zone_mode = choose_learning_zone_mode(
        learning_zone_urls,
        prompt_choice_func=ui.prompt_choice,
    )
    _begin_operation(ui, "解析课程链接", "正在打开浏览器并处理已确认的链接")
    prepared_result_handle = None

    def prepare_result_page(result: dict[str, int]) -> None:
        nonlocal prepared_result_handle
        prepared_result_handle = ui.prepare_pause_with_summary(
            "链接解析完成",
            _manual_selection_result_rows(result),
            "解析完成，请确认结果",
        )

    result = run_async(
        run_manual_course_selection(
            input_text,
            learning_zone_mode=learning_zone_mode,
            status_callback=ui.show_info,
            result_ready_callback=prepare_result_page,
        )
    )
    if prepared_result_handle is None:
        prepare_result_page(result)
    ui.wait_prepared_prompt(prepared_result_handle)


def handle_afk(ui) -> None:
    from core.app.workflows import run_afk_workflow

    _begin_operation(ui, "课程学习", "正在打开浏览器并处理课程队列")
    has_exam = run_async(run_afk_workflow(status_callback=ui.show_info))
    _maybe_delete_empty_learning_queue_file(ui)
    if has_exam:
        ui.show_success("挂课完成，并检测到考试链接")
    else:
        ui.show_warning("挂课完成，未检测到考试链接")
    ui.pause()


def handle_ai_exam(ui) -> None:
    from core.config import is_ai_configured
    from core.exam.answers import ExamAiConfigurationError
    from core.app.workflows import run_ai_exam_workflow

    if not is_ai_configured():
        ui.show_warning(
            "未填写 AI 配置（OPENAI_COMPLETION_BASE_URL / OPENAI_COMPLETION_API_KEY / "
            "MODEL_NAME），无法使用 AI 自动考试。请在 .env 填写后重试，或改用人工考试。"
        )
        ui.pause()
        return

    if not _ensure_exam_urls_for_ai_exam(ui):
        ui.pause()
        return

    auto_submit = _prompt_ai_exam_auto_submit(ui)
    _begin_operation(ui, "AI 自动考试", "正在打开浏览器并处理考试队列")
    try:
        manual_count = run_async(
            run_ai_exam_workflow(
                status_callback=ui.show_info,
                auto_submit=auto_submit,
            )
        )
    except ExamAiConfigurationError as exc:
        ui.show_error(str(exc))
        ui.pause()
        return
    _maybe_delete_empty_exam_queue_file(ui)
    if manual_count:
        ui.show_warning(f"AI 自动考试结束，剩余人工考试 {manual_count} 条")
    else:
        ui.show_success("AI 自动考试流程结束")
    ui.pause()


def handle_manual_exam(ui) -> None:
    from core.state import collect_project_state
    from core.app.workflows import run_manual_exam_workflow

    _begin_operation(ui, "人工考试", "正在打开浏览器并等待人工完成考试")
    count = run_async(run_manual_exam_workflow(status_callback=ui.show_info))
    state = collect_project_state()
    if count and state.manual_exam_count == 0:
        ui.show_success(f"人工考试流程结束，共处理 {count} 条")
    elif count:
        ui.show_warning(
            f"人工考试已处理 {count} 条，仍有 {state.manual_exam_count} 条待继续处理"
        )
    else:
        ui.show_warning("本次没有完成新的人工考试链接")
    ui.pause()


def handle_reference_collection(ui) -> None:
    from core.links import extract_urls_from_text
    from core.app.workflows import (
        run_pdf_markdown_workflow,
        run_reference_collection_workflow,
    )
    from core.discovery.pdf_ocr import PdfOcrRuntimeError, PdfOcrUnavailable

    try:
        input_text = ui.prompt_multiline_input(
            _REFERENCE_COLLECTION_PROMPTS,
            title="保存课程课件 / AI导学资料",
            cancel_message="已取消保存课程课件 / AI导学资料",
        )
    except UserCancelRequested as exc:
        ui.show_warning(str(exc) or "已取消保存课程课件 / AI导学资料")
        ui.pause()
        return

    urls = extract_urls_from_text(input_text)
    if not urls:
        ui.show_warning("输入内容中未识别到有效的 HTTP/HTTPS 链接")
        ui.pause()
        return

    try:
        _begin_operation(ui, "保存课程资料", "正在打开浏览器并解析课程资料")
        result = run_async(
            run_reference_collection_workflow(
                urls,
                status_callback=ui.show_info,
            )
        )
    except ValueError as exc:
        ui.show_warning(str(exc))
        ui.pause()
        return
    ocr_result = None
    ocr_status = "未检测到 PDF"
    pdf_count = int(result.get("pdf_count", 0))
    if pdf_count > 0:
        ocr_status = "已跳过"
        try:
            should_convert = ui.prompt_yes_no(
                f"已下载 {pdf_count} 个 PDF，是否使用 PP-OCRv6 转换为 Markdown？",
                default="N",
            )
        except UserCancelRequested:
            should_convert = False
        if should_convert:
            try:
                _begin_operation(
                    ui,
                    "PDF 转 Markdown",
                    "正在加载 PP-StructureV3 / PP-OCRv6 并解析 PDF",
                )
                ocr_result = run_async(
                    run_pdf_markdown_workflow(
                        result["output_dir"],
                        status_callback=ui.show_info,
                    )
                )
                ocr_status = (
                    "部分失败" if ocr_result["ocr_failed_count"] else "已完成"
                )
            except (PdfOcrUnavailable, PdfOcrRuntimeError, OSError) as exc:
                ocr_status = "失败"
                ui.show_warning(str(exc))

    summary_rows = [
        ("输出目录", result["output_dir"]),
        ("课程数量", str(result["course_count"])),
        ("课程详情跳过", str(result.get("course_failed_count", 0))),
        ("章节资源", str(result["section_count"])),
        ("文档保存成功", str(result["document_count"])),
        ("其中 PDF", str(pdf_count)),
        ("文档保存失败", str(result["document_failed_count"])),
        ("视频章节", str(result["video_count"])),
        ("有AI导学内容的视频", str(result["video_with_items"])),
        ("PDF 转 Markdown", ocr_status),
    ]
    if ocr_result is not None:
        summary_rows.extend(
            [
                ("PDF 新转换", str(ocr_result["ocr_converted_count"])),
                ("PDF 已复用", str(ocr_result["ocr_reused_count"])),
                ("PDF 转换失败", str(ocr_result["ocr_failed_count"])),
                ("已删除 PDF 源文件", str(ocr_result["pdf_deleted_count"])),
                ("OCR 输出目录", ocr_result["ocr_output_dir"]),
            ]
        )
    ui.show_summary(
        "课程资料保存结果",
        summary_rows,
    )
    ui.pause()


def handle_show_output_state(exam_urls_file, learning_urls_file, manual_exam_file, ui) -> None:
    from core.config import LEARNING_FAILURES_FILE
    from core.queues.exam import read_exam_urls
    from core.queues.learning import (
        ACCESS_DENIED_LEARNING_FAILURE_REASONS,
        MANUAL_LEARNING_FAILURE_REASONS,
        group_learning_failures_by_reason,
        prune_invalid_learning_failures,
        read_learning_failures,
        read_learning_urls,
        requeue_retryable_learning_failures,
        RETRIABLE_LEARNING_FAILURE_REASONS,
    )
    from core.queues.manual_exam import read_manual_exam_urls

    failures_file = LEARNING_FAILURES_FILE
    pruned = prune_invalid_learning_failures(file_path=failures_file)
    if pruned:
        ui.show_success(
            f"已清理 {len(pruned)} 条无效失败链接（不合规 URL）"
        )

    failures = read_learning_failures(file_path=failures_file)
    rows: list[tuple[str, str]] = [
        ("课程链接", str(len(read_learning_urls(learning_urls_file)))),
        ("挂课失败链接", str(len(failures))),
        ("考试链接", str(len(read_exam_urls(exam_urls_file)))),
        ("人工考试链接", str(len(read_manual_exam_urls(manual_exam_file)))),
    ]
    retriable_n = sum(
        1 for e in failures if e.reason in RETRIABLE_LEARNING_FAILURE_REASONS
    )
    manual_n = sum(
        1 for e in failures if e.reason in MANUAL_LEARNING_FAILURE_REASONS
    )
    denied_n = sum(
        1 for e in failures if e.reason in ACCESS_DENIED_LEARNING_FAILURE_REASONS
    )
    rows.append(("失败·可重试", str(retriable_n)))
    rows.append(("失败·需人工/待复查", str(manual_n)))
    rows.append(("失败·不可访问已清理", str(denied_n)))
    for _reason, count, label in group_learning_failures_by_reason(
        file_path=failures_file
    ):
        rows.append((f"失败·{label}", str(count)))

    ui.show_summary("当前输出文件状态", rows)

    retryable_count = retriable_n
    if retryable_count > 0 and ui.prompt_yes_no(
        f"检测到 {retryable_count} 条可重试挂课失败，是否重新加入课程链接？",
        default="N",
    ):
        requeued = requeue_retryable_learning_failures(
            failures_file=failures_file,
            learning_file=learning_urls_file,
        )
        ui.show_success(
            f"已将 {len(requeued)} 条可重试失败链接重新加入课程链接"
            f"（当前课程链接 {len(read_learning_urls(learning_urls_file))} 条）"
        )

    ui.pause()
