"""Textual TUI 前端：品牌栏 + KPI 仪表盘卡片 + 活动日志 + 模态提示。

本模块只负责 Textual 界面本身，不依赖 core.* 业务逻辑。桥接层
(core.ui.tui_bridge.TuiFrontend) 负责把 core.ui 的同步接口接到这里。

线程模型：
- Textual 事件循环跑在主线程。
- launcher.main() 整个阻塞循环跑在一条 daemon 工作线程上
  (_spawn_launcher_thread)，浏览器自动化在它内部各自的 run_async 里阻塞，
  不会卡住界面。
- 桥接层与界面的通信分两类：输出类（状态/日志/仪表盘/进度）走合并写缓冲
  + 至多一条在途 UiUpdate 信号消息（fire-and-forget，带背压，见
  post_ui_update / enqueue_log）；阻塞提示类经同步 call_from_thread 挂载
  模态屏后在 Queue.get 上等待结果。已完成的模态屏会保持到下一个模态屏
  挂载时再原位替换，避免切换间隙闪屏。
- 两条通道不共用队列：模态挂载前必须 flush_ui_updates() 冲刷缓冲，
  保证「先显示结果、再弹提示」的顺序（见 push_prompt）。

视觉语言（扁平分层，替代旧版「框中框中框」）：
- 屏幕画布最深(BG_CANVAS)，卡片比画布亮一档(BG_PANEL)，悬浮/状态条再上一档；
  区域靠底色与留白分区，全文只在模态卡片和分节线处出现发丝描边。
- 数值文字穿中性文字色；身份靠标签、语义（过期/失败）靠图标+状态色。
- 选中/聚焦的绿是唯一强调色：菜单光标、主按钮、进度完成段共用。
"""

from __future__ import annotations

