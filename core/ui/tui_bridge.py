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
from core.palette import GREEN, ERROR, SUCCESS, WARNING
from core.ui import tui_render
from core.ui.tui_app import (
    CourseTuiApp,
    MultilineScreen,
    OptionScreen,
    PauseScreen,
    YesNoScreen,
    _PROMPT_CANCELLED,
)


# ------------------------------------------------------------------
# Rich 渲染小工具（视觉与 CLI 一致；图标按终端 Unicode/ASCII）
# ------------------------------------------------------------------
def _icon_text(icon: str, message: str, *, style: str) -> Text:
    from core.ui.terminal_compat import ui_glyphs

    g = ui_glyphs()
    text = Text()
    text.append(f"  {g.pad_icon(icon)}  ", style=f"bold {style}")
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
        from core.ui.terminal_compat import ui_glyphs

        self._refresh_dashboard()
        self.app.call_from_thread(self.app.set_busy_status, message)
        self.app.call_from_thread(
            self.app.emit_log,
            _icon_text(ui_glyphs().icon_info, message, style=GREEN),
        )

    def _bridge_show_success(self, message: str) -> None:
        from core.ui.terminal_compat import ui_glyphs

        self._refresh_dashboard()
        self.app.call_from_thread(self.app.set_busy_status, message)
        self.app.call_from_thread(
            self.app.emit_log,
            _icon_text(ui_glyphs().icon_success, message, style=SUCCESS),
        )

    def _bridge_show_warning(self, message: str) -> None:
        from core.ui.terminal_compat import ui_glyphs

        self._refresh_dashboard()
        self.app.call_from_thread(self.app.set_busy_status, message)
        self.app.call_from_thread(
            self.app.emit_log,
            _icon_text(ui_glyphs().icon_warning, message, style=WARNING),
        )

    def _bridge_show_error(self, message: str) -> None:
        from core.ui.terminal_compat import ui_glyphs

        self._refresh_dashboard()
        self.app.call_from_thread(self.app.set_busy_status, message)
        self.app.call_from_thread(
            self.app.emit_log,
            _icon_text(ui_glyphs().icon_failure, message, style=ERROR),
        )

    def _bridge_begin_operation(self, title: str, message: str) -> None:
        # 不再弹居中模态：点亮顶部状态条（布局里的一行），让仪表盘/进度条/日志平铺不遮挡。
        self.app.call_from_thread(self.app.set_operation_status, title, message)

    def _bridge_prepare_menu_loading(self) -> None:
        # 返回主菜单：长任务结束，收起状态条并清除「操作中」标记。
        # 结果页 held-screen 会保留到主菜单挂载，无空档。
        self.app.call_from_thread(self.app.end_operation)

    def _bridge_show_summary(self, title: str, rows: list[tuple[str, str]]) -> None:
        self.app.call_from_thread(
            self.app.emit_log, tui_render.build_summary(title, rows)
        )

    def _bridge_render_dashboard(self, state: Any) -> None:
        self._latest_state = state
        self._push_dashboard(state)

    def _dashboard_inputs(self, state: Any) -> tuple[Any, str]:
        """仪表盘数据（工作线程上读取）：credential 元数据 + 建议操作。"""
        from core.auth.credential import load_credential_metadata
        from core.state import recommend_next_step

        metadata = load_credential_metadata()
        recommended = recommend_next_step(
            has_credential=state.has_credential and not state.credential_expired,
            learning_count=state.learning_count,
            exam_count=state.exam_count,
            manual_exam_count=state.manual_exam_count,
        )
        return metadata, recommended

    def _push_dashboard(self, state: Any) -> None:
        """按 tui_render 的扁平 KPI 布局刷新仪表盘卡片与品牌栏账号胶囊。"""
        metadata, recommended = self._dashboard_inputs(state)
        self.app.call_from_thread(
            self.app.update_dashboard,
            tui_render.build_account_chip(state, metadata),
            tui_render.build_stat_tiles(state),
            tui_render.build_dashboard_meta(state, metadata),
            tui_render.build_action_line(recommended),
        )

    def _refresh_dashboard(self) -> None:
        """重读队列文件刷新仪表盘数字。挂课/考试期间每条状态消息后调用一次：
        队列文件随每门课完成而更新，于是「课程 N」会跟着实时递减（24→23→…→0）。
        与状态回调同在工作线程，文件读写无并发问题；读的是小 JSON，开销可忽略。"""
        from core.state import collect_project_state

        self._latest_state = collect_project_state()
        self._push_dashboard(self._latest_state)

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
        status_renderable = None
        if self._latest_state is not None:
            metadata, recommended = self._dashboard_inputs(self._latest_state)
            status_renderable = tui_render.build_menu_status(
                self._latest_state, metadata, recommended
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
        details = tui_render.build_summary(title, rows)
        return self._prompt(
            YesNoScreen(message, default, details_renderable=details),
            cancellable=True,
        )

    def _bridge_pause(self, message: str = "按回车返回主菜单") -> None:
        self._prompt(PauseScreen(message), cancellable=True)

    def _bridge_prepare_pause_with_summary(
        self,
        title: str,
        rows: list[tuple[str, str]],
        message: str = "查看完成后返回主菜单",
    ):
        details = tui_render.build_summary(title, rows)
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
            MultilineScreen(messages, title), cancellable=True
        )
        kind, value = result
        if kind == "cancel":
            raise UserCancelRequested(cancel_message)
        return value

    async def _bridge_wait_with_progress(
        self, duration: int, description: str = "处理中"
    ) -> None:
        """按秒推进进度数字，约 10Hz 刷新 UI，让转圈与 CLI Rich 一样顺滑。"""
        duration = int(duration)
        if duration <= 0:
            return
        # 与 CLI wait_with_progress(refresh_per_second=10) 对齐
        ticks_per_sec = 10
        total_ticks = duration * ticks_per_sec
        self.app.call_from_thread(
            self.app.set_progress, description, 0, duration, 0
        )
        for tick in range(1, total_ticks + 1):
            await asyncio.sleep(1.0 / ticks_per_sec)
            completed = min(duration, tick // ticks_per_sec)
            self.app.call_from_thread(
                self.app.set_progress, description, completed, duration, tick
            )
        self.app.call_from_thread(self.app.clear_progress)



# ------------------------------------------------------------------
# 入口
# ------------------------------------------------------------------
def launch_tui() -> int:
    """启动 Textual TUI，复用 launcher.main() 作为后台控制流。"""
    import launcher

    # 直接从 tui_bridge 启动时，也要在 Textual 接管控制台前关闭 Quick Edit。
    launcher._disable_windows_console_input_modes_early()

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
