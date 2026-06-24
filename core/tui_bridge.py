"""把 core.ui 的同步接口桥接到 Textual TUI。

核心思路：launcher.main() 及其下游 controller/workflow 一行都不改。
本模块在 TUI 启动时把 core.ui 模块上的公开函数替换成桥接方法，于是
launcher 解析到的 ui.show_menu / controller 传入的 status_callback=ui.show_info
等都自动走到 TUI。唯一一处直接 import 的 core.ui 函数
(learning_common.py 的 wait_with_progress) 是懒导入，patch 后同样生效。

- 输出类 (show_info / show_success / ...)：app.call_from_thread 投递到
  活动日志，不在工作线程上长阻塞。
- 阻塞提示类 (show_menu / prompt_* / pause)：先 call_from_thread 挂载模态屏，
  再在 Queue.get 上等待用户 dismiss 的结果，从而复用现有阻塞式控制流。
- 日志：接管 setup_logging 默认装的控制台 StreamHandler，改成镜像到活动日志，
  避免它和 Textual 全屏界面抢终端。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from rich.align import Align
from rich.box import ROUNDED, SIMPLE_HEAVY
from rich.table import Table
from rich.text import Text
from textual.widgets import RichLog

import core.ui as cli_ui
from core.abort import UserCancelRequested
from core.config import LOG_FORMAT, _get_console_log_level, setup_logging
from core.credential import load_credential_metadata
from core.state import recommend_next_step
from core.tui_app import (
    CourseTuiApp,
    MultilineScreen,
    OptionScreen,
    PauseScreen,
    YesNoScreen,
)


# ------------------------------------------------------------------
# Rich 渲染小工具（视觉与原 CLI 保持一致）
# ------------------------------------------------------------------
def _icon_text(icon: str, message: str, *, style: str) -> Text:
    text = Text()
    text.append(f"  {icon}  ", style=f"bold {style}")
    text.append(message, style=style)
    return text


def _build_dashboard(state: Any) -> Align:
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
        title="当前状态",
        title_style="bold",
        min_width=50,
        padding=(0, 1),
    )
    table.add_column("项目", style="dim", min_width=8, justify="right")
    table.add_column("值", overflow="fold", min_width=36)
    table.add_row("账号", Text(account_label, style="bold"))
    table.add_row("凭证", cli_ui._credential_display(state, metadata))
    table.add_row("课程链接", cli_ui._count_display(state.learning_count))
    table.add_row("挂课失败", cli_ui._count_display(state.learning_failure_count))
    table.add_row("考试链接", cli_ui._count_display(state.exam_count))
    table.add_row("人工考试", cli_ui._count_display(state.manual_exam_count))
    table.add_row("建议操作", Text(f"→ {recommended}", style="bold yellow"))
    return Align.center(table)


def _build_summary_table(title: str, rows: list[tuple[str, str]]) -> Align:
    table = Table(
        show_header=False,
        box=SIMPLE_HEAVY,
        border_style="bright_black",
        title=title,
        title_style="bold",
        min_width=50,
        padding=(0, 1),
    )
    table.add_column("项目", style="dim", min_width=14, justify="right")
    table.add_column("结果", overflow="fold", min_width=30)
    for left, right in rows:
        table.add_row(left, Text(str(right), style="bold"))
    return Align.center(table)


# ------------------------------------------------------------------
# 日志：镜像到活动日志，替换默认控制台 handler
# ------------------------------------------------------------------
class TextualLogHandler(logging.Handler):
    """把日志记录镜像到 TUI 活动日志面板，替代抢终端的 StreamHandler。"""

    def __init__(self, app: CourseTuiApp) -> None:
        super().__init__()
        self._app = app

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            if record.levelno >= logging.ERROR:
                style = "bold red"
            elif record.levelno >= logging.WARNING:
                style = "yellow"
            else:
                style = "dim"
            self._app.call_from_thread(self._write, Text(message, style=style))
        except Exception:  # noqa: BLE001 - app 未运行 / 已退出时安静丢弃
            pass

    def _write(self, renderable: Any) -> None:
        try:
            self._app.query_one("#log", RichLog).write(renderable)
        except Exception:  # noqa: BLE001
            pass


def _install_log_handler(app: CourseTuiApp) -> None:
    """移除抢终端的控制台 StreamHandler，换成镜像到 TUI 的 handler。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        # FileHandler 是 StreamHandler 的子类，要先放行文件 handler
        if isinstance(handler, logging.FileHandler):
            continue
        if isinstance(handler, logging.StreamHandler):
            root.removeHandler(handler)

    handler = TextualLogHandler(app)
    handler.setLevel(_get_console_log_level())
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(handler)


