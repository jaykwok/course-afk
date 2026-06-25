"""Textual TUI 前端：仪表盘 + 活动日志 + 模态提示。

本模块只负责 Textual 界面本身，不依赖 core.* 业务逻辑。桥接层
(core.tui_bridge.TuiFrontend) 负责把 core.ui 的同步接口接到这里。

线程模型：
- Textual 事件循环跑在主线程。
- launcher.main() 整个阻塞循环跑在一条 daemon 工作线程上
  (_spawn_launcher_thread)，浏览器自动化在它内部各自的 run_async 里阻塞，
  不会卡住界面。
- 桥接层通过 app.call_from_thread + Queue 与本模块通信：输出类调用直接
  call_from_thread 写入；阻塞提示类先 call_from_thread 挂载模态屏，再在
  Queue.get 上等待用户 dismiss 的结果。
"""

from __future__ import annotations

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
    OptionList,
    RichLog,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option


# Ctrl+C 强制取消当前模态提示时，用作 dismiss 的返回值；桥接层识别后抛
# UserCancelRequested，让控制流正常返回主菜单（而非直接退出）。
_PROMPT_CANCELLED: Any = object()


class OptionScreen(ModalScreen[int]):
    """菜单 / 多选一。dismiss 值为 1-based 序号。"""

    def __init__(
        self,
        title: str,
        options: list[str],
        prompt: str = "请选择",
    ) -> None:
        super().__init__()
        self._title = title
        self._options = options
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(self._title, id="opt-title"),
            Static(self._prompt, id="opt-hint"),
            OptionList(
                *(
                    Option(f"{idx}. {opt}", id=str(idx))
                    for idx, opt in enumerate(self._options, start=1)
                ),
                id="opt-list",
            ),
            Horizontal(
                Button("确认 [Enter]", id="confirm", variant="primary"),
                id="opt-actions",
            ),
            id="opt-dialog",
        )

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        self.dismiss(event.option_index + 1)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "confirm":
            return
        option_list = self.query_one("#opt-list", OptionList)
        highlighted = option_list.highlighted
        if highlighted is None:
            self.app.bell()
            return
        self.dismiss(highlighted + 1)


class YesNoScreen(ModalScreen[bool]):
    """是 / 否选择。dismiss 值为 bool。"""

    BINDINGS = [
        Binding("y", "yes", "是"),
        Binding("n", "no", "否"),
    ]

    def __init__(self, message: str, default: str = "N") -> None:
        super().__init__()
        self._message = message
        self._default = (default or "N").strip().upper() or "N"

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(self._message, id="yn-msg"),
            Static(
                f"默认 {self._default}（[Y] 是 / [N] 否）",
                id="yn-hint",
            ),
            Horizontal(
                Button("是 [Y]", id="yes", variant="success"),
                Button("否 [N]", id="no", variant="error"),
                id="yn-actions",
            ),
            id="yn-dialog",
        )

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "yes":
            self.dismiss(True)
        elif event.button.id == "no":
            self.dismiss(False)


class MultilineScreen(ModalScreen[tuple[str, Any]]):
    """多行文本输入。dismiss 值为 ("ok", text) 或 ("cancel", None)。

    回车换行、Ctrl+Enter 提交、ESC 取消（与原 CLI 行为一致）。
    """

    BINDINGS = [
        Binding("ctrl+enter", "submit", "提交", priority=True),
        Binding("escape", "cancel", "取消", priority=True),
    ]

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
            Horizontal(
                Button("提交 [Ctrl+Enter]", id="submit", variant="primary"),
                Button("取消 [ESC]", id="cancel"),
                id="ml-actions",
            ),
            id="ml-dialog",
        )

    def action_submit(self) -> None:
        text = self.query_one("#ml-text", TextArea).text
        self.dismiss(("ok", text))

    def action_cancel(self) -> None:
        self.dismiss(("cancel", None))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "submit":
            self.action_submit()
        elif event.button.id == "cancel":
            self.action_cancel()