import sys
import threading
from collections import deque
from queue import Queue
from typing import Any

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import (
    Button,
    LoadingIndicator,
    OptionList,
    RichLog,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option
from rich.console import Group
from rich.text import Text

from core.menu_keys import (
    ensure_menu_option_count,
    menu_key_for_index,
    menu_keys_hint,
    parse_menu_key,
)
from core.palette import (
    ACCENT_ROW,
    BG_CANVAS,
    BG_PANEL,
    BG_PANEL_RAISED,
    ERROR,
    GREEN,
    GREEN_BRIGHT,
    GREEN_DEEP,
    HAIRLINE,
    ON_ACCENT,
    ON_DANGER,
    SUCCESS,
    TEXT_DIM,
    TEXT_MUTED,
    TEXT_PRIMARY,
    WARNING,
)
from core.ui.terminal_compat import ui_glyphs
from core.ui.tui_render import build_hint_line, build_progress_text


# Esc / Ctrl+C 强制取消当前模态提示时使用；桥接层识别后抛
# UserCancelRequested，让控制流正常返回主菜单（而非直接退出）。
_PROMPT_CANCELLED: Any = object()


class UiUpdate(Message, namespace="course_tui"):
    """「合并写缓冲里有待应用更新」的信号消息（本身不携带数据）。

    后台线程的高频更新先写进 CourseTuiApp 上的合并缓冲（值字段 latest-value
    覆盖、事件字段按全局序号保序），仅在无在途信号时投递一条本消息，UI
    处理时一次性取走缓冲——消息队列里最多一条在途信号，日志/状态风暴
    不会撑大 Textual 消息队列。数据在缓冲里而不在消息里，正是为了
    latest-value 合并与背压。"""


# 待合并的值字段名（latest-value 语义：后写覆盖先写）
_PENDING_VALUE_KINDS = ("title", "dashboard", "status", "progress", "clear_progress")


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

    交互：数字键 1-9/0 直接选中、方向键移动高亮、回车或鼠标点击确认。
    每页最多 10 项；第 10 项键位为 0。提示分隔符按终端 Unicode/ASCII 自适应。
    """

    AUTO_FOCUS = "#opt-list"

    # 数字快捷键优先于 OptionList 的首字母搜索，否则按 1 只会高亮「1. …」不确认。
    BINDINGS = [
        Binding(str(digit), f"pick_{digit}", show=False, priority=True)
        for digit in "1234567890"
    ]

    def __init__(
        self,
        title: str,
        options: list[str],
        prompt: str = "请选择",
        status_renderable: Any | None = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._options = list(options)
        ensure_menu_option_count(len(self._options))
        self._prompt = prompt
        self._status_renderable = status_renderable

    def compose(self) -> ComposeResult:
        g = ui_glyphs()
        escape_hint = ("ESC", "退出" if self._status_renderable is not None else "返回")
        total = len(self._options)
        hint_parts: list[tuple[str, str] | str] = [
            (menu_keys_hint(total), "数字键直选"),
        ]
        if g.name == "unicode":
            hint_parts.append(("↑↓", "移动"))
        else:
            hint_parts.append("方向键移动")
        hint_parts.extend([("Enter", "确认"), escape_hint])
        hint = build_hint_line(hint_parts)

        content = [Static(self._title, id="opt-title")]
        if self._status_renderable is not None:
            content.append(Static(self._status_renderable, id="opt-status"))
        prompt_text = Text(self._prompt, style=TEXT_MUTED)
        prompt_text.append(ui_glyphs().sep_tight, style=TEXT_DIM)
        prompt_text += hint
        content.extend(
            (
                Static(prompt_text, id="opt-hint"),
                OptionList(
                    *(
                        Option(
                            # 序号/标签不写死前景色，只给字重：显式 span 色会盖过
                            # 组件 CSS 的 color，聚焦行翻成「亮绿底深字」时序号
                            # 就成了绿底绿字。留空即继承组件色，随高亮态自动适配。
                            Text.assemble(
                                (menu_key_for_index(idx, total), "bold"),
                                (".", "dim"),
                                (f"  {opt}", ""),
                            ),
                            id=f"opt-{idx}",
                        )
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

    def _pick_digit(self, digit: str) -> None:
        choice = parse_menu_key(digit, len(self._options))
        if choice is not None:
            self.app.resolve_prompt(self, choice)

    def action_pick_1(self) -> None:
        self._pick_digit("1")

    def action_pick_2(self) -> None:
        self._pick_digit("2")

    def action_pick_3(self) -> None:
        self._pick_digit("3")

    def action_pick_4(self) -> None:
        self._pick_digit("4")

    def action_pick_5(self) -> None:
        self._pick_digit("5")

    def action_pick_6(self) -> None:
        self._pick_digit("6")

    def action_pick_7(self) -> None:
        self._pick_digit("7")

    def action_pick_8(self) -> None:
        self._pick_digit("8")

    def action_pick_9(self) -> None:
        self._pick_digit("9")

    def action_pick_0(self) -> None:
        self._pick_digit("0")

    def on_key(self, event: events.Key) -> None:
        # 兜底：部分终端把数字键送到 on_key 而非 Binding。
        char = event.character or ""
        if char in "0123456789":
            choice = parse_menu_key(char, len(self._options))
            if choice is not None:
                event.stop()
                event.prevent_default()
                self.app.resolve_prompt(self, choice)

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
        hint = Text.assemble(
            (f"默认 {self._default}", TEXT_MUTED),
            (_g_sep(), TEXT_DIM),
        )
        hint += build_hint_line([("Y", "是"), ("N", "否"), ("ESC", "返回")])
        content = [Static(self._message, id="yn-msg")]
        if self._details_renderable is not None:
            content.append(Static(self._details_renderable, id="yn-details"))
        content.extend(
            (
                Static(hint, id="yn-hint"),
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


def _g_sep() -> str:
    """当前终端的提示分隔符（短别名）。"""
    return ui_glyphs().sep


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
    ) -> None:
        super().__init__()
        self._messages = messages
        self._title = title

    def compose(self) -> ComposeResult:
        g = ui_glyphs()
        instruction = Text()
        for index, message in enumerate(self._messages, start=1):
            instruction.append(f" {index}. ", style=f"bold {GREEN}")
            instruction.append(f"{message}\n", style=TEXT_PRIMARY)
        yield Vertical(
            Static(self._title, id="ml-title"),
            Static(instruction, id="ml-instr"),
            TextArea(id="ml-text"),
            Static(
                build_hint_line(
                    [
                        ("Enter", "换行"),
                        ("Ctrl+V", "粘贴"),
                        ("Ctrl+Enter", "提交"),
                        ("ESC", "返回"),
                    ]
                ),
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
        hint_action = "确定" if self._button_label.startswith("OK") else "继续"
        content = []
        if self._details_renderable is not None:
            content.append(Static(self._details_renderable, id="pause-details"))
        content.extend(
            (
                Static(self._message, id="pause-msg"),
                Static(
                    build_hint_line(
                        [("Enter", hint_action), ("ESC", "返回")]
                    ),
                    id="pause-hint",
                ),
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


# ---------------------------------------------------------------------------
# 主题：统一为翡翠绿色调。焦点链路由主题派生：菜单光标 = block-cursor-background、
# 列表/文本框聚焦边框 = border、主按钮 = primary。表面层次画布最深、卡片亮一档。
# ---------------------------------------------------------------------------
COURSE_THEME = Theme(
    name="course-green",
    primary=GREEN,          # 主色：选中高亮 / 焦点边框 / 主按钮
    secondary=GREEN_DEEP,   # 深绿：次级层次
    accent=GREEN_BRIGHT,    # 亮绿：品牌标记 / 进度 / 键帽
    warning=WARNING,
    error=ERROR,
    success=SUCCESS,
    foreground=TEXT_PRIMARY,
    background=BG_CANVAS,   # 画布：全局最深
    surface=BG_CANVAS,      # 控件默认底 = 画布，卡片再自己提亮
    panel=BG_PANEL,         # 模态 / 内嵌面板
    dark=True,
    variables={
        # 亮绿底配深色字，保证选中项 / 主按钮 / 输入光标文字的对比度
        "block-cursor-foreground": ON_ACCENT,
        "button-color-foreground": ON_ACCENT,
        "input-cursor-foreground": ON_ACCENT,
        # 聚焦用加粗，不用 reverse：变体按钮(是/否/提交)是亮底深字，
        # reverse 会把它们反转为黑底，看起来像坏掉的光标。
        "button-focus-text-style": "bold",
        # 中性文字与描边与 palette 常量同源
        "text-muted": TEXT_MUTED,
        "border": "#2f4a3e",
        "border-blurred": HAIRLINE,
        # 输入区选区：低饱和绿
        "input-selection-background": "#1d4c3c",
    },
)


# 边框样式按终端在 import 时选定（round 在纯 cmd 易错位 → ascii/solid）
_UI = ui_glyphs()
_BORDER = _UI.textual_border
_KEYS_JOIN = _UI.keys_join
_BRAND_MARK = "◆" if _UI.name == "unicode" else "*"


class CourseTuiApp(App):
    """中国电信网上大学自动化工具的 Textual 主应用。"""

    CSS = f"""
    Screen {{
        background: {BG_CANVAS};
    }}

    /* ---------- 品牌栏：替代默认 Header 的常驻顶栏 ----------
       注意 height:1 的条上不能加 border：边框会吃掉唯一的内容行。 */
    #brand-bar {{
        height: 1;
        width: 100%;
        background: {BG_PANEL};
    }}

    #brand-mark {{
        width: auto;
        height: 1;
        padding: 0 0 0 1;
        color: {GREEN_BRIGHT};
        text-style: bold;
    }}

    #brand-title {{
        width: auto;
        height: 1;
        padding: 0 1 0 0;
        color: {TEXT_PRIMARY};
        text-style: bold;
    }}

    #brand-sub {{
        width: 1fr;
        height: 1;
        padding: 0 1 0 0;
        color: {TEXT_MUTED};
        overflow: hidden;
    }}

    #brand-account {{
        width: auto;
        /* 兜底上限：账号名在渲染层已按显示宽度截断，这里防止极端情况撑爆品牌栏 */
        max-width: 38;
        height: 1;
        padding: 0 1;
    }}

    #main {{
        /* 常驻外壳：始终可见。菜单 / 确认 / 结果页是叠在上面的不透明模态，会遮住它；
           长任务（挂课/考试）期间不弹模态，于是仪表盘 / 进度条 / 活动日志 + 状态条
           一起作为布局里的若干行平铺显示，互不遮挡。 */
        layout: vertical;
    }}

    /* 操作状态条：长任务期间「当前在做什么」常驻顶部一行（品牌栏下方）。
       空闲时 display:none 不占位；set_operation_status 时加 .active 显示。 */
    #status-bar {{
        height: 1;
        margin: 0;
        padding: 0 1;
        background: {BG_PANEL_RAISED};
        display: none;
    }}

    #status-bar.active {{
        display: block;
    }}

    #status-spinner {{
        height: 1;
        width: 2;
        margin: 0 1 0 0;
        color: {GREEN_BRIGHT};
    }}

    #status-text {{
        height: 1;
        width: 1fr;
        color: {TEXT_PRIMARY};
    }}

    /* 快捷键常驻状态条右侧（顶部、醒目），不再埋在左下角 Footer。
       width 必须 auto：Static 默认铺满整行，会把 1fr 的状态文本挤到 1 列宽。 */
    #status-keys {{
        width: auto;
        height: 1;
        color: {TEXT_MUTED};
    }}

    /* ---------- 仪表盘卡片：KPI 磁贴 + 账号 meta + 建议操作 ---------- */
    #dashboard {{
        height: auto;
        margin: 1 1 0 1;
        padding: 1 1;
        background: {BG_PANEL};
        display: none;
    }}

    #dashboard.ready {{
        display: block;
    }}

    #dash-stats {{
        height: auto;
    }}

    #dash-meta {{
        height: auto;
        margin-top: 1;
    }}

    #dash-action {{
        height: auto;
    }}

    /* 挂课进度：双行（摘要 + 进度轨），未点亮时不占位 */
    #progress {{
        height: auto;
        margin: 0 1;
        padding: 0 1;
        display: none;
    }}

    #progress.live {{
        display: block;
        margin: 1 1 0 1;
    }}

    /* ---------- 活动日志：无框，弱化小标题分节 ---------- */
    #log-caption {{
        height: 1;
        margin: 1 1 0 1;
        padding: 0 1;
        color: {TEXT_DIM};
    }}

    #log {{
        margin: 0 1 1 1;
        padding: 0 1;
        height: 1fr;
        background: transparent;
        border: none;
    }}

    /* ---------- 模态屏 ---------- */
    OptionScreen, YesNoScreen, MultilineScreen, PauseScreen {{
        align: center middle;
    }}

    #opt-dialog, #yn-dialog, #ml-dialog, #pause-dialog {{
        width: 84;
        height: auto;
        max-width: 94%;
        max-height: 96%;
        border: {_BORDER} {HAIRLINE};
        background: {BG_PANEL};
        padding: 1 2;
    }}

    #opt-title, #ml-title, #yn-msg {{
        text-style: bold;
        color: {TEXT_PRIMARY};
        border-bottom: solid {HAIRLINE};
        margin-bottom: 1;
    }}

    #opt-hint, #yn-hint, #ml-hint, #pause-msg, #pause-hint {{
        color: {TEXT_MUTED};
        margin-bottom: 1;
    }}

    #ml-instr {{
        height: auto;
        /* 上限 5：60x20 矮终端下给输入框让出一行（说明本身可滚动） */
        max-height: 5;
        overflow-y: auto;
        margin-bottom: 1;
    }}

    #opt-status {{
        height: auto;
        margin-bottom: 1;
        padding: 0 1;
        background: {BG_PANEL_RAISED};
    }}

    #yn-details, #pause-details {{
        height: 1fr;
        min-height: 3;
        overflow-y: auto;
        margin-bottom: 1;
        padding: 0 1;
        background: {BG_CANVAS};
    }}

    YesNoScreen.with-details #yn-dialog,
    PauseScreen.with-details #pause-dialog {{
        height: 90%;
        max-height: 34;
    }}

    #opt-list {{
        /* 1fr 让列表在窄/矮终端收缩（自带滚动条），而不是把对话框顶出屏幕；
           宽裕时 1fr 在 auto 对话框里按内容高度展开，不受影响。 */
        height: 1fr;
        min-height: 3;
        max-height: 15;
        margin-bottom: 1;
        padding: 0;
        background: transparent;
        border: none;
    }}

    #opt-list > .option-list--option {{
        padding: 0 1;
    }}

    #opt-list > .option-list--option-hover {{
        background: {ACCENT_ROW};
    }}

    #opt-list > .option-list--option-highlighted {{
        background: {ACCENT_ROW};
        color: {TEXT_PRIMARY};
        text-style: bold;
    }}

    #opt-list:focus > .option-list--option-highlighted {{
        background: {GREEN};
        color: {ON_ACCENT};
    }}

    #opt-list:focus {{
        background-tint: transparent;
    }}

    #ml-dialog {{
        /* 不设 min-height：矮终端下按比例收缩，输入区 1fr 跟着缩，按钮不溢出；
           94% 保证 90x22 这类常规尺寸下输入区至少 3 行（90% 会少一行）。 */
        height: 94%;
        max-height: 36;
    }}

    #ml-text {{
        /* 圆角 1 行描边而不是 tall（tall 上下各吃 2 行）；min-height 3 是硬底线：
        边框占 2 行，再少一行可编辑内容都没有。margin-bottom 0 + 说明区上限 5
        已为 60x20 腾出空间，常规尺寸下 1fr 自然分到更多。 */
        height: 1fr;
        min-height: 3;
        margin-bottom: 0;
        border: {_BORDER} {HAIRLINE};
        background: {BG_CANVAS};
    }}

    #ml-text:focus {{
        border: {_BORDER} {GREEN};
    }}

    /* 标题块（文字+下划线）与内容之间不留空行：标题自身就是分节线 */
    #ml-title {{
        margin-bottom: 0;
    }}

    #yn-actions, #ml-actions, #pause-actions {{
        align-horizontal: center;
        height: auto;
        margin-top: 0;
    }}

    /* ---------- 扁平按钮：高 1 行、无描边、聚焦翻色 ---------- */
    Button {{
        height: 1;
        min-width: 8;
        margin: 0 1;
        padding: 0 2;
        border: none;
        background: {BG_PANEL_RAISED};
        color: {TEXT_PRIMARY};
    }}

    Button:hover, Button:focus {{
        background: {GREEN};
        color: {ON_ACCENT};
        text-style: bold;
    }}

    /* 肯定类按钮（是 / 提交 / 继续）默认即主绿实底 */
    Button.-primary, Button.-success {{
        background: {GREEN};
        color: {ON_ACCENT};
    }}

    /* 否定类按钮（否）：中性底 + 状态红字，聚焦才红底（深红字，勿用绿系深字） */
    Button.-error {{
        background: {BG_PANEL_RAISED};
        color: {ERROR};
    }}

    Button.-error:hover, Button.-error:focus {{
        background: {ERROR};
        color: {ON_DANGER};
    }}
    """

    TITLE = "中国电信网上大学自动化工具"
    SUB_TITLE = f"登录{_KEYS_JOIN}学习{_KEYS_JOIN}考试  统一入口"

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
        # 是否处于长任务中（begin_operation 起、返回主菜单止）。仅在此期间，
        # show_info/show_success 这类状态更新才会退掉 held 的子提示——避免「退出」
        # 这种独立 show_success 误伤 held 主菜单。
        self._operation_active: bool = False
        # ---------- 后台线程 → UI 的合并写缓冲（背压核心） ----------
        # 值字段（title/dashboard/status/progress/clear_progress）后写覆盖；
        # 事件字段（begin_operation/end_operation）按全局序号保序；两类按
        # 序号归并后整体应用，等价于逐条顺序执行。信号消息在途时不再投递，
        # 队列里至多一条在途 UiUpdate。
        self._ui_lock = threading.Lock()
        self._ui_seq = 0
        self._ui_values: dict[str, tuple[int, Any]] = {}
        self._ui_events: list[tuple[int, str, Any]] = []
        self._ui_in_flight = False
        # 活动日志的有界缓冲：高频日志先入队，UI 定时批量写入 RichLog；
        # 满了丢最旧并计数，UI 侧补一条「已丢弃 N 条」提示。
        self._log_buffer: deque = deque(maxlen=512)
        self._log_dropped = 0

    def compose(self) -> ComposeResult:
        g = ui_glyphs()
        yield Horizontal(
            Static(f"{_BRAND_MARK} ", id="brand-mark"),
            Static(self.title, id="brand-title"),
            Static(self.sub_title, id="brand-sub"),
            Static(id="brand-account"),
            id="brand-bar",
        )
        yield Vertical(
            Horizontal(
                LoadingIndicator(id="status-spinner"),
                Static(id="status-text"),
                Static(
                    build_hint_line([("ESC", "取消"), ("Ctrl+C", "退出")]),
                    id="status-keys",
                ),
                id="status-bar",
            ),
            Vertical(
                Static(id="dash-stats"),
                Static(id="dash-meta"),
                Static(id="dash-action"),
                id="dashboard",
            ),
            Static(id="progress"),
            Static(f"{g.bullet} 活动日志", id="log-caption"),
            RichLog(id="log", markup=True, auto_scroll=True, max_lines=1500),
            id="main",
        )

    def on_mount(self) -> None:
        self.register_theme(COURSE_THEME)
        self.theme = COURSE_THEME.name
        # 20Hz 批量落盘日志缓冲：日志风暴时 RichLog 的写入次数与日志条数解耦
        self.set_interval(0.05, self._drain_log_buffer)
        self._spawn_launcher_thread()

    # ------------------------------------------------------------------
    # 后台线程 → UI：合并写缓冲 + 有界日志缓冲（线程安全，背压核心）
    # ------------------------------------------------------------------
    def post_ui_update(
        self,
        *,
        event: tuple[str, Any] | None = None,
        **values: Any,
    ) -> None:
        """线程安全的 UI 更新入口：写缓冲 + 至多一条在途信号消息。

        值字段（title/dashboard/status/progress/clear_progress）latest-value
        合并；event 传 ("begin_operation"/"end_operation", 载荷) 保序。"""
        with self._ui_lock:
            self._ui_seq += 1
            seq = self._ui_seq
            if event is not None:
                self._ui_events.append((seq, f"event:{event[0]}", event[1]))
            for key, value in values.items():
                if key not in _PENDING_VALUE_KINDS:
                    raise ValueError(f"未知的 UI 更新字段: {key}")
                self._ui_values[key] = (seq, value)
            if self._ui_in_flight:
                return
            self._ui_in_flight = True
        try:
            self.post_message(UiUpdate())
        except Exception:  # noqa: BLE001 - app 未运行 / 已退出
            with self._ui_lock:
                self._ui_in_flight = False

    def enqueue_log(self, renderable: Any) -> None:
        """日志入有界缓冲（满了丢最旧并计数），UI 定时批量写 RichLog。"""
        with self._ui_lock:
            if len(self._log_buffer) == self._log_buffer.maxlen:
                self._log_dropped += 1
            self._log_buffer.append(renderable)

    def on_course_tui_ui_update(self, event: UiUpdate) -> None:
        """信号到达：取走缓冲中的全部待应用更新，按序号归并执行。

        in_flight 只能在这里清除：flush_ui_updates 只取数据不动标志——
        已进 Textual 队列的旧信号无法取消，flush 若清标志，新更新会再投
        一个信号，队列里就可能同时压两个。旧信号迟到时取到空批，无害。"""
        with self._ui_lock:
            self._ui_in_flight = False
        self._apply_pending_ui_updates()

    def _take_pending(self) -> list[tuple[int, str, Any]]:
        with self._ui_lock:
            pending = [
                (seq, f"value:{kind}", value)
                for kind, (seq, value) in self._ui_values.items()
            ] + list(self._ui_events)
            self._ui_values.clear()
            self._ui_events.clear()
        pending.sort(key=lambda item: item[0])
        return pending

    def _apply_pending_ui_updates(self) -> None:
        for _seq, kind, value in self._take_pending():
            if kind == "value:title":
                self.set_title(value[0], value[1])
            elif kind == "value:dashboard":
                self.update_dashboard(*value)
            elif kind == "value:status":
                self.set_busy_status(value)
            elif kind == "value:progress":
                self.set_progress(*value)
            elif kind == "value:clear_progress":
                self.clear_progress()
            elif kind == "event:begin_operation":
                self.set_operation_status(*value)
            elif kind == "event:end_operation":
                self.end_operation()

    def _drain_log_buffer(self) -> None:
        with self._ui_lock:
            entries = list(self._log_buffer)
            self._log_buffer.clear()
            dropped = self._log_dropped
            self._log_dropped = 0
        if not entries and not dropped:
            # 20Hz 定时器空闲时直接返回，避免每秒做 20 次 selector 查询。
            return
        try:
            log = self.query_one("#log", RichLog)
        except Exception:  # noqa: BLE001 - 尚未挂载
            return
        if dropped:
            entries.append(
                Text(f"⋯ 高频日志过多，已丢弃 {dropped} 条", style=f"dim {TEXT_DIM}")
            )
        # 整批合成一次 write：RichLog.write 每次都要测量/渲染/更新虚拟高度
        # 与滚动状态，风暴下逐条写会把一个 UI tick 撑爆
        log.write(entries[0] if len(entries) == 1 else Group(*entries))

    def flush_ui_updates(self) -> None:
        """同步屏障：立刻应用全部待处理更新与日志（app 线程上执行）。

        模态挂载前 / 退出前调用，保证「先显示结果、再弹提示/退出」的顺序
        不被 fire-and-forget 打乱。注意不清 in_flight：见
        on_course_tui_ui_update 的说明。"""
        self._apply_pending_ui_updates()
        self._drain_log_buffer()

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
            # 退出前同步屏障：最后的「已退出」等日志/状态先落屏再退出，
            # 不被 fire-and-forget 的异步时序吞掉
            self.call_from_thread(self.flush_ui_updates)
        except Exception:
            pass
        try:
            self.call_from_thread(self.exit)
        except Exception:
            pass

    def _safe_emit_error(self, message: str) -> None:
        try:
            from rich.text import Text

            g = ui_glyphs()
            self.call_from_thread(
                self.emit_log,
                Text(
                    f"  {g.pad_icon(g.icon_failure)}  {message}",
                    style=f"bold {ERROR}",
                ),
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 桥接层回调（在 app 线程上执行：多经 UiUpdate 信号 / 日志定时器触发，
    # 少数错误路径仍走 call_from_thread）
    # ------------------------------------------------------------------
    def emit_log(self, renderable: Any) -> None:
        self.query_one("#log", RichLog).write(renderable)

    def clear_activity_log(self) -> None:
        """清空活动日志显示与尚未落屏的缓冲，不影响磁盘日志文件。"""
        with self._ui_lock:
            self._log_buffer.clear()
            self._log_dropped = 0
        self.query_one("#log", RichLog).clear()

    def update_dashboard(
        self,
        account_chip: Any,
        stat_tiles: Any,
        meta_line: Any,
        action_line: Any,
    ) -> None:
        """写入仪表盘卡片三段 + 品牌栏账号胶囊（tui_render 产出的 Rich 对象）。"""
        self.query_one("#brand-account", Static).update(account_chip)
        self.query_one("#dash-stats", Static).update(stat_tiles)
        self.query_one("#dash-meta", Static).update(meta_line)
        self.query_one("#dash-action", Static).update(action_line)
        self.query_one("#dashboard").add_class("ready")

    def set_main_content_visible(self, visible: bool) -> None:
        main_content = self.query_one("#main", Vertical)
        main_content.styles.visibility = "visible" if visible else "hidden"

    def set_operation_status(self, title: str, message: str) -> None:
        """长任务开始：先退掉残留的 held 模态（如刚选完的主菜单 / 结果页），再点亮
        顶部状态条。held 模态原本要等「下一个模态挂载」才会被原位替换；而长任务现在
        不再弹模态，若不主动退掉，它会一直盖住 #main（状态条 / 仪表盘 / 日志全看不到）。"""
        self._operation_active = True
        self._dismiss_held_modal()
        label = f"{title} — {message}" if title and message else (title or message)
        self.query_one("#status-text", Static).update(label)
        self.query_one("#status-bar").add_class("active")

    def _dismiss_held_modal(self) -> None:
        """退掉 held 模态（菜单 / 结果页），露出常驻 #main 外壳。"""
        held = self._held_prompt_screen
        if held is None:
            return
        self._held_prompt_screen = None
        if self.screen is held:
            self.pop_screen()

    def set_busy_status(self, message: str) -> None:
        """长任务期间持续刷新状态条文本（show_info / success / ... 经此更新当前进度）。
        仅在「操作中」才顺手退掉 held 模态：推荐流程里 ask_auto_submit 这类「操作中途的
        是/否」答完后，若不主动退掉，held 提示会一直盖住 #main。独立消息（如「退出」的
        show_success）不在操作中，不动 held，避免误伤主菜单的 held 帧。"""
        if self._operation_active:
            self._dismiss_held_modal()
        self.query_one("#status-text", Static).update(message)
        self.query_one("#status-bar").add_class("active")

    def clear_operation_status(self) -> None:
        """弹出模态（菜单 / 子提示 / 结果页）时收起状态条显示（不动「操作中」标记：
        子提示并不结束长任务）。"""
        self.query_one("#status-bar").remove_class("active")
        self.query_one("#status-text", Static).update("")

    def end_operation(self) -> None:
        """返回主菜单：长任务真正结束，收起状态条并清除「操作中」标记。"""
        self._operation_active = False
        self.clear_operation_status()

    def set_title(self, title: str, subtitle: str | None) -> None:
        if title:
            self.title = title
        self.sub_title = subtitle or ""
        self.query_one("#brand-title", Static).update(self.title)
        self.query_one("#brand-sub", Static).update(self.sub_title)

    def set_progress(
        self,
        description: str,
        completed: int,
        total: int,
        spin_frame: int | None = None,
    ) -> None:
        if total <= 0:
            return
        try:
            available = self.query_one("#progress", Static).container_size.width
        except Exception:  # noqa: BLE001 - 布局未就绪时用默认宽
            available = 0
        # 第二行固定开销：3 空格缩进 + 括号 + 尾标，约 8 列；轨道在窄终端缩短不折行
        bar_width = 30 if available <= 0 else max(10, min(30, available - 8))
        self.query_one("#progress", Static).update(
            build_progress_text(
                description,
                completed,
                total,
                spin_frame=spin_frame,
                bar_width=bar_width,
            )
        )
        self.query_one("#progress").add_class("live")

    def clear_progress(self) -> None:
        self.query_one("#progress", Static).update("")
        self.query_one("#progress").remove_class("live")

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
        # 同步屏障：模态经 call_from_thread 挂载，会越过还在消息队列里排队的
        # UiUpdate 信号；不先冲刷缓冲，迟到的 status 快照会在模态挂载后点亮
        # 状态条（旧时序下 show_info 先于 pause 完成故无此问题）。
        self.flush_ui_updates()
        # 挂载任何模态（菜单 / 确认 / 结果）前收起操作状态条：模态会遮住 #main，
        # 状态条无须再显示；下一次 begin_operation 会重新点亮。
        self.clear_operation_status()
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