# ------------------------------------------------------------------
# 桥接前端：实现 core.ui 接口契约
# ------------------------------------------------------------------
class TuiFrontend:
    """实现 core.ui 公开接口，把调用桥接到 Textual 应用。"""

    # core.ui 上需要替换的公开符号 -> 桥接方法名
    _PATCH_MAP = {
        "show_title": "_bridge_show_title",
        "show_info": "_bridge_show_info",
        "show_success": "_bridge_show_success",
        "show_warning": "_bridge_show_warning",
        "show_error": "_bridge_show_error",
        "render_dashboard": "_bridge_render_dashboard",
        "show_summary": "_bridge_show_summary",
        "show_menu": "_bridge_show_menu",
        "prompt_choice": "_bridge_prompt_choice",
        "prompt_yes_no": "_bridge_prompt_yes_no",
        "prompt_multiline_input": "_bridge_prompt_multiline_input",
        "pause": "_bridge_pause",
        "wait_with_progress": "_bridge_wait_with_progress",
    }

    def __init__(self, app: CourseTuiApp) -> None:
        self.app = app
        self._originals: dict[str, Any] = {}

    def install(self) -> None:
        for attr_name, method_name in self._PATCH_MAP.items():
            self._originals[attr_name] = getattr(cli_ui, attr_name, None)
            setattr(cli_ui, attr_name, getattr(self, method_name))

    def restore(self) -> None:
        for attr_name, original in self._originals.items():
            if original is not None:
                setattr(cli_ui, attr_name, original)
        self._originals.clear()

    # ---------------- 输出类（投递到活动日志，不阻塞业务流程）----------------
    def _bridge_show_title(self, title: str, subtitle: str | None = None) -> None:
        self.app.call_from_thread(self.app.set_title, title, subtitle)

    def _bridge_show_info(self, message: str) -> None:
        self.app.call_from_thread(
            self.app.emit_log, _icon_text("·", message, style="cyan")
        )

    def _bridge_show_success(self, message: str) -> None:
        self.app.call_from_thread(
            self.app.emit_log, _icon_text("√", message, style="green")
        )

    def _bridge_show_warning(self, message: str) -> None:
        self.app.call_from_thread(
            self.app.emit_log, _icon_text("!", message, style="yellow")
        )

    def _bridge_show_error(self, message: str) -> None:
        self.app.call_from_thread(
            self.app.emit_log, _icon_text("×", message, style="red")
        )

    def _bridge_show_summary(self, title: str, rows: list[tuple[str, str]]) -> None:
        self.app.call_from_thread(
            self.app.emit_log, _build_summary_table(title, rows)
        )

    def _bridge_render_dashboard(self, state: Any) -> None:
        self.app.call_from_thread(self.app.set_dashboard, _build_dashboard(state))

    # ---------------- 阻塞提示类（工作线程在 Queue.get 上等待结果）----------------
    def _prompt(self, screen: Any) -> Any:
        # call_from_thread 阻塞工作线程直到模态屏挂载完成并返回 Queue；
        # 随后 Queue.get 阻塞直到用户 dismiss（dismiss 回调会把结果 put 进队列）。
        queue = self.app.call_from_thread(self.app.push_prompt, screen)
        return queue.get()

    def _bridge_show_menu(self, options: list[str]) -> int:
        return self._prompt(OptionScreen("主菜单", options, "请选择功能"))

    def _bridge_prompt_choice(
        self, title: str, options: list[str], prompt: str = "请选择"
    ) -> int:
        return self._prompt(OptionScreen(title, options, prompt))

    def _bridge_prompt_yes_no(self, message: str, default: str = "N") -> bool:
        return self._prompt(YesNoScreen(message, default))

    def _bridge_pause(self, message: str = "按回车返回主菜单") -> None:
        self._prompt(PauseScreen(message))

    def _bridge_prompt_multiline_input(
        self,
        messages: list[str],
        *,
        title: str = "手动选择课程 / 录入链接",
        cancel_message: str = "已取消手动选择课程 / 录入链接",
    ) -> str:
        kind, value = self._prompt(MultilineScreen(messages, title, cancel_message))
        if kind == "cancel":
            raise UserCancelRequested(cancel_message)
        return value

    async def _bridge_wait_with_progress(
        self, duration: int, description: str = "处理中"
    ) -> None:
        duration = int(duration)
        if duration <= 0:
            return
        self.app.call_from_thread(self.app.set_progress, description, 0, duration)
        for completed in range(1, duration + 1):
            await asyncio.sleep(1)
            self.app.call_from_thread(
                self.app.set_progress, description, completed, duration
            )
        self.app.call_from_thread(self.app.clear_progress)


# ------------------------------------------------------------------
# 入口
# ------------------------------------------------------------------
def launch_tui() -> int:
    """启动 Textual TUI，复用 launcher.main() 作为后台控制流。"""
    import launcher  # 在 Textual 接管控制台前，先触发 launcher 的控制台模式调整

    setup_logging()

    app = CourseTuiApp()
    _install_log_handler(app)

    frontend = TuiFrontend(app)
    frontend.install()
    try:
        app.run()
    finally:
        frontend.restore()
    return 0