class PauseScreen(ModalScreen[None]):
    """“按回车返回”提示。dismiss 值为 None。"""

    BINDINGS = [
        Binding("enter", "continue", "继续"),
        Binding("escape", "continue", "继续", show=False),
    ]

    def __init__(self, message: str = "按回车返回主菜单") -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(self._message, id="pause-msg"),
            Horizontal(
                Button("继续 [Enter]", id="continue", variant="primary"),
                id="pause-actions",
            ),
            id="pause-dialog",
        )

    def action_continue(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "continue":
            self.dismiss(None)


class CourseTuiApp(App):
    """课程自动化工具的 Textual 主应用。"""

    CSS = """
    Screen {
        background: $surface;
    }

    #main {
        layout: vertical;
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
    OptionScreen, YesNoScreen, MultilineScreen, PauseScreen {
        align: center middle;
    }

    #opt-dialog, #yn-dialog, #ml-dialog, #pause-dialog {
        width: 68;
        max-width: 92%;
        border: heavy $primary;
        background: $panel;
        padding: 1 2;
    }

    #opt-title, #ml-title, #yn-msg {
        text-style: bold;
        color: $text;
        margin-bottom: 1;
    }

    #yn-msg {
        border-bottom: solid $primary 40%;
        padding-bottom: 1;
    }

    #opt-hint, #yn-hint, #ml-instr, #pause-msg {
        color: $text-muted;
        margin-bottom: 1;
    }

    #opt-list {
        height: auto;
        max-height: 18;
        margin-bottom: 1;
        border: solid $primary 30%;
    }

    #ml-text {
        height: 12;
        margin-bottom: 1;
        border: solid $primary 30%;
    }

    #opt-actions, #yn-actions, #ml-actions, #pause-actions {
        align-horizontal: center;
        height: auto;
        margin-top: 1;
    }

    Button {
        margin: 0 1;
    }
    """

    TITLE = "课程自动化工具"
    SUB_TITLE = "登录 · 学习 · 考试 统一入口"

    # Ctrl+C：Textual 会拦截它，显式绑定到退出动作。
    BINDINGS = [
        Binding("ctrl+c", "request_quit", "退出", show=False, priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        # 当前「可被 Ctrl+C 取消」的模态提示队列（是/否、多行、回车、子菜单）。
        # 主菜单不在此列——在主菜单按 Ctrl+C 应直接退出。仅 app 线程读写，无需锁。
        self._cancellable_prompt_queue: Queue | None = None

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
            except BaseException as exc:  # noqa: BLE001 - 任何未预期错误都展示后退出
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
                self.emit_log, Text(f"  ×  {message}", style="bold red")
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

    def action_request_quit(self) -> None:
        from core.config import interrupt_running_async

        # 1. 工作流中的模态提示（是/否、多行、回车、子菜单）正打开：取消该提示，
        #    桥接层识别 _PROMPT_CANCELLED 后抛 UserCancelRequested，正常返回主菜单。
        if self._cancellable_prompt_queue is not None:
            try:
                self.screen.dismiss(_PROMPT_CANCELLED)
            except Exception:
                pass
            return
        # 2. 工作流正在工作线程上跑（Playwright 阻塞）：跨线程取消，触发优雅保存，
        #    随后以 UserCancelRequested 返回主菜单。
        if interrupt_running_async():
            return
        # 3. 主菜单 / 空闲：直接退出。
        self.exit()

    # ------------------------------------------------------------------
    # 供桥接层挂载模态屏（call_from_thread 调用，阻塞工作线程直到挂载完成）
    # ------------------------------------------------------------------
    async def push_prompt(
        self, screen: ModalScreen, *, cancellable: bool = False
    ) -> Queue:
        queue: Queue = Queue()
        if cancellable:
            self._cancellable_prompt_queue = queue

        def _on_dismiss(result: Any) -> None:
            if self._cancellable_prompt_queue is queue:
                self._cancellable_prompt_queue = None
            queue.put(result)

        await self.push_screen(screen, callback=_on_dismiss)
        return queue
