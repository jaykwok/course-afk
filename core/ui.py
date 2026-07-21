from __future__ import annotations

import math
import os
import re
import sys
import time
from datetime import datetime

from rich.align import Align
from rich.box import ASCII, DOUBLE_EDGE, HEAVY_HEAD, ROUNDED, SIMPLE_HEAVY
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from core.abort import UserCancelRequested
from core.credential import load_credential_metadata
from core.palette import GREEN, GREEN_BRIGHT, ERROR, SUCCESS, WARNING
from core.state import ProjectState, recommend_next_step

# 输出图标约定（全 ASCII，cmd/conhost 宽度稳定；CLI 与 TUI 日志一致）。
# 普通提示用裸 `-`，成功/警告/失败用方括号，统一占 3 格宽以左对齐。
ICON_INFO = "-"  # 普通提示
ICON_SUCCESS = "[+]"  # 成功
ICON_WARNING = "[!]"  # 警告
ICON_FAILURE = "[-]"  # 失败


def _detect_legacy_windows_mode() -> bool | None:
    if sys.platform.startswith("win") and os.environ.get("WT_SESSION"):
        return False
    return None


def _is_legacy_windows_console() -> bool:
    """纯 cmd / conhost（未设置 WT_SESSION）。

    这类控制台搭配 CJK 字体时，box-drawing 字符（╭╮╰╯─│ 等，East Asian Ambiguous）
    按全角 2 格渲染，而 Rich 按半角 1 格计算，导致表格/面板边框在角落错位断裂。
    """
    return sys.platform.startswith("win") and not os.environ.get("WT_SESSION")


def _rich_box(default_box):
    """Rich 表格/面板边框：Windows Terminal 用原花式边框，纯 cmd 退化为 ASCII。"""
    if _is_legacy_windows_console():
        return ASCII
    return default_box


console = Console(emoji=False, legacy_windows=_detect_legacy_windows_mode())


def _read_console_line(prompt: Text | str, *, leading_newline: bool = True) -> str:
    if leading_newline:
        console.print()
    console.print(prompt, end="")
    return input()


def _write_console_raw(text: str) -> None:
    console.file.write(text)
    console.file.flush()


def _echo_input_char(char: str) -> None:
    _write_console_raw(char)


def _erase_input_char(chars: list[str]) -> bool:
    if chars and chars[-1] != "\n":
        chars.pop()
        _write_console_raw("\b \b")
        return True
    return False


def _read_windows_multiline_input(cancel_message: str) -> str:
    import ctypes
    from ctypes import wintypes

    class CharUnion(ctypes.Union):
        _fields_ = [
            ("UnicodeChar", wintypes.WCHAR),
            ("AsciiChar", wintypes.CHAR),
        ]

    class KeyEventRecord(ctypes.Structure):
        _fields_ = [
            ("bKeyDown", wintypes.BOOL),
            ("wRepeatCount", wintypes.WORD),
            ("wVirtualKeyCode", wintypes.WORD),
            ("wVirtualScanCode", wintypes.WORD),
            ("uChar", CharUnion),
            ("dwControlKeyState", wintypes.DWORD),
        ]

    class EventUnion(ctypes.Union):
        _fields_ = [("KeyEvent", KeyEventRecord), ("Padding", ctypes.c_byte * 16)]

    class InputRecord(ctypes.Structure):
        _fields_ = [("EventType", wintypes.WORD), ("Event", EventUnion)]

    kernel32 = ctypes.windll.kernel32
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.GetConsoleMode.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetConsoleMode.restype = wintypes.BOOL
    kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.SetConsoleMode.restype = wintypes.BOOL
    kernel32.ReadConsoleInputW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(InputRecord),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.ReadConsoleInputW.restype = wintypes.BOOL

    stdin_handle = kernel32.GetStdHandle(-10)
    original_mode = wintypes.DWORD()
    invalid_handle = ctypes.c_void_p(-1).value
    if stdin_handle in (None, invalid_handle) or not kernel32.GetConsoleMode(
        stdin_handle, ctypes.byref(original_mode)
    ):
        return sys.stdin.read()

    enable_line_input = 0x0002
    enable_echo_input = 0x0004
    kernel32.SetConsoleMode(
        stdin_handle,
        original_mode.value & ~(enable_line_input | enable_echo_input),
    )

    key_event = 0x0001
    virtual_key_enter = 0x0D
    virtual_key_escape = 0x1B
    virtual_key_backspace = 0x08
    ctrl_pressed = 0x0004 | 0x0008
    chars: list[str] = []
    record = InputRecord()
    records_read = wintypes.DWORD()

    try:
        while True:
            if not kernel32.ReadConsoleInputW(
                stdin_handle,
                ctypes.byref(record),
                1,
                ctypes.byref(records_read),
            ):
                raise OSError("读取控制台输入失败")
            if record.EventType != key_event:
                continue

            event = record.Event.KeyEvent
            if not event.bKeyDown:
                continue
            repeat_count = max(1, int(event.wRepeatCount))
            virtual_key = int(event.wVirtualKeyCode)

            if virtual_key == virtual_key_escape:
                console.print()
                raise UserCancelRequested(cancel_message)
            if virtual_key == virtual_key_enter:
                if event.dwControlKeyState & ctrl_pressed:
                    console.print()
                    return "".join(chars)
                for _ in range(repeat_count):
                    chars.append("\n")
                    console.print()
                    console.print("  ", end="")
                continue
            if virtual_key == virtual_key_backspace:
                for _ in range(repeat_count):
                    _erase_input_char(chars)
                continue

            char = event.uChar.UnicodeChar
            if char == "\x03":
                raise KeyboardInterrupt
            if not char or ord(char) < 32:
                continue
            for _ in range(repeat_count):
                chars.append(char)
                _echo_input_char(char)
    finally:
        kernel32.SetConsoleMode(stdin_handle, original_mode.value)


