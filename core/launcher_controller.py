from __future__ import annotations

from core.abort import UserCancelRequested
from core.config import run_async


_AI_EXAM_URL_INPUT_PROMPTS = [
    "请粘贴考试链接或包含考试链接的文本，一行一个链接。",
    "识别到的链接会去重写入 考试链接.json，并立即开始 AI 自动考试。",
]


def _prompt_ai_exam_auto_submit(ui) -> bool:
    return ui.prompt_yes_no("AI考试是否自动交卷？", default="N")


def _ensure_exam_urls_for_ai_exam(ui) -> bool:
    from core.config import EXAM_URLS_FILE
    from core.exam_queue import append_exam_url, read_exam_urls
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
    from core.learning_queue import read_learning_urls

    if not LEARNING_URLS_FILE.exists():
        return
    if read_learning_urls(LEARNING_URLS_FILE):
        return

    del_file(LEARNING_URLS_FILE)
    ui.show_success("已删除空的课程链接.json")


def _maybe_delete_empty_exam_queue_file(ui) -> None:
    from core.config import EXAM_URLS_FILE
    from core.exam_queue import read_exam_urls
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
        "检测到学习专区链接，请选择处理方式",
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
    "manual-exam-pending": "AI 考试完成，仍有人工考试待处理",
    "done": "全部流程完成",
}


def handle_recommended_flow(ui) -> None:
    from core.exam_answers import ExamAiConfigurationError
    from core.workflows import run_recommended_flow

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


def handle_refresh_credential(state, ui) -> None:
    from core.login import LoginNotCompletedError
    from core.workflows import refresh_credential

    if state.has_credential and not state.credential_expired:
        ui.show_warning("当前登录凭证仍有效，继续将覆盖现有登录状态")
    try:
        profile = refresh_credential(status_callback=ui.show_info)
    except LoginNotCompletedError as exc:
        ui.show_warning(str(exc))
        ui.pause()
        return
    ui.show_success(f"登录凭证已更新，当前账号：{profile.label}")
    ui.pause()


def handle_show_learning_links(learning_urls_file, ui) -> None:
    from core.learning_queue import read_learning_urls

    links = read_learning_urls(learning_urls_file)
    if not links:
        ui.show_warning("课程链接.json 当前为空")
    else:
        ui.show_summary(
            "课程链接状态",
            [("课程链接总数", str(len(links))), ("首条课程链接", links[0])],
        )
    ui.pause()


def handle_manual_selection(prompts, ui) -> None:
    from core.links import split_manual_selection_urls
    from core.workflows import parse_manual_selection_input, run_manual_course_selection

    try:
        input_text = ui.prompt_multiline_input(prompts)
    except UserCancelRequested as exc:
        ui.show_warning(str(exc) or "已取消手动选择课程 / 录入链接")
        ui.pause()
        return
    _, _, learning_zone_urls, _ = split_manual_selection_urls(
        parse_manual_selection_input(input_text)
    )
    learning_zone_mode = choose_learning_zone_mode(
        learning_zone_urls,
        prompt_choice_func=ui.prompt_choice,
    )
    result = run_async(
        run_manual_course_selection(
            input_text,
            learning_zone_mode=learning_zone_mode,
            status_callback=ui.show_info,
        )
    )
    ui.show_summary(
        "手动选择课程 / 录入链接结果",
        [
            ("识别到的输入链接", str(result["input_url_count"])),
            ("直接写入的学习链接", str(result["direct_learning_count"])),
            ("直接写入的考试链接", str(result["direct_exam_count"])),
            ("学习专区链接数量", str(result["learning_zone_url_count"])),
            ("学习专区自动解析数量", str(result["learning_zone_parsed_count"])),
            ("需要手动打开的入口链接", str(result["entry_url_count"])),
            ("手动点击记录的学习链接", str(result["manual_record_count"])),
            ("手动点击记录的考试链接", str(result["manual_exam_record_count"])),
            ("当前学习链接总数", str(result["learning_total"])),
            ("当前考试链接总数", str(result["exam_total"])),
        ],
    )
    ui.pause()


def handle_afk(ui) -> None:
    from core.workflows import run_afk_workflow

    has_exam = run_async(run_afk_workflow(status_callback=ui.show_info))
    _maybe_delete_empty_learning_queue_file(ui)
    if has_exam:
        ui.show_success("挂课完成，并检测到考试链接")
    else:
        ui.show_warning("挂课完成，未检测到考试链接")
    ui.pause()


def handle_ai_exam(ui) -> None:
    from core.exam_answers import ExamAiConfigurationError
    from core.workflows import run_ai_exam_workflow

    if not _ensure_exam_urls_for_ai_exam(ui):
        ui.pause()
        return

    auto_submit = _prompt_ai_exam_auto_submit(ui)
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
    from core.workflows import run_manual_exam_workflow

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


def handle_show_output_state(exam_urls_file, learning_urls_file, manual_exam_file, ui) -> None:
    from core.exam_queue import read_exam_urls
    from core.learning_queue import read_learning_failures, read_learning_urls
    from core.manual_exam_queue import read_manual_exam_urls

    ui.show_summary(
        "当前输出文件状态",
        [
            ("课程链接", str(len(read_learning_urls(learning_urls_file)))),
            ("挂课失败链接", str(len(read_learning_failures()))),
            ("考试链接", str(len(read_exam_urls(exam_urls_file)))),
            ("人工考试链接", str(len(read_manual_exam_urls(manual_exam_file)))),
        ],
    )
    ui.pause()
