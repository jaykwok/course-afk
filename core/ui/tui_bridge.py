"""把 core.ui 的同步接口桥接到 Textual TUI。

核心思路：launcher.main() 及其下游 controller/workflow 一行都不改。
本模块在 TUI 启动时把 core.ui 模块上的公开函数替换成桥接方法，于是
launcher 解析到的 ui.show_menu / controller 传入的 status_callback=ui.show_info
等都自动走到 TUI。唯一一处直接 import 的 core.ui 函数
(learning_common.py 的 wait_with_progress) 是懒导入，patch 后同样生效。

- 输出类 (show_info / show_success / ...)：控制类更新经 app.post_ui_update
  写入合并缓冲（latest-value / 保序、至多一条在途信号消息），日志经
  app.enqueue_log 入有界缓冲由界面定时批量落盘——工作线程全程不阻塞。
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

import core.ui as cli_ui
from core.abort import UserCancelRequested
from core.config import LOG_FORMAT, _get_console_log_level, setup_logging
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
    text.append(message, style=style)
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
        except Exception:  # noqa: BLE001 - app 未运行 / 已退出时安静丢弃
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
        # 仪表盘去重签名：计数/账号不变就跳过重建与投递（挂课期间每条状态
        # 消息都会触发 _refresh_dashboard，但计数一门课才变一次）。
        self._last_dashboard_signature: tuple | None = None
        # 队列/凭证文件的最近 mtime 快照：_refresh_dashboard 的读盘短路依据
        self._last_queue_file_stats: tuple | None = None

    def _post(self, **fields: Any) -> None:
        """把控制类 UI 更新写入合并缓冲并投递信号（fire-and-forget，带背压）。

        值字段（title/dashboard/status/progress）latest-value 合并、事件字段
        （begin_operation/end_operation）保序；消息队列至多一条在途信号。
        call_from_thread 保留给必须同步等结果的模态挂载。"""
        event: tuple[str, Any] | None = None
        if "begin_operation" in fields:
            event = ("begin_operation", fields.pop("begin_operation"))
        elif fields.pop("end_operation", False):
            event = ("end_operation", None)
        try:
            self.app.post_ui_update(event=event, **fields)
        except Exception:  # noqa: BLE001 - app 未运行 / 已退出时安静丢弃
            pass

    def install(self) -> None:
        for attr_name, method_name in self._PATCH_MAP.items():
            self._originals[attr_name] = getattr(cli_ui, attr_name, None)
            setattr(cli_ui, attr_name, getattr(self, method_name))

    def restore(self) -> None:
        for attr_name, original in self._originals.items():
            if original is not None:
                setattr(cli_ui, attr_name, original)
        self._originals.clear()

    # ---------------- 输出类（合并缓冲投递 + 有界日志队列，不阻塞业务流程）----------------
    def _bridge_show_title(self, title: str, subtitle: str | None = None) -> None:
        self._post(title=(title, subtitle))

    def _bridge_show_info(self, message: str) -> None:
        if message == _AFK_START_STATUS:
            # 每轮挂课从干净的活动日志开始；同步屏障保证旧缓冲也先清掉，
            # 随后入队的「开始挂课」会成为本轮第一条可见日志。
            try:
                self.app.call_from_thread(self.app.clear_activity_log)
            except Exception:  # noqa: BLE001 - app 未运行 / 已退出时安静忽略
                pass
        self._refresh_dashboard()
        self._post(status=message)
        self.app.enqueue_log(
            _icon_text(ui_glyphs().icon_info, message, style=GREEN)
        )

    def _bridge_show_success(self, message: str) -> None:
        self._refresh_dashboard()
        self._post(status=message)
        self.app.enqueue_log(
            _icon_text(ui_glyphs().icon_success, message, style=SUCCESS)
        )

    def _bridge_show_warning(self, message: str) -> None:
        self._refresh_dashboard()
        self._post(status=message)
        self.app.enqueue_log(
            _icon_text(ui_glyphs().icon_warning, message, style=WARNING)
        )

    def _bridge_show_error(self, message: str) -> None:
        self._refresh_dashboard()
        self._post(status=message)
        self.app.enqueue_log(
            _icon_text(ui_glyphs().icon_failure, message, style=ERROR)
        )

    def _bridge_begin_operation(self, title: str, message: str) -> None:
        # 不再弹居中模态：点亮顶部状态条（布局里的一行），让仪表盘/进度条/日志平铺不遮挡。
        self._post(begin_operation=(title, message))

    def _bridge_prepare_menu_loading(self) -> None:
        # 返回主菜单：长任务结束，收起状态条并清除「操作中」标记。
        # 结果页 held-screen 会保留到主菜单挂载，无空档。
        self._post(end_operation=True)

    def _bridge_show_summary(self, title: str, rows: list[tuple[str, str]]) -> None:
        self.app.enqueue_log(tui_render.build_summary(title, rows))

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

    def _queue_files_changed(self) -> bool:
        """用 5 个 stat（~0.05ms）判断队列/凭证文件是否有变动，避免每条状态
        消息都做 ~1.2ms 的 JSON 重读——挂课期间计数一门课完成才变一次。"""
        from core.config import (
            CREDENTIAL_META_FILE,
            EXAM_URLS_FILE,
            LEARNING_FAILURES_FILE,
            LEARNING_URLS_FILE,
            MANUAL_EXAM_FILE,
        )

        stats = tuple(
            path.stat().st_mtime_ns if path.exists() else None
            for path in (
                LEARNING_URLS_FILE,
                LEARNING_FAILURES_FILE,
                EXAM_URLS_FILE,
                MANUAL_EXAM_FILE,
                CREDENTIAL_META_FILE,
            )
        )
        if stats == self._last_queue_file_stats:
            return False
        self._last_queue_file_stats = stats
        return True

    def _refresh_dashboard(self) -> None:
        """重读队列文件刷新仪表盘数字。挂课/考试期间每条状态消息后调用一次：
        队列文件随每门课完成而更新，于是「课程 N」会跟着实时递减（24→23→…→0）。
        先用 mtime 短路：文件没动就跳过 JSON 重读与后续重建/投递。"""
        if not self._queue_files_changed():
            return
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
        """按秒推进进度数字，约 10Hz 刷新 UI，让转圈与 CLI Rich 一样顺滑。

        每个 tick 走合并缓冲投递：不同步等界面一个往返，风暴下自动合并。"""
        duration = int(duration)
        if duration <= 0:
            return
        # 与 CLI wait_with_progress(refresh_per_second=10) 对齐
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