_SUBMIT_ESCAPE_SEQUENCES = {
    "\x1b\r",
    "\x1b\n",
    "\x1b[13;5u",
    "\x1b[27;5;13~",
}


def _read_escape_sequence() -> str:
    import select

    sequence = "\x1b"
    deadline = time.monotonic() + 0.05
    while time.monotonic() < deadline:
        timeout = max(0, deadline - time.monotonic())
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            break
        char = sys.stdin.read(1)
        if not char:
            break
        sequence += char
        if char in {"\r", "\n", "~", "u"}:
            break
    return sequence


def _is_submit_escape_sequence(sequence: str) -> bool:
    if sequence in _SUBMIT_ESCAPE_SEQUENCES:
        return True
    return bool(re.fullmatch(r"\x1b\[13;5(?::\d+)?u", sequence))


def _read_posix_multiline_input(cancel_message: str) -> str:
    if not sys.stdin.isatty():
        return sys.stdin.read()

    import termios
    import tty

    file_descriptor = sys.stdin.fileno()
    original_attributes = termios.tcgetattr(file_descriptor)
    chars: list[str] = []
    try:
        tty.setraw(file_descriptor)
        sys.stdout.write("\x1b[>1u")
        sys.stdout.flush()
        while True:
            char = sys.stdin.read(1)
            if char == "\x1b":
                sequence = _read_escape_sequence()
                if _is_submit_escape_sequence(sequence):
                    console.print()
                    return "".join(chars)
                if sequence == "\x1b[13u":
                    chars.append("\n")
                    console.print()
                    console.print("  ", end="")
                    continue
                if sequence in {"\x1b", "\x1b[27u"}:
                    console.print()
                    raise UserCancelRequested(cancel_message)
                continue
            if char == "\x03":
                raise KeyboardInterrupt
            if char in {"\r", "\n"}:
                chars.append("\n")
                console.print()
                console.print("  ", end="")
                continue
            if char in {"\x08", "\x7f"}:
                _erase_input_char(chars)
                continue
            if char and ord(char) >= 32:
                chars.append(char)
                _echo_input_char(char)
    finally:
        try:
            sys.stdout.write("\x1b[<u")
            sys.stdout.flush()
        finally:
            termios.tcsetattr(file_descriptor, termios.TCSADRAIN, original_attributes)


def _read_multiline_console_input(cancel_message: str) -> str:
    console.print("  ", end="")
    if sys.platform.startswith("win"):
        return _read_windows_multiline_input(cancel_message)
    return _read_posix_multiline_input(cancel_message)


