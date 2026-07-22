"""终端能力探测：决定 UI 用 Unicode 花式字符还是纯 ASCII。

背景：传统 Windows cmd / conhost 搭配 CJK 字体时，box-drawing（━─╭╮）、
中点 · 等 East Asian Ambiguous 字符常按全角 2 格渲染，而布局按半角 1 格算，
进度条/表格边框会错位。Windows Terminal、VS Code 终端等宽度处理正常。

策略：
  1. 环境变量强制：COURSE_AFK_UI_CHARS=unicode|ascii（最高优先级）
  2. 识别宿主窗口/终端（WT / VS Code / ConEmu / mintty / PyCharm 等）→ Unicode
  3. 非 Windows → Unicode
  4. 其余（含双击 run.bat 打开的纯 cmd）→ ASCII

识别手段：环境变量（WT_SESSION 等）+ 可选父进程名（WindowsTerminal.exe 等）。
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache


# 用户强制：unicode / ascii / auto（默认 auto）
_ENV_FORCE = "COURSE_AFK_UI_CHARS"

# 父进程名（小写、去 .exe）→ 现代终端
_MODERN_PARENT_STEMS = frozenset(
    {
        "windowsterminal",
        "openconsole",  # Windows Terminal 控制台宿主
        "wt",
        "code",
        "code - insiders",
        "cursor",
        "devenv",  # Visual Studio
        "pycharm64",
        "pycharm",
        "webstorm64",
        "idea64",
        "rider64",
        "conemu64",
        "conemu",
        "cmder",
        "mintty",
        "alacritty",
        "wezterm",
        "wezterm-gui",
        "hyper",
        "tabby",
        "fluentterminal",
        "terminus",
    }
)


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_windows_terminal() -> bool:
    """Windows Terminal（官方会注入 WT_SESSION / WT_PROFILE_ID）。"""
    return bool(os.environ.get("WT_SESSION") or os.environ.get("WT_PROFILE_ID"))


def is_vscode_terminal() -> bool:
    """VS Code / Cursor 集成终端。"""
    term_program = (os.environ.get("TERM_PROGRAM") or "").lower()
    if term_program in {"vscode", "cursor"}:
        return True
    # 部分版本只注入 VSCODE_INJECTION，不设 TERM_PROGRAM
    return bool(os.environ.get("VSCODE_INJECTION"))


def is_conemu_or_cmder() -> bool:
    return bool(
        os.environ.get("ConEmuANSI")
        or os.environ.get("ConEmuPID")
        or os.environ.get("CMDER_ROOT")
    )


def is_mintty() -> bool:
    """Git Bash / MSYS2 mintty。"""
    term_program = (os.environ.get("TERM_PROGRAM") or "").lower()
    if term_program == "mintty":
        return True
    return bool(os.environ.get("MSYSTEM") or os.environ.get("MINGW_PREFIX"))


def is_jetbrains_terminal() -> bool:
    """PyCharm / IDEA 等内置终端常设 TERMINAL_EMULATOR=JetBrains-JediTerm。"""
    emu = (os.environ.get("TERMINAL_EMULATOR") or "").lower()
    return "jetbrains" in emu or "jedi" in emu


def _env_force_unicode() -> bool | None:
    """COURSE_AFK_UI_CHARS=unicode|ascii → 强制；auto/空 → None。"""
    raw = (os.environ.get(_ENV_FORCE) or "").strip().lower()
    if raw in {"unicode", "utf8", "utf-8", "pretty", "1", "true", "yes"}:
        return True
    if raw in {"ascii", "cmd", "legacy", "0", "false", "no"}:
        return False
    return None


@lru_cache(maxsize=1)
def _windows_parent_process_stem() -> str:
    """当前进程父进程可执行文件名（小写）。失败返回空串。"""
    if not is_windows():
        return ""
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
        CreateToolhelp32Snapshot = kernel32.CreateToolhelp32Snapshot
        CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        Process32FirstW = kernel32.Process32FirstW
        Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        Process32FirstW.restype = wintypes.BOOL
        Process32NextW = kernel32.Process32NextW
        Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        Process32NextW.restype = wintypes.BOOL
        CloseHandle = kernel32.CloseHandle

        pid = kernel32.GetCurrentProcessId()
        snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap in (0, INVALID_HANDLE_VALUE, None):
            return ""
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if not Process32FirstW(snap, ctypes.byref(entry)):
                return ""
            parent_pid = None
            while True:
                if entry.th32ProcessID == pid:
                    parent_pid = entry.th32ParentProcessID
                    break
                if not Process32NextW(snap, ctypes.byref(entry)):
                    break
            if not parent_pid:
                return ""
            # 再扫一遍找父进程名
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if not Process32FirstW(snap, ctypes.byref(entry)):
                return ""
            while True:
                if entry.th32ProcessID == parent_pid:
                    name = (entry.szExeFile or "").strip().lower()
                    # 去掉路径只留文件名
                    if "\\" in name:
                        name = name.rsplit("\\", 1)[-1]
                    return name
                if not Process32NextW(snap, ctypes.byref(entry)):
                    break
            return ""
        finally:
            CloseHandle(snap)
    except Exception:
        return ""


def _parent_looks_modern() -> bool:
    stem = _windows_parent_process_stem()
    if not stem:
        return False
    if stem in _MODERN_PARENT_STEMS:
        return True
    # 去 .exe 再比一次
    bare = stem[:-4] if stem.endswith(".exe") else stem
    return bare in _MODERN_PARENT_STEMS or f"{bare}.exe" in _MODERN_PARENT_STEMS


def is_modern_terminal_host() -> bool:
    """是否挂在已知「宽度处理正常」的终端宿主上。"""
    if is_windows_terminal():
        return True
    if is_vscode_terminal():
        return True
    if is_conemu_or_cmder():
        return True
    if is_mintty():
        return True
    if is_jetbrains_terminal():
        return True
    term_program = (os.environ.get("TERM_PROGRAM") or "").lower()
    if term_program in {
        "alacritty",
        "wezterm",
        "hyper",
        "tabby",
        "iterm.app",
        "apple_terminal",
    }:
        return True
    if (os.environ.get("TERMINAL_EMULATOR") or "").strip():
        # 其它 IDE 内置终端多半设此变量
        return True
    if is_windows() and _parent_looks_modern():
        return True
    return False


def is_legacy_windows_console() -> bool:
    """纯 cmd / conhost：未挂在 WT / VS Code / ConEmu 等宿主上。"""
    if not is_windows():
        return False
    if is_modern_terminal_host():
        return False
    # TERM=xterm-* 且带 TERM_PROGRAM 的，前面已覆盖；单独 xterm 在纯 cmd 上很少见
    return True


def prefers_unicode_ui() -> bool:
    """是否可用 box-drawing / 中点等（宽度可信）。"""
    forced = _env_force_unicode()
    if forced is not None:
        return forced
    if not is_windows():
        return True
    return not is_legacy_windows_console()


def detect_terminal_kind() -> str:
    """供日志/调试：终端类型标签。"""
    forced = _env_force_unicode()
    if forced is True:
        return "forced_unicode"
    if forced is False:
        return "forced_ascii"
    if not is_windows():
        return "unix"
    if is_windows_terminal():
        return "windows_terminal"
    if is_vscode_terminal():
        return "vscode"
    if is_conemu_or_cmder():
        return "conemu"
    if is_mintty():
        return "mintty"
    if is_jetbrains_terminal():
        return "jetbrains"
    if _parent_looks_modern():
        parent = _windows_parent_process_stem() or "modern_parent"
        return f"parent:{parent}"
    return "cmd"


# ---------- 全套 UI 字形（进度条 + 日志图标 + 分隔符 + 箭头）----------


class ProgressCharset:
    """进度条字形。"""

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
    """整套 TUI/CLI 装饰字符：图标、分隔、箭头、进度轨、Textual 边框样式名。

    现代终端用 Unicode；纯 cmd 全 ASCII，避免 CJK 字体全角错位。
    """

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
        """日志图标左对齐到固定显示宽度。"""
        return f"{icon:<{self.icon_width}}"

    def join_keys(self, *parts: str) -> str:
        """快捷键提示：A · B · C（Unicode）或 A | B | C（ASCII）。"""
        return self.keys_join.join(p for p in parts if p)


# Windows Terminal / VS Code 等
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
    # 单字符图标（WT 下宽度稳定）；日志用 icon_width=2 对齐
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
    """当前终端应使用的整套 UI 字形。"""
    use_unicode = prefers_unicode_ui() if unicode is None else bool(unicode)
    return _UNICODE_GLYPHS if use_unicode else _ASCII_GLYPHS


def progress_charset(*, unicode: bool | None = None) -> ProgressCharset:
    """返回当前终端应使用的进度条字符集。"""
    return ui_glyphs(unicode=unicode).progress


def clear_terminal_compat_cache() -> None:
    """测试用：清父进程探测缓存。"""
    _windows_parent_process_stem.cache_clear()
