"""Explicit synchronous frontend for the Textual application and its worker."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from rich.text import Text

from core.abort import UserCancelRequested
from core.diagnostics import redact_text
from core.config import (
    LOG_FORMAT,
    _get_console_log_level,
    setup_logging,
    _RedactingFormatter,
)
from core.palette import GREEN, ERROR, SUCCESS, WARNING
from core.ui import tui_render
from core.ui.terminal_compat import ui_glyphs
from core.ui.tui_app import (
    CourseTuiApp,
    MultilineScreen,
    OptionScreen,
    PauseScreen,
    YesNoScreen,
    _PROMPT_CANCELLED,
)


_AFK_START_STATUS = "开始挂课"


# ------------------------------------------------------------------
# Rich 渲染小工具（图标按终端 Unicode/ASCII 自适应）
# ------------------------------------------------------------------
def _icon_text(icon: str, message: str, *, style: str) -> Text:
    g = ui_glyphs()
    text = Text()
    text.append(f"  {g.pad_icon(icon)}  ", style=f"bold {style}")
    text.append(redact_text(message), style=style)
    return text


# ------------------------------------------------------------------
# 日志：镜像到活动日志，替换默认控制台 handler
# ------------------------------------------------------------------
class TextualLogHandler(logging.Handler):
    """把日志记录镜像到 TUI 活动日志面板，替代抢终端的 StreamHandler。

    经 enqueue_log 有界缓冲投递（UI 定时批量落盘），日志风暴时不阻塞业务线程。
    """

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
            self._app.enqueue_log(Text(message, style=style))
            if record.levelno >= logging.WARNING:
                self._app.record_operation_feedback(
                    Text(redact_text(record.getMessage()), style=style)
                )
        except Exception:  # noqa: BLE001 - app 未运行 / 已退出时安静丢弃
            pass


def _install_log_handler(app: CourseTuiApp):
    """移除抢终端的控制台 StreamHandler，换成镜像到 TUI 的 handler。"""
    root = logging.getLogger()
    previous = []
    for handler in list(root.handlers):
        # FileHandler 是 StreamHandler 的子类，要先放行文件 handler
        if isinstance(handler, logging.FileHandler):
            continue
        if isinstance(handler, logging.StreamHandler):
            root.removeHandler(handler)
            previous.append(handler)

    handler = TextualLogHandler(app)
    handler.setLevel(_get_console_log_level())
    handler.setFormatter(_RedactingFormatter(LOG_FORMAT))
    root.addHandler(handler)
    return handler, previous


# ------------------------------------------------------------------
# 桥接前端：实现 core.ui 接口契约
# ------------------------------------------------------------------
class TuiFrontend:
    """实现 core.ui 公开接口，把调用桥接到 Textual 应用。"""

    def __init__(self, app: CourseTuiApp) -> None:
        self.app = app
        self.app.frontend = self
        from core.state import StateRevision

        self._revision = StateRevision()
        self._latest_state: Any | None = None
        # 仪表盘去重签名：计数/账号不变就跳过重建与投递（挂课期间每条状态
        # 消息都会触发 _refresh_dashboard，但计数一门课才变一次）。
        self._last_dashboard_signature: tuple | None = None
        # SQLite 提交版本与凭证修改标记，避免日志风暴反复扫描计数
        self._last_revision_signature: tuple | None = None

    def _post(self, **fields: Any) -> None:
        """把控制类 UI 更新写入合并缓冲并投递信号（fire-and-forget，带背压）。

        值字段（title/dashboard/status/progress）latest-value 合并、事件字段
        （begin_operation/end_operation）保序；消息队列至多一条在途信号。
        call_from_thread 保留给必须同步等结果的模态挂载。"""
        for key, value in fields.items():
            if isinstance(value, str):
                fields[key] = redact_text(value)
            elif isinstance(value, tuple):
                fields[key] = tuple(
                    redact_text(item) if isinstance(item, str) else item
                    for item in value
                )
        event: tuple[str, Any] | None = None
        if "begin_operation" in fields:
            event = ("begin_operation", fields.pop("begin_operation"))
        elif fields.pop("end_operation", False):
            event = ("end_operation", None)
        try:
            self.app.post_ui_update(event=event, **fields)
        except Exception:  # noqa: BLE001 - app 未运行 / 已退出时安静丢弃
            pass

    def close(self):
        self._revision.close()

    # ---------------- 输出类（合并缓冲投递 + 有界日志队列，不阻塞业务流程）----------------
    def show_title(self, title: str, subtitle: str | None = None) -> None:
        self._post(title=(title, subtitle))

    def show_info(self, message: str) -> None:
        if message == _AFK_START_STATUS:
            # 每轮挂课从干净的活动日志开始；同步屏障保证旧缓冲也先清掉，
            # 随后入队的「开始挂课」会成为本轮第一条可见日志。
            try:
                self.app.call_from_thread(self.app.clear_activity_log)
            except Exception:  # noqa: BLE001 - app 未运行 / 已退出时安静忽略
                pass
        self._refresh_dashboard()
        self._post(status=message)
        self.app.enqueue_log(_icon_text(ui_glyphs().icon_info, message, style=GREEN))

    def show_success(self, message: str) -> None:
        self.app.record_operation_feedback(Text(redact_text(message), style=SUCCESS))
        self._refresh_dashboard()
        self._post(status=message)
        self.app.enqueue_log(
            _icon_text(ui_glyphs().icon_success, message, style=SUCCESS)
        )

    def show_warning(self, message: str) -> None:
        self.app.record_operation_feedback(Text(redact_text(message), style=WARNING))
        self._refresh_dashboard()
        self._post(status=message)
        self.app.enqueue_log(
            _icon_text(ui_glyphs().icon_warning, message, style=WARNING)
        )

    def show_error(self, message: str) -> None:
        self.app.record_operation_feedback(Text(redact_text(message), style=ERROR))
        self._refresh_dashboard()
        self._post(status=message)
        self.app.enqueue_log(_icon_text(ui_glyphs().icon_failure, message, style=ERROR))

    def begin_operation(self, title: str, message: str) -> None:
        # 不再弹居中模态：点亮顶部状态条（布局里的一行），让仪表盘/进度条/日志平铺不遮挡。
        self._post(begin_operation=(title, message))

    def prepare_menu_loading(self) -> None:
        # 返回主菜单：长任务结束，收起状态条并清除「操作中」标记。
        # 结果页 held-screen 会保留到主菜单挂载，无空档。
        self._post(end_operation=True)

    def show_summary(self, title: str, rows: list[tuple[str, str]]) -> None:
        summary = tui_render.build_summary(
            redact_text(title), [(redact_text(k), redact_text(v)) for k, v in rows]
        )
        self.app.record_operation_feedback(summary)
        self.app.enqueue_log(summary)

    def render_dashboard(self, state: Any) -> None:
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

    def _dashboard_signature(self, state: Any, metadata: Any) -> tuple:
        return (
            state.has_credential,
            state.credential_expired,
            state.learning_count,
            state.learning_failure_count,
            state.exam_count,
            state.manual_exam_count,
            getattr(metadata, "account_label", None),
            # 到期日也要进签名：同一账号续期后（新旧凭证都有效）只有它变了
            getattr(metadata, "expires_at", None),
        )

    def _push_dashboard(self, state: Any) -> None:
        """按 tui_render 的扁平 KPI 布局刷新仪表盘卡片与品牌栏账号胶囊。

        签名（计数/账号）不变时跳过：挂课期间每条状态消息都会走到这里，
        但「课程 N」一门课完成才变一次，重建 + 投递纯属浪费。"""
        metadata, recommended = self._dashboard_inputs(state)
        signature = self._dashboard_signature(state, metadata)
        if signature == self._last_dashboard_signature:
            return
        self._last_dashboard_signature = signature
        self._post(
            dashboard=(
                tui_render.build_account_chip(state, metadata),
                tui_render.build_stat_tiles(state),
                tui_render.build_dashboard_meta(state, metadata),
                tui_render.build_action_line(recommended),
            )
        )

    def _state_changed(self) -> bool:
        signature = self._revision.signature()
        if signature == self._last_revision_signature:
            return False
        self._last_revision_signature = signature
        return True

    def _refresh_dashboard(self) -> None:
        """Refresh counters only after a committed DB or credential change."""
        if not self._state_changed():
            return
        from core.state import collect_project_state

        self._latest_state = collect_project_state()
        self._push_dashboard(self._latest_state)

    # ---------------- 阻塞提示类（工作线程在 Queue.get 上等待结果）----------------
    def _prompt(self, screen: Any, *, cancellable: bool = False) -> Any:
        from core.abort import UserAbortRequested

        if self.app._shutting_down:
            raise UserAbortRequested("应用已退出")
        # call_from_thread 阻塞工作线程直到模态屏挂载完成并返回 Queue；
        # 随后 Queue.get 阻塞直到用户操作把结果写入队列。
        try:
            queue = self.app.call_from_thread(
                self.app.push_prompt, screen, cancellable=cancellable
            )
        except RuntimeError as exc:
            raise UserAbortRequested("应用已退出") from exc
        result = queue.get()
        if self.app._shutting_down:
            raise UserAbortRequested("应用已退出")
        # Ctrl+C 强制取消（仅 cancellable 提示）→ 抛 UserCancelRequested 返回主菜单
        if result is _PROMPT_CANCELLED:
            raise UserCancelRequested("已取消当前操作，返回主菜单")
        return result

    def show_menu(self, options: list[str]) -> int:
        # 主菜单不可取消：在主菜单按 Ctrl+C 直接退出应用
        status_renderable = None
        compact_status_renderable = None
        if self._latest_state is not None:
            metadata, recommended = self._dashboard_inputs(self._latest_state)
            status_renderable = tui_render.build_menu_status(
                self._latest_state, metadata, recommended
            )
            compact_status_renderable = tui_render.build_menu_status(
                self._latest_state, metadata, recommended, compact=True
            )
        return self._prompt(
            OptionScreen(
                "主菜单",
                options,
                "请选择功能",
                status_renderable=status_renderable,
                main_menu=True,
                compact_status_renderable=compact_status_renderable,
            )
        )

    def prompt_choice(
        self, title: str, options: list[str], prompt: str = "请选择"
    ) -> int:
        return self._prompt(OptionScreen(title, options, prompt), cancellable=True)

    def prompt_yes_no(self, message: str, default: str = "N") -> bool:
        return self._prompt(YesNoScreen(message, default), cancellable=True)

    def prompt_summary_confirmation(
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

    def pause(self, message: str = "按回车返回主菜单") -> None:
        self._prompt(PauseScreen(message), cancellable=True)

    def prepare_pause_with_summary(
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

    def wait_prepared_prompt(self, handle) -> None:
        result = handle.get()
        if self.app._shutting_down:
            from core.abort import UserAbortRequested

            raise UserAbortRequested("应用已退出")
        if result is _PROMPT_CANCELLED:
            raise UserCancelRequested("已取消当前操作，返回主菜单")

    def prompt_multiline_input(
        self,
        messages: list[str],
        *,
        title: str = "手动选择课程 / 录入链接",
        cancel_message: str = "已取消手动选择课程 / 录入链接",
    ) -> str:
        result = self._prompt(MultilineScreen(messages, title), cancellable=True)
        kind, value = result
        if kind == "cancel":
            raise UserCancelRequested(cancel_message)
        return value

    async def wait_with_progress(
        self, duration: int, description: str = "处理中"
    ) -> None:
        """按秒推进进度数字，约 10Hz 刷新 UI，进度与转圈使用独立刻度。

        每个 tick 走合并缓冲投递：不同步等界面一个往返，风暴下自动合并。"""
        duration = int(duration)
        if duration <= 0:
            return
        ticks_per_sec = 10
        total_ticks = duration * ticks_per_sec
        self._post(progress=(description, 0, duration, 0))
        for tick in range(1, total_ticks + 1):
            await asyncio.sleep(1.0 / ticks_per_sec)
            completed = min(duration, tick // ticks_per_sec)
            self._post(progress=(description, completed, duration, tick))
        self._post(clear_progress=True)


# ------------------------------------------------------------------
# 入口
# ------------------------------------------------------------------
def launch_tui() -> int:
    """Own the frontend, worker, engine, signals and temporary log handler."""
    import launcher
    import os
    import signal
    import sys
    from core.config import DATA_DIR
    from core.runtime import application_lock, protect_application_children
    from core.startup import report_fatal_error, shutdown_workflows

    launcher._check_python_version()
    launcher._disable_windows_console_input_modes_early()
    setup_logging()
    app = CourseTuiApp()
    log_handler, previous_handlers = _install_log_handler(app)
    frontend = None
    previous_term = signal.getsignal(signal.SIGTERM)
    try:
        frontend = TuiFrontend(app)
        signal.signal(signal.SIGTERM, lambda *_: app.exit(143))
        with application_lock(DATA_DIR / "application.lock"):
            protect_application_children()
            result = app.run()
            if app._launcher_thread is not None and app._launcher_thread.is_alive():
                report_fatal_error(
                    RuntimeError("工作线程未在退出期限内停止；已提交的数据可在下次恢复")
                )
                logging.shutdown()
                os._exit(1)
    finally:
        primary_failure = sys.exc_info()[0] is not None or app.fatal_error is not None
        try:
            try:
                shutdown_workflows()
            except Exception:
                if not primary_failure:
                    raise
                logging.exception("工作流关闭失败，保留原始错误")
        finally:
            signal.signal(signal.SIGTERM, previous_term)
            if frontend is not None:
                frontend.close()
            root = logging.getLogger()
            root.removeHandler(log_handler)
            log_handler.close()
            for handler in previous_handlers:
                root.addHandler(handler)
    if app.fatal_error is not None:
        report_fatal_error(app.fatal_error)
    return int(app._exit_status or result or 0)