def _ask_choice_number(prompt: str, *, min_value: int, max_value: int) -> int:
    choices_text = f" [{min_value}-{max_value}]: "
    while True:
        prompt_text = Text("  ")
        prompt_text.append(prompt, style=f"bold {GREEN}")
        prompt_text.append(choices_text, style="dim")
        raw_choice = _read_console_line(prompt_text).strip()
        if raw_choice.isdigit():
            choice = int(raw_choice)
            if min_value <= choice <= max_value:
                return choice
        show_warning(f"请输入 {min_value}-{max_value} 之间的数字")


def show_title(title: str, subtitle: str | None = None) -> None:
    console.print()
    title_text = Text(justify="center")
    title_text.append(title, style=f"bold {GREEN_BRIGHT}")
    if subtitle:
        title_text.append(f"\n{subtitle}", style="dim white")
    console.print(
        Align.center(
            Panel(
                title_text,
                expand=False,
                border_style=GREEN_BRIGHT,
                padding=(1, 6),
                box=_rich_box(DOUBLE_EDGE),
            )
        )
    )
    console.print()


def show_info(message: str) -> None:
    console.print(f"  [{GREEN}]{ICON_INFO:<3}[/]  {escape(message)}")


def show_success(message: str) -> None:
    console.print(f"  [bold {SUCCESS}]{ICON_SUCCESS:<3}[/]  [{SUCCESS}]{escape(message)}[/]")


def show_warning(message: str) -> None:
    console.print(f"  [bold {WARNING}]{ICON_WARNING:<3}[/]  [{WARNING}]{escape(message)}[/]")


def show_error(message: str) -> None:
    console.print(f"  [bold {ERROR}]{ICON_FAILURE:<3}[/]  [bold {ERROR}]{escape(message)}[/]")


def begin_operation(title: str, message: str) -> None:
    """显示持续任务状态；CLI 直接输出当前阶段。"""
    show_title(title)
    show_info(message)


def prepare_menu_loading() -> None:
    """主菜单重新加载前的过渡占位。

    用于填满“结果页确认 → 主菜单挂载”之间的 held-screen 间隙：TUI 下由桥接层
    替换为忙碌状态，慢机器上不再像卡住；CLI 同步顺序输出、无此间隙，故为空操作。
    """
    return None


def _credential_display(state: ProjectState, metadata) -> Text:
    if not state.has_credential:
        return Text(f"{ICON_FAILURE} 不存在", style=f"bold {ERROR}")
    if state.credential_expired:
        return Text(f"{ICON_WARNING} 已过期", style=f"bold {WARNING}")
    if metadata and metadata.expires_at:
        try:
            expires_dt = datetime.fromisoformat(metadata.expires_at)
            now = datetime.now()
            if now.date() >= expires_dt.date():
                return Text(f"{ICON_WARNING} 已过期", style=f"bold {WARNING}")
            seconds_left = (expires_dt - now).total_seconds()
            days_left = max(1, math.ceil(seconds_left / 86400))
            t = Text(f"{ICON_SUCCESS} 有效至 {expires_dt:%Y-%m-%d}", style=f"bold {SUCCESS}")
            t.append(f"  （还有 {days_left} 天）", style="dim")
            return t
        except ValueError:
            pass
    return Text(f"{ICON_SUCCESS} 有效", style=f"bold {SUCCESS}")


def build_dashboard_renderable(state: ProjectState, *, expand: bool = False):
    """构造仪表盘渲染对象（CLI 与 TUI 共用，避免两处重复实现漂移）。

    expand=True 时表格铺满可用宽度（TUI 用，避免宽终端下表格居中留大片空白）；
    expand=False 时保持居中、紧凑（CLI 用）。
    """
    metadata = load_credential_metadata()
    account_label = metadata.account_label if metadata else "未登录"

    recommended = recommend_next_step(
        has_credential=state.has_credential and not state.credential_expired,
        learning_count=state.learning_count,
        exam_count=state.exam_count,
        manual_exam_count=state.manual_exam_count,
    )

    table = Table(
        show_header=False,
        box=_rich_box(ROUNDED),
        border_style=GREEN,
        title=f"[bold {GREEN_BRIGHT}]当前状态[/]",
        title_style=f"bold {GREEN_BRIGHT}",
        min_width=54,
        padding=(0, 1),
        expand=expand,
    )
    table.add_column("项目", style="dim white", min_width=10, justify="right", no_wrap=True)
    table.add_column("值", overflow="fold", min_width=24, ratio=1)

    table.add_row("当前账号", Text(account_label, style="bold white"))
    table.add_row("账号有效期", _credential_display(state, metadata))

    counts = Text()
    for index, (label, count) in enumerate(
        (
            ("课程", state.learning_count),
            ("挂课失败", state.learning_failure_count),
            ("考试", state.exam_count),
            ("人工考试", state.manual_exam_count),
        )
    ):
        if index:
            counts.append("   ", style="dim")
        counts.append(f"{label} ", style="dim white")
        counts.append(str(count), style="bold bright_white" if count else "dim")
    table.add_row("任务数量", counts)

    table.add_row(
        "建议操作",
        Text(f"->  {recommended}", style=f"bold {WARNING}"),
    )
    return table if expand else Align.center(table)


