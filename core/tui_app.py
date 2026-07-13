"""Textual TUI 前端：仪表盘 + 活动日志 + 模态提示。

本模块只负责 Textual 界面本身，不依赖 core.* 业务逻辑。桥接层
(core.tui_bridge.TuiFrontend) 负责把 core.ui 的同步接口接到这里。

线程模型：
- Textual 事件循环跑在主线程。
- launcher.main() 整个阻塞循环跑在一条 daemon 工作线程上
  (_spawn_launcher_thread)，浏览器自动化在它内部各自的 run_async 里阻塞，
  不会卡住界面。
- 桥接层通过 app.call_from_thread + Queue 与本模块通信：输出类调用直接
  call_from_thread 写入；阻塞提示类挂载模态屏后在 Queue.get 上等待结果。
  已完成的模态屏会保持到下一个模态屏挂载时再原位替换，避免切换间隙闪屏。
"""

from __future__ import annotations

import sys
import threading
from queue import Queue
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    LoadingIndicator,
    OptionList,
    RichLog,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option


# Esc / Ctrl+C 强制取消当前模态提示时使用；桥接层识别后抛
# UserCancelRequested，让控制流正常返回主菜单（而非直接退出）。
_PROMPT_CANCELLED: Any = object()


def _read_windows_clipboard() -> str:
    """读取 Windows 系统剪贴板文本（CF_UNICODETEXT），作为 cmd/conhost 粘贴失效的兜底。

    Textual 自带的 Ctrl+V 读的是应用内部剪贴板（默认空），而传统 cmd/conhost 又不会
    把系统剪贴板作为 paste 事件可靠地发给 TUI；MultilineScreen 因此直接用 Win32 API
    读系统剪贴板再插入。非 Windows 或读取失败返回空串。
    """
    if not sys.platform.startswith("win"):
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        cf_unicode_text = 13
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        user32.CloseClipboard.argtypes = []
        kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]

        if not user32.OpenClipboard(None):
            return ""
        try:
            handle = user32.GetClipboardData(cf_unicode_text)
            if not handle:
                return ""
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()
    except Exception:
        return ""


