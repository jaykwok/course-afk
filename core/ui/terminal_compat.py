"""终端能力探测：Unicode 花式 UI vs 纯 ASCII。

产品入口只有两种：``run.bat`` 或 ``python launcher.py``。
要区分的是底下挂的控制台宿主，不是启动方式本身。

判定优先级（由高到低）：
  1. ``COURSE_AFK_UI_CHARS=unicode|ascii`` 强制
  2. 控制台窗口类（本机 Win11 实测主信号）
       - PseudoConsoleWindow → WT / ConPTY 托管 → Unicode
       - ConsoleWindowClass  → 经典 conhost → 仍可看 3/4，默认偏 ASCII
  3. ``WT_SESSION`` / ``WT_PROFILE_ID``（在 WT 标签内直接跑 launcher）
  4. 开发兜底：VS Code / Cursor 集成终端
  5. 祖先链仅认 windowsterminal / openconsole（窗口类拿不到时）
  6. 非 Windows → Unicode；其余 → ASCII

说明：不要用「父进程是 cmd」判断 legacy——bat 下面父进程几乎总是 cmd，
Win11 默认终端仍可能是 WT（PseudoConsoleWindow）。
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache


_ENV_FORCE = "COURSE_AFK_UI_CHARS"

# 仅 WT 相关宿主；入口场景不枚举 mintty/IDE 全家桶
_WT_HOST_STEMS = frozenset({"windowsterminal", "openconsole", "wt"})
_ANCESTOR_WALK_DEPTH = 8

_MODERN_CONSOLE_CLASSES = frozenset(
    {
        "pseudoconsolewindow",  # ConPTY / Windows Terminal
        "cascadia_hosting_window_class",
    }
)
_LEGACY_CONSOLE_CLASSES = frozenset({"consolewindowclass"})


def is_windows() -> bool:
    return sys.platform.startswith("win")


def _env_force_unicode() -> bool | None:
    raw = (os.environ.get(_ENV_FORCE) or "").strip().lower()
    if raw in {"unicode", "utf8", "utf-8", "pretty", "1", "true", "yes"}:
        return True
    if raw in {"ascii", "cmd", "legacy", "0", "false", "no"}:
        return False
    return None


def is_windows_terminal_env() -> bool:
    """WT 标签内常注入；双击 bat 经默认终端托管时往往没有。"""
    return bool(os.environ.get("WT_SESSION") or os.environ.get("WT_PROFILE_ID"))


def is_vscode_terminal() -> bool:
    """开发时在 VS Code / Cursor 里跑 launcher。"""
    term_program = (os.environ.get("TERM_PROGRAM") or "").lower()
    if term_program in {"vscode", "cursor"}:
        return True
    return bool(os.environ.get("VSCODE_INJECTION"))


def _normalize_exe_stem(name: str) -> str:
    name = (name or "").strip().lower()
    if "\\" in name:
        name = name.rsplit("\\", 1)[-1]
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    if name.endswith(".exe"):
        name = name[:-4]
    return name


@lru_cache(maxsize=1)
def _windows_process_table() -> dict[int, tuple[int, str]]:
    """pid → (parent_pid, exe_stem)。"""
    if not is_windows():
        return {}
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32 = ctypes.windll.kernel32
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap in (0, INVALID_HANDLE_VALUE, None):
            return {}
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if not kernel32.Process32FirstW(snap, ctypes.byref(entry)):
                return {}
            table: dict[int, tuple[int, str]] = {}
            while True:
                stem = _normalize_exe_stem(entry.szExeFile or "")
                table[int(entry.th32ProcessID)] = (
                    int(entry.th32ParentProcessID),
                    stem,
                )
                if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                    break
            return table
        finally:
            kernel32.CloseHandle(snap)
    except Exception:
        return {}


@lru_cache(maxsize=1)
def _windows_ancestor_stems(max_depth: int = _ANCESTOR_WALK_DEPTH) -> tuple[str, ...]:
    """直接父进程起向上的 exe stem（仅作 WT 兜底，不靠「父=cmd」判 legacy）。"""
    if not is_windows():
        return ()
    try:
        import ctypes

        table = _windows_process_table()
        if not table:
            return ()
        pid = int(ctypes.windll.kernel32.GetCurrentProcessId())
        stems: list[str] = []
        seen: set[int] = set()
        for _ in range(max(1, int(max_depth))):
            if pid in seen:
                break
            seen.add(pid)
            info = table.get(pid)
            if not info:
                break
            parent_pid, _self = info
            parent = table.get(parent_pid)
            if not parent:
                break
            _ppid, parent_stem = parent
            if parent_stem:
                stems.append(parent_stem)
            pid = parent_pid
        return tuple(stems)
    except Exception:
        return ()


def _ancestor_wt_host() -> str | None:
    for stem in _windows_ancestor_stems():
        bare = _normalize_exe_stem(stem)
        if bare in _WT_HOST_STEMS:
            return bare
    return None


@lru_cache(maxsize=1)
def _console_window_class() -> str:
    """附属控制台窗口类名；无控制台返回空。"""
    if not is_windows():
        return ""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        hwnd = kernel32.GetConsoleWindow()
        if not hwnd:
            return ""
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        return (buf.value or "").strip()
    except Exception:
        return ""


def _console_class_is_modern() -> bool:
    cls = _console_window_class().lower()
    return bool(cls) and cls in _MODERN_CONSOLE_CLASSES


def _console_class_is_legacy_conhost() -> bool:
    cls = _console_window_class().lower()
    return bool(cls) and cls in _LEGACY_CONSOLE_CLASSES


def is_modern_terminal_host() -> bool:
    """宽度可信的现代宿主（WT / ConPTY / 开发终端）。"""
    # 1) 窗口类：双击 bat + Win11 默认 WT 的主路径
    if is_windows() and _console_class_is_modern():
        return True
    # 2) WT 环境变量：在 WT 标签内跑 python launcher
    if is_windows_terminal_env():
        return True
    # 3) 开发：VS Code / Cursor
    if is_vscode_terminal():
        return True
    # 4) 窗口类缺失时的 WT 祖先兜底
    if is_windows() and _ancestor_wt_host():
        return True
    return False


def is_legacy_windows_console() -> bool:
    """经典 conhost / 无现代信号。"""
    if not is_windows():
        return False
    return not is_modern_terminal_host()


def prefers_unicode_ui() -> bool:
    forced = _env_force_unicode()
    if forced is not None:
        return forced
    if not is_windows():
        return True
    return not is_legacy_windows_console()


def detect_terminal_kind() -> str:
    """调试标签。"""
    forced = _env_force_unicode()
    if forced is True:
        return "forced_unicode"
    if forced is False:
        return "forced_ascii"
    if not is_windows():
        return "unix"
    if _console_class_is_modern():
        return f"console_class:{_console_window_class()}"
    if is_windows_terminal_env():
        return "windows_terminal"
    if is_vscode_terminal():
        return "vscode"
    host = _ancestor_wt_host()
    if host:
        return f"ancestor:{host}"
    if _console_class_is_legacy_conhost():
        return "conhost"
    return "cmd"


# ---------- 字形 ----------


class ProgressCharset:
    __slots__ = (
        "name",
        "spinner",
        "sep",
        "track_done",
        "track_todo",
        "bracket_l",
        "bracket_r",
        "tail_run",
        "tail_done",
    )

    def __init__(
        self,
        *,
        name: str,
        spinner: tuple[str, ...],
        sep: str,
        track_done: str,
        track_todo: str,
        bracket_l: str,
        bracket_r: str,
        tail_run: str,
        tail_done: str,
    ):
        self.name = name
        self.spinner = spinner
        self.sep = sep
        self.track_done = track_done
        self.track_todo = track_todo
        self.bracket_l = bracket_l
        self.bracket_r = bracket_r
        self.tail_run = tail_run
        self.tail_done = tail_done


class UiGlyphs:
    __slots__ = (
        "name",
        "icon_info",
        "icon_success",
        "icon_warning",
        "icon_failure",
        "icon_width",
        "sep",
        "sep_tight",
        "arrow",
        "bullet",
        "nav_up_down",
        "keys_join",
        "progress",
        "textual_border",
        "textual_border_soft",
    )

    def __init__(
        self,
        *,
        name: str,
        icon_info: str,
        icon_success: str,
        icon_warning: str,
        icon_failure: str,
        icon_width: int,
        sep: str,
        sep_tight: str,
        arrow: str,
        bullet: str,
        nav_up_down: str,
        keys_join: str,
        progress: ProgressCharset,
        textual_border: str,
        textual_border_soft: str,
    ):
        self.name = name
        self.icon_info = icon_info
        self.icon_success = icon_success
        self.icon_warning = icon_warning
        self.icon_failure = icon_failure
        self.icon_width = icon_width
        self.sep = sep
        self.sep_tight = sep_tight
        self.arrow = arrow
        self.bullet = bullet
        self.nav_up_down = nav_up_down
        self.keys_join = keys_join
        self.progress = progress
        self.textual_border = textual_border
        self.textual_border_soft = textual_border_soft

    def pad_icon(self, icon: str) -> str:
        return f"{icon:<{self.icon_width}}"

    def join_keys(self, *parts: str) -> str:
        return self.keys_join.join(p for p in parts if p)


_UNICODE_PROGRESS = ProgressCharset(
    name="unicode",
    spinner=("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"),
    sep="  ·  ",
    track_done="━",
    track_todo="─",
    bracket_l="",
    bracket_r="",
    tail_run=" ·",
    tail_done=" ✓",
)

_ASCII_PROGRESS = ProgressCharset(
    name="ascii",
    spinner=("|", "/", "-", "\\"),
    sep="  |  ",
    track_done="#",
    track_todo="-",
    bracket_l="[",
    bracket_r="]",
    tail_run=" ...",
    tail_done=" [+]",
)

_UNICODE_GLYPHS = UiGlyphs(
    name="unicode",
    icon_info="·",
    icon_success="✓",
    icon_warning="!",
    icon_failure="✗",
    icon_width=2,
    sep="  ·  ",
    sep_tight=" · ",
    arrow="→",
    bullet="•",
    nav_up_down="↑↓",
    keys_join="  ·  ",
    progress=_UNICODE_PROGRESS,
    textual_border="round",
    textual_border_soft="round",
)

_ASCII_GLYPHS = UiGlyphs(
    name="ascii",
    icon_info="-",
    icon_success="[+]",
    icon_warning="[!]",
    icon_failure="[-]",
    icon_width=3,
    sep="  |  ",
    sep_tight=" | ",
    arrow="->",
    bullet="*",
    nav_up_down="方向键",
    keys_join="  |  ",
    progress=_ASCII_PROGRESS,
    textual_border="ascii",
    textual_border_soft="solid",
)


def ui_glyphs(*, unicode: bool | None = None) -> UiGlyphs:
    use_unicode = prefers_unicode_ui() if unicode is None else bool(unicode)
    return _UNICODE_GLYPHS if use_unicode else _ASCII_GLYPHS


def progress_charset(*, unicode: bool | None = None) -> ProgressCharset:
    return ui_glyphs(unicode=unicode).progress


def clear_terminal_compat_cache() -> None:
    _windows_process_table.cache_clear()
    _windows_ancestor_stems.cache_clear()
    _console_window_class.cache_clear()