def build_menu_status_renderable(state: ProjectState):
    """构造主菜单内的紧凑状态卡片。

    Textual 主菜单是模态屏，会遮住底层完整仪表盘，因此这里保留用户做菜单
    决策最需要的账号、有效期、任务数量和建议操作，并控制在较小高度内。
    """
    metadata = load_credential_metadata()
    account_label = metadata.account_label if metadata else "未登录"
    recommended = recommend_next_step(
        has_credential=state.has_credential and not state.credential_expired,
        learning_count=state.learning_count,
        exam_count=state.exam_count,
        manual_exam_count=state.manual_exam_count,
    )

    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column(style="dim white", width=10, justify="right", no_wrap=True)
    grid.add_column(ratio=1, overflow="fold")
    grid.add_row("当前账号", Text(account_label, style="bold bright_white"))
    grid.add_row("账号有效期", _credential_display(state, metadata))

    counts = Text()
    for index, (label, count) in enumerate(
        (
            ("课程", state.learning_count),
            ("挂课失败", state.learning_failure_count),
            ("考试", state.exam_count),
            ("人工考试", state.manual_exam_count),
        )
    ):
        if index:
            counts.append("  |  ", style="dim")
        counts.append(f"{label} ", style="dim")
        counts.append(str(count), style="bold bright_white" if count else "dim")
    grid.add_row("任务数量", counts)
    grid.add_row(
        "建议操作",
        Text(f"->  {recommended}", style=f"bold {WARNING}"),
    )

    return Panel(
        grid,
        title=f"[bold {GREEN_BRIGHT}]账号与任务状态[/]",
        border_style=GREEN,
        padding=(0, 1),
        box=_rich_box(ROUNDED),
    )


def render_dashboard(state: ProjectState) -> None:
    console.print(build_dashboard_renderable(state))
    console.print()


def show_menu(options: list[str]) -> int:
    table = Table(
        show_header=False,
        box=_rich_box(HEAVY_HEAD),
        border_style=GREEN,
        title=f"[bold {GREEN_BRIGHT}]主菜单[/]",
        title_style=f"bold {GREEN_BRIGHT}",
        min_width=54,
        padding=(0, 1),
    )
    table.add_column("序号", justify="right", style=f"bold {GREEN}", width=4)
    table.add_column("功能", min_width=44)
    for index, option in enumerate(options, start=1):
        if index == len(options):
            table.add_row(str(index), Text(option, style="dim"))
        else:
            table.add_row(str(index), option)
    console.print(Align.center(table))
    return _ask_choice_number("请选择功能", min_value=1, max_value=len(options))


def prompt_choice(title: str, options: list[str], prompt: str = "请选择") -> int:
    table = Table(
        show_header=False,
        box=_rich_box(ROUNDED),
        border_style=GREEN,
        title=f"[bold {GREEN_BRIGHT}]{title}[/]",
        title_style=f"bold {GREEN_BRIGHT}",
        min_width=54,
        padding=(0, 1),
    )
    table.add_column("序号", justify="right", style=f"bold {GREEN}", width=4)
    table.add_column("选项", min_width=44)
    for index, option in enumerate(options, start=1):
        table.add_row(str(index), option)
    console.print(Align.center(table))
    return _ask_choice_number(prompt, min_value=1, max_value=len(options))


def prompt_yes_no(message: str, default: str = "N") -> bool:
    normalized_default = (default or "N").strip().upper()
    if normalized_default not in {"Y", "N"}:
        normalized_default = "N"
    while True:
        prompt_text = Text("  ")
        prompt_text.append(message, style=f"bold {GREEN}")
        prompt_text.append(f" [Y/N，默认 {normalized_default}]: ", style="dim")
        choice = _read_console_line(prompt_text).strip()
        if not choice:
            choice = normalized_default
        choice = choice.upper()
        if choice in {"Y", "N"}:
            break
        show_warning("请输入 Y 或 N")
    return choice.strip().upper() == "Y"


