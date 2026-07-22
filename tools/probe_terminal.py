"""本机探测：终端识别结果（供 run.bat / cmd 启动对比）。"""
from __future__ import annotations

import json
import os
import sys


def main() -> int:
    info: dict = {
        "cwd": os.getcwd(),
        "WT_SESSION": os.environ.get("WT_SESSION"),
        "WT_PROFILE_ID": os.environ.get("WT_PROFILE_ID"),
        "TERM": os.environ.get("TERM"),
        "TERM_PROGRAM": os.environ.get("TERM_PROGRAM"),
    }
    # 保证从项目根可 import
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)

    from core.ui.terminal_compat import (
        _windows_ancestor_stems,
        _windows_process_table,
        clear_terminal_compat_cache,
        detect_terminal_kind,
        is_legacy_windows_console,
        prefers_unicode_ui,
        ui_glyphs,
    )

    clear_terminal_compat_cache()
    info["ancestors"] = list(_windows_ancestor_stems())
    info["kind"] = detect_terminal_kind()
    info["unicode"] = prefers_unicode_ui()
    info["glyphs"] = ui_glyphs().name
    info["legacy"] = is_legacy_windows_console()

    try:
        import ctypes
        from ctypes import wintypes

        from core.ui.terminal_compat import _console_window_class

        k = ctypes.windll.kernel32
        u = ctypes.windll.user32
        hwnd = k.GetConsoleWindow()
        pid = wintypes.DWORD(0)
        if hwnd:
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        clear_terminal_compat_cache()
        table = _windows_process_table()
        owner_pid = int(pid.value) if pid.value else 0
        info["console_hwnd"] = int(hwnd) if hwnd else 0
        info["console_class"] = _console_window_class()
        info["console_owner_pid"] = owner_pid
        info["console_owner"] = table.get(owner_pid, (None, None))[1] if owner_pid else None
        info["self_pid"] = int(k.GetCurrentProcessId())
        self_info = table.get(info["self_pid"])
        info["self_parent"] = self_info[0] if self_info else None
        # 控制台进程列表
        max_count = 64
        arr = (wintypes.DWORD * max_count)()
        n = k.GetConsoleProcessList(arr, max_count)
        console_pids = [int(arr[i]) for i in range(min(int(n or 0), max_count))]
        info["console_process_list"] = [
            {"pid": p, "name": table.get(p, (None, "?"))[1]} for p in console_pids
        ]
    except Exception as exc:  # noqa: BLE001
        info["console_err"] = repr(exc)

    # 注册表默认终端
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Console\%%Startup")
        try:
            info["reg_DelegationConsole"] = winreg.QueryValueEx(key, "DelegationConsole")[0]
            info["reg_DelegationTerminal"] = winreg.QueryValueEx(key, "DelegationTerminal")[0]
        finally:
            winreg.CloseKey(key)
    except OSError as exc:
        info["reg_err"] = repr(exc)

    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
