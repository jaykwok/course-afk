from __future__ import annotations

import asyncio
import ctypes
import sys


def _check_python_version() -> None:
    if sys.version_info < (3, 14):
        raise RuntimeError("需要 Python 3.14 或更高版本")


# Keep this launcher-local so it runs before importing config/logging code.
def _disable_windows_console_input_modes_early() -> None:
    if not sys.platform.startswith("win"):
        return

    try:
        kernel32 = ctypes.windll.kernel32
        from ctypes import wintypes
        kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetConsoleMode.restype = wintypes.BOOL
        kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.SetConsoleMode.restype = wintypes.BOOL
        stdin_handle = kernel32.GetStdHandle(-10)
        if stdin_handle in (0, -1, None, ctypes.c_void_p(-1).value):
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


from core.app.menus import MAIN_MENU_OPTIONS

MENU_OPTIONS = MAIN_MENU_OPTIONS

MANUAL_SELECTION_PROMPTS = [
    "请粘贴入口链接、课程链接、考试链接或课程集合页（学习专区 / 案例库）。",
    "如果包含课程集合页，程序会先询问你是全部学习，还是手动选择学习模块。",
    "程序会依次打开入口页面，请你手动点击要处理的课程或考试。",
    "如页面提示需要报名，请先报名，再点击开始学习。",
    "新打开的页面会自动分类写入 课程链接.json 或 考试链接.json。",
]


def main() -> int:
    _check_python_version()
    from core.abort import UserAbortRequested, UserCancelRequested
    from core.config import setup_logging
    from core.config import (
        EXAM_URLS_FILE,
        LEARNING_URLS_FILE,
        MANUAL_EXAM_FILE,
    )

    setup_logging()

    import core.ui as ui
    from core.app.launcher_controller import (
        handle_afk,
        handle_ai_exam,
        handle_manual_selection,
        handle_reference_collection,
        handle_account_management,
        handle_task_management,
    )
    from core.state import collect_project_state

    try:
        first_loop = True
        while True:
            if not first_loop:
                ui.prepare_menu_loading()
            first_loop = False
            state = collect_project_state()
            ui.show_title("中国电信网上大学自动化工具", "登录、学习、考试统一入口")
            ui.render_dashboard(state)
            choice = ui.show_menu(MENU_OPTIONS)

            try:
                if choice == 1:
                    handle_afk(ui)
                elif choice == 2:
                    handle_ai_exam(ui)
                elif choice == 3:
                    handle_manual_selection(MANUAL_SELECTION_PROMPTS, ui)
                elif choice == 4:
                    handle_reference_collection(ui)
                elif choice == 5:
                    handle_task_management(EXAM_URLS_FILE, LEARNING_URLS_FILE, MANUAL_EXAM_FILE, ui)
                elif choice == 6:
                    handle_account_management(state, ui)
                elif choice == 7:
                    ui.show_success("已退出统一入口")
                    return 0
                else:
                    ui.show_error("无效选择，请重试")
            except (UserCancelRequested, asyncio.CancelledError) as exc:
                # 浏览器关闭 / TUI Ctrl+C 等取消：剩余链接已保存，返回主菜单
                ui.show_warning(str(exc) or "已取消当前操作，返回主菜单")
                continue
    except UserAbortRequested as exc:
        ui.show_warning(str(exc))
        return 0
    except KeyboardInterrupt:
        ui.show_warning("已收到 Ctrl+C，程序退出")
        return 0


if __name__ == "__main__":
    _check_python_version()
    # 默认入口走 Textual TUI；main() 仍可作为后台控制流被复用/被测试。
    from core.ui.tui_bridge import launch_tui

    raise SystemExit(launch_tui())