def prompt_summary_confirmation(
    title: str,
    rows: list[tuple[str, str]],
    message: str = "确认继续处理？",
    default: str = "Y",
) -> bool:
    """显示链接分类汇总，并确认是否继续。"""
    show_summary(title, rows)
    return prompt_yes_no(message, default)


def prompt_multiline_input(
    messages: list[str],
    *,
    title: str = "手动选择课程 / 录入链接",
    cancel_message: str = "已取消手动选择课程 / 录入链接",
) -> str:
    instruction = Text()
    for index, message in enumerate(messages, start=1):
        instruction.append(f"  {index}. ", style=f"bold {GREEN}")
        instruction.append(f"{message}\n", style="white")
    instruction.append(
        "\n  按 Enter 换行，右键 / Ctrl+V 粘贴，输入完成后按 Ctrl+Enter 提交",
        style=f"bold {WARNING}",
    )
    instruction.append("\n  输入过程中可按 ESC 取消并返回主菜单", style=f"bold {WARNING}")
    console.print(
        Align.center(
            Panel(
                instruction,
                title=f"[bold white]{title}[/bold white]",
                border_style=GREEN,
                box=_rich_box(ROUNDED),
                width=76,
                padding=(1, 2),
            )
        )
    )
    return _read_multiline_console_input(cancel_message)


def pause(message: str = "按回车返回主菜单") -> None:
    console.print()
    console.print(Rule(style="bright_black"))
    prompt_text = Text("  ")
    prompt_text.append(message, style="dim")
    _read_console_line(prompt_text, leading_newline=False)
    console.print()


def build_summary_renderable(title: str, rows: list[tuple[str, str]], *, expand: bool = False):
    """构造汇总渲染对象（CLI 与 TUI 共用）。expand=True 时铺满宽度（TUI 用）。"""
    table = Table(
        show_header=False,
        box=_rich_box(SIMPLE_HEAVY),
        border_style=GREEN,
        title=f"[bold {GREEN_BRIGHT}]{title}[/]",
        title_style=f"bold {GREEN_BRIGHT}",
        min_width=54,
        padding=(0, 1),
        expand=expand,
    )
    table.add_column("项目", style="dim white", min_width=16, justify="right", no_wrap=True)
    table.add_column("结果", overflow="fold", min_width=34, ratio=1)
    for left, right in rows:
        table.add_row(left, Text(right, style="bold white"))
    return table if expand else Align.center(table)


def show_summary(title: str, rows: list[tuple[str, str]]) -> None:
    console.print(build_summary_renderable(title, rows))


def pause_with_summary(
    title: str,
    rows: list[tuple[str, str]],
    message: str = "查看完成后返回主菜单",
) -> None:
    """显示处理结果汇总，等待单次确认后返回。"""
    handle = prepare_pause_with_summary(title, rows, message)
    wait_prepared_prompt(handle)


def prepare_pause_with_summary(
    title: str,
    rows: list[tuple[str, str]],
    message: str = "查看完成后返回主菜单",
):
    """先渲染结果页，返回稍后用于等待确认的句柄。"""
    show_summary(title, rows)
    return message


def wait_prepared_prompt(handle) -> None:
    """等待已渲染结果页的最终确认。"""
    pause(str(handle))


async def wait_with_progress(
    duration: int,
    description: str = "处理中",
) -> None:
    import asyncio

    duration = int(duration)
    if duration <= 0:
        return
    with Progress(
        SpinnerColumn(spinner_name="dots"),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=28),
        TextColumn(f"[{GREEN}]{{task.completed}}[/][dim]/{{task.total}}s[/dim]"),
        TextColumn("[dim]([/dim][bold]{task.percentage:>3.0f}%[/bold][dim])[/dim]"),
        TimeRemainingColumn(),
        console=console,
        auto_refresh=True,
        refresh_per_second=10,
        transient=True,
    ) as progress:
        task = progress.add_task(description, total=duration)
        for _ in range(duration):
            await asyncio.sleep(1)
            progress.update(task, advance=1)
