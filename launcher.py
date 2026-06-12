from __future__ import annotations

import ctypes
import sys


# Keep this launcher-local so it runs before importing config/logging code.
def _disable_windows_console_input_modes_early() -> None:
    if not sys.platform.startswith("win"):
        return

    try:
        kernel32 = ctypes.windll.kernel32
        stdin_handle = kernel32.GetStdHandle(-10)
        if stdin_handle in (0, -1):
            return

        mode = ctypes.c_uint()
        if not kernel32.GetConsoleMode(stdin_handle, ctypes.byref(mode)):
            return

        extended_flags = 0x0080
        quick_edit_mode = 0x0040
        insert_mode = 0x0020
        updated_mode = (mode.value | extended_flags) & ~(quick_edit_mode | insert_mode)
        if updated_mode != mode.value:
            kernel32.SetConsoleMode(stdin_handle, updated_mode)
    except Exception:
        pass


_disable_windows_console_input_modes_early()


MENU_OPTIONS = [
    "推荐流程 / 继续上次进度（挂课+考试）",
    "仅挂课",
    "切换账号 / 更新登录凭证",
    "手动选择课程 / 录入课程或考试链接",
    "AI 自动考试",
    "人工考试",
    "查看输出文件统计",
    "查看课程链接详情",
    "退出",
]

MANUAL_SELECTION_PROMPTS = [
    "请粘贴入口链接、课程链接、考试链接或学习专区链接。",
    "如果包含学习专区链接，程序会先询问你是全部学习，还是手动选择学习模块。",
    "程序会依次打开入口页面，请你手动点击要处理的课程或考试。",
    "如页面提示需要报名，请先报名，再点击开始学习。",
    "新打开的页面会自动分类写入 课程链接.json 或 考试链接.json。",
]


def main() -> int:
    from core.abort import UserAbortRequested
    from core.config import setup_logging
    from core.config import (
        EXAM_URLS_FILE,
        LEARNING_URLS_FILE,
        MANUAL_EXAM_FILE,
    )

    setup_logging()

    import core.ui as ui
    from core.launcher_controller import (
        handle_afk,
        handle_ai_exam,
        handle_manual_exam,
        handle_manual_selection,
        handle_recommended_flow,
        handle_refresh_credential,
        handle_show_learning_links,
        handle_show_output_state,
    )
    from core.state import collect_project_state

    try:
        while True:
            state = collect_project_state()
            ui.show_title("课程自动化工具", "登录、学习、考试统一入口")
            ui.render_dashboard(state)
            choice = ui.show_menu(MENU_OPTIONS)

            if choice == 1:
                handle_recommended_flow(ui)
            elif choice == 2:
                handle_afk(ui)
            elif choice == 3:
                handle_refresh_credential(state, ui)
            elif choice == 4:
                handle_manual_selection(MANUAL_SELECTION_PROMPTS, ui)
            elif choice == 5:
                handle_ai_exam(ui)
            elif choice == 6:
                handle_manual_exam(ui)
            elif choice == 7:
                handle_show_output_state(
                    EXAM_URLS_FILE,
                    LEARNING_URLS_FILE,
                    MANUAL_EXAM_FILE,
                    ui,
                )
            elif choice == 8:
                handle_show_learning_links(LEARNING_URLS_FILE, ui)
            elif choice == 9:
                ui.show_success("已退出统一入口")
                return 0
            else:
                ui.show_error("无效选择，请重试")
    except UserAbortRequested as exc:
        ui.show_warning(str(exc))
        return 0
    except KeyboardInterrupt:
        ui.show_warning("已收到 Ctrl+C，程序退出")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
