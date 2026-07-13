"""把 core.ui 的同步接口桥接到 Textual TUI。

核心思路：launcher.main() 及其下游 controller/workflow 一行都不改。
本模块在 TUI 启动时把 core.ui 模块上的公开函数替换成桥接方法，于是
launcher 解析到的 ui.show_menu / controller 传入的 status_callback=ui.show_info
等都自动走到 TUI。唯一一处直接 import 的 core.ui 函数
(learning_common.py 的 wait_with_progress) 是懒导入，patch 后同样生效。

- 输出类 (show_info / show_success / ...)：app.call_from_thread 投递到
  活动日志，不在工作线程上长阻塞。
- 阻塞提示类 (show_menu / prompt_* / pause)：先 call_from_thread 挂载模态屏，
  再在 Queue.get 上等待用户选择结果，从而复用现有阻塞式控制流。
- 日志：接管 setup_logging 默认装的控制台 StreamHandler，改成镜像到活动日志，
  避免它和 Textual 全屏界面抢终端。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from rich.text import Text
from textual.widgets import RichLog

import core.ui as cli_ui
from core.abort import UserCancelRequested
from core.config import LOG_FORMAT, _get_console_log_level, setup_logging
from core.palette import CYAN, ERROR, SUCCESS, WARNING
from core.tui_app import (
    CourseTuiApp,
    MultilineScreen,
    OptionScreen,
    PauseScreen,
    YesNoScreen,
    _PROMPT_CANCELLED,
)


# ------------------------------------------------------------------
# Rich 渲染小工具（视觉与原 CLI 保持一致）
# ------------------------------------------------------------------
def _icon_text(icon: str, message: str, *, style: str) -> Text:
    text = Text()
    text.append(f"  {icon}  ", style=f"bold {style}")
    text.append(message, style=style)
    return text


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
                style = f"bold {ERROR}"
            elif record.levelno >= logging.WARNING:
                style = WARNING
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
        "begin_operation": "_bridge_begin_operation",
        "render_dashboard": "_bridge_render_dashboard",
        "show_summary": "_bridge_show_summary",
        "show_menu": "_bridge_show_menu",
        "prompt_choice": "_bridge_prompt_choice",
        "prompt_yes_no": "_bridge_prompt_yes_no",
        "prompt_summary_confirmation": "_bridge_prompt_summary_confirmation",
        "prompt_multiline_input": "_bridge_prompt_multiline_input",
        "pause": "_bridge_pause",
        "pause_with_summary": "_bridge_pause_with_summary",
        "prepare_menu_loading": "_bridge_prepare_menu_loading",
        "prepare_pause_with_summary": "_bridge_prepare_pause_with_summary",
        "wait_prepared_prompt": "_bridge_wait_prepared_prompt",
        "wait_with_progress": "_bridge_wait_with_progress",
    }

    def __init__(self, app: CourseTuiApp) -> None:
        self.app = app
        self._originals: dict[str, Any] = {}
        self._latest_state: Any | None = None

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
        self.app.call_from_thread(self.app.set_busy_status, message)
        self.app.call_from_thread(
            self.app.emit_log, _icon_text("[*]", message, style=CYAN)
        )

    def _bridge_show_success(self, message: str) -> None:
        self.app.call_from_thread(self.app.set_busy_status, message)
        self.app.call_from_thread(
            self.app.emit_log, _icon_text("[+]", message, style=SUCCESS)
        )

    def _bridge_show_warning(self, message: str) -> None:
        self.app.call_from_thread(self.app.set_busy_status, message)
        self.app.call_from_thread(
            self.app.emit_log, _icon_text("[!]", message, style=WARNING)
        )

    def _bridge_show_error(self, message: str) -> None:
        self.app.call_from_thread(self.app.set_busy_status, message)
        self.app.call_from_thread(
            self.app.emit_log, _icon_text("[-]", message, style=ERROR)
        )

    def _bridge_begin_operation(self, title: str, message: str) -> None:
        self.app.call_from_thread(self.app.show_busy, title, message)

    def _bridge_prepare_menu_loading(self) -> None:
        # 用忙碌状态顶掉持有的结果页，填满“结果页确认 → 主菜单挂载”的间隙。
        self.app.call_from_thread(self.app.show_busy, "主菜单", "正在加载主菜单…")

    def _bridge_show_summary(self, title: str, rows: list[tuple[str, str]]) -> None:
        self.app.call_from_thread(
            self.app.emit_log, cli_ui.build_summary_renderable(title, rows, expand=True)
        )

    def _bridge_render_dashboard(self, state: Any) -> None:
        self._latest_state = state
        self.app.call_from_thread(
            self.app.set_dashboard, cli_ui.build_dashboard_renderable(state, expand=True)
        )

    # ---------------- 阻塞提示类（工作线程在 Queue.get 上等待结果）----------------
    def _prompt(self, screen: Any, *, cancellable: bool = False) -> Any:
        # call_from_thread 阻塞工作线程直到模态屏挂载完成并返回 Queue；
        # 随后 Queue.get 阻塞直到用户操作把结果写入队列。
        queue = self.app.call_from_thread(
            self.app.push_prompt, screen, cancellable=cancellable
        )
        result = queue.get()
        # Ctrl+C 强制取消（仅 cancellable 提示）→ 抛 UserCancelRequested 返回主菜单
        if result is _PROMPT_CANCELLED:
            raise UserCancelRequested("已取消当前操作，返回主菜单")
        return result

    def _bridge_show_menu(self, options: list[str]) -> int:
        # 主菜单不可取消：在主菜单按 Ctrl+C 直接退出应用
        status_renderable = (
            cli_ui.build_menu_status_renderable(self._latest_state)
            if self._latest_state is not None
            else None
        )
        return self._prompt(
            OptionScreen(
                "主菜单",
                options,
                "请选择功能",
                status_renderable=status_renderable,
            )
        )

    def _bridge_prompt_choice(
        self, title: str, options: list[str], prompt: str = "请选择"
    ) -> int:
        return self._prompt(OptionScreen(title, options, prompt), cancellable=True)

    def _bridge_prompt_yes_no(self, message: str, default: str = "N") -> bool:
        return self._prompt(YesNoScreen(message, default), cancellable=True)

    def _bridge_prompt_summary_confirmation(
        self,
        title: str,
        rows: list[tuple[str, str]],
        message: str = "确认继续处理？",
        default: str = "Y",
    ) -> bool:
        details = cli_ui.build_summary_renderable(title, rows, expand=True)
        return self._prompt(
            YesNoScreen(message, default, details_renderable=details),
            cancellable=True,
        )

    def _bridge_pause(self, message: str = "按回车返回主菜单") -> None:
        self._prompt(PauseScreen(message), cancellable=True)

    def _bridge_pause_with_summary(
        self,
        title: str,
        rows: list[tuple[str, str]],
        message: str = "查看完成后返回主菜单",
    ) -> None:
        handle = self._bridge_prepare_pause_with_summary(title, rows, message)
        self._bridge_wait_prepared_prompt(handle)

    def _bridge_prepare_pause_with_summary(
        self,
        title: str,
        rows: list[tuple[str, str]],
        message: str = "查看完成后返回主菜单",
    ):
        details = cli_ui.build_summary_renderable(title, rows, expand=True)
        return self.app.call_from_thread(
            self.app.push_prompt,
            PauseScreen(
                message,
                details_renderable=details,
                button_label="OK [Enter]",
            ),
            cancellable=True,
        )

    def _bridge_wait_prepared_prompt(self, handle) -> None:
        result = handle.get()
        if result is _PROMPT_CANCELLED:
            raise UserCancelRequested("已取消当前操作，返回主菜单")

    def _bridge_prompt_multiline_input(
        self,
        messages: list[str],
        *,
        title: str = "手动选择课程 / 录入链接",
        cancel_message: str = "已取消手动选择课程 / 录入链接",
    ) -> str:
        result = self._prompt(
            MultilineScreen(messages, title, cancel_message), cancellable=True
        )
        kind, value = result
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
