from __future__ import annotations

import math
import os
import re
import sys
import time
from datetime import datetime

from rich.align import Align
from rich.box import DOUBLE_EDGE, HEAVY_HEAD, ROUNDED, SIMPLE_HEAVY
from rich.console import Console
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
from core.state import ProjectState, recommend_next_step


def _detect_legacy_windows_mode() -> bool | None:
    if sys.platform.startswith("win") and os.environ.get("WT_SESSION"):
        return False
    return None


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
        prompt_text.append(prompt, style="bold cyan")
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
    title_text.append(title, style="bold bright_cyan")
    if subtitle:
        title_text.append(f"\n{subtitle}", style="dim white")
    console.print(
        Align.center(
            Panel(
                title_text,
                expand=False,
                border_style="bright_cyan",
                padding=(1, 6),
                box=DOUBLE_EDGE,
            )
        )
    )
    console.print()


def show_info(message: str) -> None:
    console.print(f"  [cyan]·[/cyan]  {message}")


def show_success(message: str) -> None:
    console.print(f"  [bold green]√[/bold green]  [green]{message}[/green]")


def show_warning(message: str) -> None:
    console.print(f"  [bold yellow]![/bold yellow]  [yellow]{message}[/yellow]")


def show_error(message: str) -> None:
    console.print(f"  [bold red]×[/bold red]  [bold red]{message}[/bold red]")


def _credential_display(state: ProjectState, metadata) -> Text:
    if not state.has_credential:
        return Text("×  不存在", style="bold red")
    if state.credential_expired:
        return Text("!  已过期", style="bold yellow")
    if metadata and metadata.expires_at:
        try:
            expires_dt = datetime.fromisoformat(metadata.expires_at)
            now = datetime.now()
            if now.date() >= expires_dt.date():
                return Text("!  已过期", style="bold yellow")
            seconds_left = (expires_dt - now).total_seconds()
            days_left = max(1, math.ceil(seconds_left / 86400))
            t = Text("√  有效", style="bold green")
            t.append(f"  （还有 {days_left} 天）", style="dim")
            return t
        except ValueError:
            pass
    return Text("√  有效", style="bold green")


def _count_display(count: int) -> Text:
    if count == 0:
        return Text("0", style="dim")
    return Text(str(count), style="bold bright_white")


def build_dashboard_renderable(state: ProjectState):
    """构造仪表盘渲染对象（CLI 与 TUI 共用，避免两处重复实现漂移）。"""
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
        box=ROUNDED,
        border_style="bright_black",
        title="[bold white]当前状态[/bold white]",
        title_style="bold white",
        min_width=54,
        padding=(0, 1),
    )
    table.add_column("项目", style="dim white", min_width=10, justify="right")
    table.add_column("值", overflow="fold", min_width=40)

    table.add_row("账号", Text(account_label, style="bold white"))
    table.add_row("凭证", _credential_display(state, metadata))
    table.add_row("课程链接", _count_display(state.learning_count))
    table.add_row("挂课失败", _count_display(state.learning_failure_count))
    table.add_row("考试链接", _count_display(state.exam_count))
    table.add_row("人工考试", _count_display(state.manual_exam_count))
    table.add_row(
        "建议操作",
        Text(f"->  {recommended}", style="bold bright_yellow"),
    )
    return Align.center(table)


def render_dashboard(state: ProjectState) -> None:
    console.print(build_dashboard_renderable(state))
    console.print()


def show_menu(options: list[str]) -> int:
    table = Table(
        show_header=False,
        box=HEAVY_HEAD,
        border_style="bright_black",
        title="[bold white]主菜单[/bold white]",
        title_style="bold white",
        min_width=54,
        padding=(0, 1),
    )
    table.add_column("序号", justify="right", style="bold cyan", width=4)
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
        box=ROUNDED,
        border_style="bright_black",
        title=f"[bold white]{title}[/bold white]",
        title_style="bold white",
        min_width=54,
        padding=(0, 1),
    )
    table.add_column("序号", justify="right", style="bold cyan", width=4)
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
        prompt_text.append(message, style="bold cyan")
        prompt_text.append(f" [Y/N，默认 {normalized_default}]: ", style="dim")
        choice = _read_console_line(prompt_text).strip()
        if not choice:
            choice = normalized_default
        choice = choice.upper()
        if choice in {"Y", "N"}:
            break
        show_warning("请输入 Y 或 N")
    return choice.strip().upper() == "Y"


def prompt_multiline_input(
    messages: list[str],
    *,
    title: str = "手动选择课程 / 录入链接",
    cancel_message: str = "已取消手动选择课程 / 录入链接",
) -> str:
    instruction = Text()
    for index, message in enumerate(messages, start=1):
        instruction.append(f"  {index}. ", style="bold cyan")
        instruction.append(f"{message}\n", style="white")
    instruction.append(
        "\n  按 Enter 换行，输入完成后按 Ctrl+Enter 提交",
        style="bold yellow",
    )
    instruction.append("\n  输入过程中可按 ESC 取消并返回主菜单", style="bold yellow")
    console.print(
        Align.center(
            Panel(
                instruction,
                title=f"[bold white]{title}[/bold white]",
                border_style="cyan",
                box=ROUNDED,
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


def build_summary_renderable(title: str, rows: list[tuple[str, str]]):
    """构造汇总渲染对象（CLI 与 TUI 共用）。"""
    table = Table(
        show_header=False,
        box=SIMPLE_HEAVY,
        border_style="bright_black",
        title=f"[bold white]{title}[/bold white]",
        title_style="bold white",
        min_width=54,
        padding=(0, 1),
    )
    table.add_column("项目", style="dim white", min_width=16, justify="right")
    table.add_column("结果", overflow="fold", min_width=34)
    for left, right in rows:
        table.add_row(left, Text(right, style="bold white"))
    return Align.center(table)


def show_summary(title: str, rows: list[tuple[str, str]]) -> None:
    console.print(build_summary_renderable(title, rows))


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
        TextColumn("[cyan]{task.completed}[/cyan][dim]/{task.total}s[/dim]"),
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
