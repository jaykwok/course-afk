"""在 CREATE_NEW_CONSOLE 新会话里跑探测，模拟双击 bat。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

CREATE_NEW_CONSOLE = 0x00000010


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    py = root / ".venv" / "Scripts" / "python.exe"
    out = root / "tools" / "_probe_newcon_result.json"
    if out.exists():
        out.unlink()

    # 内联探测，减少路径/import 问题
    code = r"""
import json, os, sys, ctypes
from ctypes import wintypes
from pathlib import Path
root = Path(r""" + repr(str(root)) + r""")
sys.path.insert(0, str(root))
os.chdir(root)

info = {
    "cwd": os.getcwd(),
    "WT_SESSION": os.environ.get("WT_SESSION"),
    "WT_PROFILE_ID": os.environ.get("WT_PROFILE_ID"),
    "TERM": os.environ.get("TERM"),
}

k = ctypes.windll.kernel32
u = ctypes.windll.user32
hwnd = k.GetConsoleWindow()
buf = ctypes.create_unicode_buffer(256)
cls = ""
if hwnd:
    u.GetClassNameW(hwnd, buf, 256)
    cls = buf.value
pid = wintypes.DWORD(0)
if hwnd:
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
info["console_hwnd"] = int(hwnd or 0)
info["console_class"] = cls
info["console_owner_pid"] = int(pid.value)

from core.ui.terminal_compat import (
    clear_terminal_compat_cache, detect_terminal_kind, prefers_unicode_ui,
    ui_glyphs, _windows_ancestor_stems, is_legacy_windows_console,
    _windows_process_table,
)
clear_terminal_compat_cache()
info["ancestors"] = list(_windows_ancestor_stems())
info["kind"] = detect_terminal_kind()
info["unicode"] = prefers_unicode_ui()
info["glyphs"] = ui_glyphs().name
info["legacy"] = is_legacy_windows_console()
table = _windows_process_table()
info["console_owner"] = table.get(int(pid.value), (None, None))[1] if pid.value else None
info["self_pid"] = int(k.GetCurrentProcessId())

# Enum top-level for Cascadia / Console related to our pid or title
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
found = []
self_pid = info["self_pid"]
def cb(h, lparam):
    b = ctypes.create_unicode_buffer(256)
    u.GetClassNameW(h, b, 256)
    c = b.value
    if "Console" in c or "CASCADIA" in c.upper() or "Pseudo" in c:
        p = wintypes.DWORD(0)
        u.GetWindowThreadProcessId(h, ctypes.byref(p))
        t = ctypes.create_unicode_buffer(256)
        u.GetWindowTextW(h, t, 256)
        found.append({"hwnd": int(h), "class": c, "pid": int(p.value), "title": t.value[:100]})
    return True
u.EnumWindows(WNDENUMPROC(cb), 0)
info["enum_console_windows"] = found[:30]

out = Path(r""" + repr(str(out)) + r""")
out.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
print("wrote", out)
"""
    env = os.environ.copy()
    env.pop("WT_SESSION", None)
    env.pop("WT_PROFILE_ID", None)
    env.pop("TERM", None)

    p = subprocess.run(
        [str(py), "-c", code],
        env=env,
        creationflags=CREATE_NEW_CONSOLE,
        timeout=25,
        cwd=str(root),
    )
    print("returncode", p.returncode)
    if out.exists():
        print(out.read_text(encoding="utf-8"))
    else:
        print("missing", out)
    return p.returncode


if __name__ == "__main__":
    raise SystemExit(main())