class OptionScreen(ModalScreen[int]):
    """菜单 / 多选一。提交值为 1-based 序号。

    交互：方向键移动高亮、回车选中，或鼠标直接点击选项即可（不需要单独的确认按钮）。
    提示文字避免 ↑↓ 等 East-Asian ambiguous 宽度字符，防止某些控制台字体下错位。
    """

    AUTO_FOCUS = "#opt-list"

    def __init__(
        self,
        title: str,
        options: list[str],
        prompt: str = "请选择",
        status_renderable: Any | None = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._options = options
        self._prompt = prompt
        self._status_renderable = status_renderable

    def compose(self) -> ComposeResult:
        escape_hint = "ESC 退出" if self._status_renderable is not None else "ESC 返回"
        content = [Static(self._title, id="opt-title")]
        if self._status_renderable is not None:
            content.append(Static(self._status_renderable, id="opt-status"))
        content.extend(
            (
                Static(
                    f"{self._prompt}（方向键移动，回车或点击确认 | {escape_hint}）",
                    id="opt-hint",
                ),
                OptionList(
                    *(
                        Option(f"{idx}. {opt}", id=str(idx))
                        for idx, opt in enumerate(self._options, start=1)
                    ),
                    id="opt-list",
                ),
            )
        )
        yield Vertical(
            *content,
            id="opt-dialog",
        )

    def on_mount(self) -> None:
        # 等模态层挂载完成后再显示底层内容，避免切换时漏出日志或账号卡片。
        self.app.set_main_content_visible(True)

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        self.app.resolve_prompt(self, event.option_index + 1)


class YesNoScreen(ModalScreen[bool]):
    """是 / 否选择。提交值为 bool。"""

    AUTO_FOCUS = "#yes"

    BINDINGS = [
        Binding("y", "yes", "是"),
        Binding("n", "no", "否"),
        Binding("left,up", "app.focus_previous", "上一项", show=False),
        Binding("right,down", "app.focus_next", "下一项", show=False),
    ]

    def __init__(
        self,
        message: str,
        default: str = "N",
        *,
        details_renderable: Any | None = None,
    ) -> None:
        super().__init__()
        self._message = message
        self._default = (default or "N").strip().upper() or "N"
        self._details_renderable = details_renderable
        if details_renderable is not None:
            self.add_class("with-details")

    def compose(self) -> ComposeResult:
        content = [Static(self._message, id="yn-msg")]
        if self._details_renderable is not None:
            content.append(Static(self._details_renderable, id="yn-details"))
        content.extend(
            (
                Static(
                    f"默认 {self._default}（[Y] 是 / [N] 否 · ESC 返回）",
                    id="yn-hint",
                ),
                Horizontal(
                    Button("是 [Y]", id="yes", variant="success"),
                    Button("否 [N]", id="no", variant="error"),
                    id="yn-actions",
                ),
            )
        )
        yield Vertical(*content, id="yn-dialog")

    def on_mount(self) -> None:
        self.app.set_main_content_visible(True)

    def action_yes(self) -> None:
        self.app.resolve_prompt(self, True)

    def action_no(self) -> None:
        self.app.resolve_prompt(self, False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "yes":
            self.action_yes()
        elif event.button.id == "no":
            self.action_no()


class MultilineScreen(ModalScreen[tuple[str, Any]]):
    """多行文本输入。提交值为 ("ok", text) 或 ("cancel", None)。

    回车换行、Ctrl+Enter 提交、ESC 取消（与原 CLI 行为一致）。
    """

    AUTO_FOCUS = "#ml-text"

    BINDINGS = [
        # 支持增强键盘协议的 ctrl+enter，也兼容传统终端将其编码为 LF / ctrl+j。
        Binding("ctrl+enter,ctrl+j", "submit", "提交", priority=True),
    ] + (
        # conhost(cmd) 不会把系统剪贴板作为 paste 事件发给 TUI，Textual 自带 Ctrl+V
        # 又只读应用内部剪贴板；这里劫持 Ctrl+V 直接读 Windows 系统剪贴板再插入。
        [Binding("ctrl+v", "paste_clipboard", "粘贴", priority=True)]
        if sys.platform.startswith("win")
        else []
    )

    def __init__(
        self,
        messages: list[str],
        title: str = "手动选择课程 / 录入链接",
        cancel_message: str = "已取消",
    ) -> None:
        super().__init__()
        self._messages = messages
        self._title = title
        self._cancel_message = cancel_message

    def compose(self) -> ComposeResult:
        instruction_lines = "\n".join(
            f"{idx}. {msg}" for idx, msg in enumerate(self._messages, start=1)
        )
        yield Vertical(
            Static(self._title, id="ml-title"),
            Static(instruction_lines, id="ml-instr"),
            TextArea(id="ml-text"),
            Static(
                "Enter 换行 | Ctrl+V 粘贴 | Ctrl+Enter 提交 | ESC 返回",
                id="ml-hint",
            ),
            Horizontal(
                Button("提交 [Ctrl+Enter]", id="submit", variant="primary"),
                Button("取消 [ESC]", id="cancel"),
                id="ml-actions",
            ),
            id="ml-dialog",
        )

    def on_mount(self) -> None:
        self.app.set_main_content_visible(True)

    def action_submit(self) -> None:
        text = self.query_one("#ml-text", TextArea).text
        self.app.resolve_prompt(self, ("ok", text))

    def action_paste_clipboard(self) -> None:
        text = _read_windows_clipboard()
        if text:
            self.query_one("#ml-text", TextArea).insert(text)

    def action_cancel(self) -> None:
        self.app.resolve_prompt(self, ("cancel", None))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "submit":
            self.action_submit()
        elif event.button.id == "cancel":
            self.action_cancel()


class PauseScreen(ModalScreen[None]):
    """“按回车返回”提示。提交值为 None。"""

    AUTO_FOCUS = "#continue"

    BINDINGS = [
        Binding("enter", "continue", "继续"),
    ]

    def __init__(
        self,
        message: str = "按回车返回主菜单",
        *,
        details_renderable: Any | None = None,
        button_label: str = "继续 [Enter]",
    ) -> None:
        super().__init__()
        self._message = message
        self._details_renderable = details_renderable
        self._button_label = button_label
        if details_renderable is not None:
            self.add_class("with-details")

    def compose(self) -> ComposeResult:
        content = []
        if self._details_renderable is not None:
            content.append(Static(self._details_renderable, id="pause-details"))
        hint_action = "确定" if self._button_label.startswith("OK") else "继续"
        content.extend(
            (
                Static(self._message, id="pause-msg"),
                Static(f"Enter {hint_action} · ESC 返回", id="pause-hint"),
                Horizontal(
                    Button(self._button_label, id="continue", variant="primary"),
                    id="pause-actions",
                ),
            )
        )
        yield Vertical(*content, id="pause-dialog")

    def on_mount(self) -> None:
        self.app.set_main_content_visible(True)

    def action_continue(self) -> None:
        self.app.resolve_prompt(self, None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "continue":
            self.action_continue()


class BusyScreen(ModalScreen[None]):
    """浏览器及后台任务运行期间持续显示的状态界面。"""

    AUTO_FOCUS = ""

    def __init__(self, title: str, message: str) -> None:
        super().__init__()
        self._title = title
        self._message = message

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(self._title, id="busy-title"),
            LoadingIndicator(id="busy-indicator"),
            Static(self._message, id="busy-message"),
            Static("任务执行期间可按 ESC 取消并返回主菜单", id="busy-hint"),
            id="busy-dialog",
        )

    def on_mount(self) -> None:
        self.app.set_main_content_visible(True)

    def update_status(self, message: str) -> None:
        self.query_one("#busy-message", Static).update(message)


class CourseTuiApp(App):
    """课程自动化工具的 Textual 主应用。"""

    CSS = """
    Screen {
        background: $surface;
    }

    #main {
        layout: vertical;
        visibility: hidden;
    }

    #dashboard {
        height: auto;
        max-height: 16;
        border: round $accent 60%;
        padding: 0 1;
        margin: 1 0 0 0;
        color: $text;
    }

    #progress {
        height: 1;
        margin: 0;
        padding: 0 1;
        color: $text;
    }

    #log {
        border: round $primary 50%;
        margin: 1 0;
        height: 1fr;
        background: $surface;
    }

    /* ---------- 模态屏 ---------- */
    OptionScreen, YesNoScreen, MultilineScreen, PauseScreen, BusyScreen {
        align: center middle;
    }

    #opt-dialog, #yn-dialog, #ml-dialog, #pause-dialog, #busy-dialog {
        width: 80;
        height: auto;
        max-width: 94%;
        max-height: 96%;
        border: heavy $primary;
        background: $panel;
        padding: 1 2;
    }

    #opt-title, #ml-title, #yn-msg, #busy-title {
        text-style: bold;
        color: $text;
        margin-bottom: 1;
    }

    #yn-msg {
        border-bottom: solid $primary 40%;
        padding-bottom: 1;
    }

    #opt-hint, #yn-hint, #ml-instr, #ml-hint, #pause-msg, #pause-hint,
    #busy-message, #busy-hint {
        color: $text-muted;
        margin-bottom: 1;
    }

    #opt-status {
        height: auto;
        margin-bottom: 1;
    }

    #yn-details, #pause-details {
        height: 1fr;
        min-height: 3;
        overflow-y: auto;
        margin-bottom: 1;
    }

    #busy-dialog {
        height: auto;
        max-height: 18;
    }

    #busy-indicator {
        height: 3;
        margin-bottom: 1;
    }

    YesNoScreen.with-details #yn-dialog,
    PauseScreen.with-details #pause-dialog {
        height: 90%;
        max-height: 34;
    }

    #opt-list {
        height: auto;
        max-height: 15;
        margin-bottom: 1;
        border: solid $primary 30%;
    }

    #ml-dialog {
        height: 90%;
        min-height: 16;
        max-height: 36;
    }

    #ml-instr {
        height: auto;
        max-height: 8;
        overflow-y: auto;
    }

    #ml-text {
        height: 1fr;
        min-height: 3;
        margin-bottom: 1;
        border: solid $primary 30%;
    }

    #yn-actions, #ml-actions, #pause-actions {
        align-horizontal: center;
        height: 3;
        margin-top: 0;
    }

    Button {
        margin: 0 1;
    }
    """

    TITLE = "课程自动化工具"
    SUB_TITLE = "登录 · 学习 · 考试 统一入口"

    # Esc 在任何界面都表示返回；主菜单没有上一级，因此返回即退出。
    # Ctrl+C 保留同样的优雅取消能力。
    BINDINGS = [
        Binding("escape", "go_back", "返回", show=True, priority=True),
        Binding("ctrl+c", "request_quit", "退出", show=False, priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        # 当前「可被 Esc / Ctrl+C 取消」的模态提示队列（是/否、多行、回车、子菜单）。
        # 主菜单不在此列——在主菜单返回即退出。仅 app 线程读写，无需锁。
        self._cancellable_prompt_queue: Queue | None = None
        self._active_prompt_queue: Queue | None = None
        self._active_prompt_screen: ModalScreen | None = None
        self._held_prompt_screen: ModalScreen | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            Static(id="dashboard"),
            Static(id="progress"),
            RichLog(id="log", markup=True, auto_scroll=True),
            id="main",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._spawn_launcher_thread()

    # ------------------------------------------------------------------
    # 工作线程：跑 launcher.main()。daemon=True 保证 Ctrl+C / 退出时
    # 即使卡在 Playwright 里，进程也能干净退出。
    # ------------------------------------------------------------------
    def _spawn_launcher_thread(self) -> None:
        import launcher

        def target() -> None:
            try:
                launcher.main()
            except Exception as exc:  # noqa: BLE001 - 未预期错误展示后退出；SystemExit/KeyboardInterrupt 放行
                self._safe_emit_error(f"运行出错：{exc}")
            finally:
                self._safe_exit()

        threading.Thread(
            target=target, name="course-launcher", daemon=True
        ).start()

    def _safe_exit(self) -> None:
        try:
            self.call_from_thread(self.exit)
        except Exception:
            pass

    def _safe_emit_error(self, message: str) -> None:
        try:
            from rich.text import Text

            self.call_from_thread(
                self.emit_log, Text(f"  [-]  {message}", style="bold red")
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 桥接层回调（始终经由 call_from_thread 调用，在 app 线程上执行）
    # ------------------------------------------------------------------
    def emit_log(self, renderable: Any) -> None:
        self.query_one("#log", RichLog).write(renderable)

    def set_dashboard(self, renderable: Any) -> None:
        self.query_one("#dashboard", Static).update(renderable)

    def set_main_content_visible(self, visible: bool) -> None:
        main_content = self.query_one("#main", Vertical)
        main_content.styles.visibility = "visible" if visible else "hidden"

    def set_busy_status(self, message: str) -> None:
        if isinstance(self.screen, BusyScreen):
            self.screen.update_status(message)

    def set_title(self, title: str, subtitle: str | None) -> None:
        if title:
            self.title = title
        self.sub_title = subtitle or ""

    def set_progress(self, description: str, completed: int, total: int) -> None:
        if total <= 0:
            return
        ratio = max(0, min(1, completed / total))
        bar_width = 20
        filled = int(ratio * bar_width)
        bar = "█" * filled + "░" * (bar_width - filled)
        text = f"  {description}  {bar} {completed}/{total}s ({ratio * 100:>3.0f}%)"
        from rich.text import Text

        self.query_one("#progress", Static).update(Text(text, style="cyan"))

    def clear_progress(self) -> None:
        self.query_one("#progress", Static).update("")

    def _cancel_current_operation_or_exit(self) -> None:
        from core.config import interrupt_running_async

        # 1. 工作流中的模态提示（是/否、多行、回车、子菜单）正打开：取消该提示，
        #    桥接层识别 _PROMPT_CANCELLED 后抛 UserCancelRequested，正常返回主菜单。
        # 仅当栈顶仍是当前提示时才提交取消，避免误操作其后挂载的其他屏。
        if (
            self._cancellable_prompt_queue is not None
            and self._active_prompt_screen is self.screen
        ):
            self.resolve_prompt(self.screen, _PROMPT_CANCELLED)
            return
        # 2. 工作流正在工作线程上跑（Playwright 阻塞）：跨线程取消，触发优雅保存，
        #    随后以 UserCancelRequested 返回主菜单。
        if interrupt_running_async():
            return
        # 3. 主菜单 / 空闲：直接退出。
        self.exit()

    def action_go_back(self) -> None:
        """Esc：取消当前操作并返回上一级；在主菜单直接退出。"""
        self._cancel_current_operation_or_exit()

    def action_request_quit(self) -> None:
        """Ctrl+C：沿用可保存进度的取消 / 退出流程。"""
        self._cancel_current_operation_or_exit()

    # ------------------------------------------------------------------
    # 供桥接层挂载模态屏（call_from_thread 调用，阻塞工作线程直到挂载完成）
    # ------------------------------------------------------------------
    async def push_prompt(
        self, screen: ModalScreen, *, cancellable: bool = False
    ) -> Queue:
        queue: Queue = Queue()
        previous_screen = self._held_prompt_screen
        self._held_prompt_screen = None
        self._active_prompt_queue = queue
        self._active_prompt_screen = screen
        if cancellable:
            self._cancellable_prompt_queue = queue
        else:
            self._cancellable_prompt_queue = None

        # 用原位替换完成模态屏交接。旧界面在新界面挂载完成前始终保留，
        # 因而不会露出底层账号、日志或空白背景。
        if previous_screen is not None and self.screen is previous_screen:
            # switch_screen 会把挂载收尾安排到当前消息之后；这里不能等待，
            # 否则 call_from_thread 的异步回调会与 call_next 相互等待。
            self.switch_screen(screen)
        else:
            await self.push_screen(screen)
        return queue

    async def show_busy(self, title: str, message: str) -> None:
        """原位切换到任务状态页，并保持到结果提示挂载。"""
        screen = BusyScreen(title, message)
        previous_screen = self._held_prompt_screen
        self._active_prompt_queue = None
        self._active_prompt_screen = None
        self._cancellable_prompt_queue = None
        self._held_prompt_screen = screen
        if previous_screen is not None and self.screen is previous_screen:
            self.switch_screen(screen)
        else:
            await self.push_screen(screen)

    def resolve_prompt(self, screen: ModalScreen, result: Any) -> None:
        """提交当前提示结果，但保持画面直到下一个提示完成原位替换。"""
        if screen is not self._active_prompt_screen:
            return
        queue = self._active_prompt_queue
        if queue is None:
            return

        self._active_prompt_queue = None
        self._active_prompt_screen = None
        if self._cancellable_prompt_queue is queue:
            self._cancellable_prompt_queue = None
        self._held_prompt_screen = screen
        queue.put(result)
