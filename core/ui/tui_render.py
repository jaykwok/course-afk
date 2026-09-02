"""TUI 专用 Rich 渲染层：KPI 磁贴、账号胶囊、键帽提示、进度条。

与 core.ui 里 CLI 共用的 builder 刻意分开：CLI 讲究居中表格 + 边框，
TUI 讲究扁平卡片 + 留白 + 分层底色，两套审美各自演化、互不迁就；
数据仍取自同一 ProjectState / credential 元数据，不会漂移。

配色遵循 core.palette：
- 数值文字用中性文字色（TEXT_PRIMARY），身份靠标签、语义靠图标+状态色；
- 状态色（过期/失败）必须图标与文字同行，不允许只靠颜色；
- 进度轨未完成段是主色同族的更深一档（TRACK），不是灰色。
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Iterable

from rich.cells import cell_len
from rich.text import Text

from core.palette import (
    ERROR,
    GREEN,
    GREEN_BRIGHT,
    HAIRLINE,
    SUCCESS,
    TEXT_DIM,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TRACK,
    WARNING,
)
from core.state import ProjectState
from core.ui.terminal_compat import progress_charset, ui_glyphs


def _g():
    """当前终端 UI 字形（短别名）。"""
    return ui_glyphs()


def _pad_to(text: Text, width: int) -> Text:
    """把 Text 用空格补齐到指定显示宽度（CJK 感知）。"""
    pad = width - cell_len(text.plain)
    if pad > 0:
        text.append(" " * pad)
    return text


def _tile_gap(g) -> Text:
    """磁贴列之间的分隔：Unicode 细竖线 / ASCII 竖线，弱化色。"""
    bar = "│" if g.name == "unicode" else "|"
    return Text(f" {bar} ", style=TEXT_DIM)


# ------------------------------------------------------------------
# 账号 / 有效期（与 CLI 的 core.ui._credential_display 同源数据）
# ------------------------------------------------------------------
def validity_parts(state: ProjectState, metadata: Any) -> tuple[Text, str]:
    """账号有效期 → (带图标文本, 语义色)。语义色恒伴随图标，不只靠颜色。"""
    g = _g()
    if not state.has_credential:
        return Text(f"{g.icon_failure} 未登录", style=f"bold {ERROR}"), "error"
    if state.credential_expired:
        return Text(f"{g.icon_warning} 已过期", style=f"bold {WARNING}"), "warning"
    if metadata and getattr(metadata, "expires_at", None):
        try:
            expires_dt = datetime.fromisoformat(metadata.expires_at)
            now = datetime.now()
            if now.date() >= expires_dt.date():
                return (
                    Text(f"{g.icon_warning} 已过期", style=f"bold {WARNING}"),
                    "warning",
                )
            days_left = max(
                1, math.ceil((expires_dt - now).total_seconds() / 86400)
            )
            text = Text(f"{g.icon_success} 有效至 {expires_dt:%Y-%m-%d}", style=f"bold {SUCCESS}")
            text.append(f"（还有 {days_left} 天）", style=TEXT_DIM)
            return text, "ok"
        except ValueError:
            pass
    return Text(f"{g.icon_success} 有效", style=f"bold {SUCCESS}"), "ok"


def _severity_color(severity: str) -> str:
    return {"ok": SUCCESS, "warning": WARNING, "error": ERROR}.get(severity, SUCCESS)


# 账号胶囊里账号名的显示宽度上限：品牌栏左侧标题已占 ~28 列，60 列窄终端下
# 60-28-状态字 ≈ 这个量级；更长则按显示宽度截断（CJK 感知）加省略号。
_CHIP_LABEL_MAX_CELLS = 20


def _truncate_cells(text: str, max_cells: int) -> str:
    """按显示宽度截断（CJK 算 2 列），超出部分换成省略号。"""
    if cell_len(text) <= max_cells:
        return text
    kept: list[str] = []
    width = 0
    budget = max_cells - 1  # 给省略号留 1 列
    for ch in text:
        ch_width = cell_len(ch)
        if width + ch_width > budget:
            break
        kept.append(ch)
        width += ch_width
    return "".join(kept) + "…"


def build_account_chip(state: ProjectState, metadata: Any) -> Text:
    """品牌栏右侧账号胶囊：状态点 + 账号名 + 状态字。

    健康度不能只靠状态点颜色（色觉障碍不可分辨），必须伴随文字：
    有效 / 已过期 / 未登录。账号名超宽时截断，状态字永远完整可见。
    """
    dot = "●" if _g().name == "unicode" else "*"
    if not state.has_credential:
        return Text.assemble(
            (f"{dot} ", f"bold {ERROR}"), ("未登录", TEXT_MUTED)
        )
    label = getattr(metadata, "account_label", None) or "已登录"
    validity, severity = validity_parts(state, metadata)
    chip = Text()
    chip.append(f"{dot} ", style=f"bold {_severity_color(severity)}")
    chip.append(
        _truncate_cells(label, _CHIP_LABEL_MAX_CELLS), style=f"bold {TEXT_PRIMARY}"
    )
    # 状态字（详情在仪表盘 meta 行）：warning 也必须有文字，不只靠黄点
    status_text = "已过期" if severity == "warning" else "有效"
    chip.append(f"{_g().sep_tight}{status_text}", style=TEXT_DIM)
    return chip


# ------------------------------------------------------------------
# KPI 磁贴（仪表盘卡片 / 主菜单状态区共用）
# ------------------------------------------------------------------
def _tile_specs(state: ProjectState) -> list[tuple[str, Text]]:
    """(标签, 数值文本)。数值用中性亮字；仅「挂课失败>0」按状态色着色并带图标。"""
    g = _g()
    tiles: list[tuple[str, Text]] = []
    for label, count in (
        ("课程", state.learning_count),
        ("挂课失败", state.learning_failure_count),
        ("考试", state.exam_count),
        ("人工考试", state.manual_exam_count),
    ):
        if label == "挂课失败" and count > 0:
            value = Text(
                f"{count} {g.icon_warning}", style=f"bold {WARNING}"
            )
        elif count > 0:
            value = Text(str(count), style=f"bold {TEXT_PRIMARY}")
        else:
            value = Text("0", style=TEXT_DIM)
        tiles.append((label, value))
    return tiles


def build_stat_tiles(state: ProjectState) -> Text:
    """双行 KPI 磁贴行：上标签（弱化）下数值（亮），列间细竖线分隔。"""
    g = _g()
    specs = _tile_specs(state)
    widths = [
        max(cell_len(label), cell_len(value.plain)) + 1
        for label, value in specs
    ]
    label_row = Text()
    value_row = Text()
    for index, ((label, value), width) in enumerate(zip(specs, widths)):
        if index:
            label_row += _tile_gap(g)
            value_row += _tile_gap(g)
        label_row += _pad_to(Text(label, style=TEXT_MUTED), width)
        value_row += _pad_to(value, width)
    label_row.append("\n")
    label_row += value_row
    return label_row


def build_stat_tiles_compact(state: ProjectState) -> Text:
    """单行紧凑磁贴（主菜单状态区用）：标签 弱化 + 数值 亮，中点分隔。"""
    g = _g()
    line = Text()
    for index, (label, value) in enumerate(_tile_specs(state)):
        if index:
            line.append(g.sep_tight, style=TEXT_DIM)
        line.append(f"{label} ", style=TEXT_MUTED)
        line += value
    return line


def build_dashboard_meta(state: ProjectState, metadata: Any) -> Text:
    """仪表盘 meta 行：账号 + 有效期（状态色恒带图标）。"""
    label = getattr(metadata, "account_label", None) or "未登录"
    validity, _severity = validity_parts(state, metadata)
    line = Text()
    line.append("账号 ", style=TEXT_MUTED)
    line.append(label, style=f"bold {TEXT_PRIMARY}")
    line.append(_g().sep, style=TEXT_DIM)
    line += validity
    return line


def build_action_line(recommended: str) -> Text:
    """建议操作行：箭头 + 弱化标签 + 亮黄建议（当前唯一强调色）。"""
    g = _g()
    return Text.assemble(
        (f"{g.arrow} ", f"bold {GREEN_BRIGHT}"),
        ("建议操作  ", TEXT_MUTED),
        (recommended, f"bold {WARNING}"),
    )


def build_menu_status(state: ProjectState, metadata: Any, recommended: str) -> Text:
    """主菜单状态区三行：账号 meta / 紧凑磁贴 / 建议操作。"""
    lines = [
        build_dashboard_meta(state, metadata),
        build_stat_tiles_compact(state),
        build_action_line(recommended),
    ]
    return Text.join(Text("\n"), lines)


def build_summary(title: str, rows: list[tuple[str, str]]) -> Text:
    """TUI 结果汇总：无框两列（标签弱化 / 值亮），标题 + 发丝分节线。

    取代 CLI 的 SIMPLE_HEAVY 表格边框——模态卡片本身就是容器，内层再画框
    就回到旧版「框中框」。
    """
    key_width = max((cell_len(key) for key, _ in rows), default=0)
    content_width = max(
        [key_width + 2 + cell_len(value) for _, value in rows]
        + [cell_len(title)],
        default=0,
    )
    rule_width = max(16, min(60, content_width))
    rule = "─" if _g().name == "unicode" else "-"
    text = Text(title, style=f"bold {TEXT_PRIMARY}")
    text.append(f"\n{rule * rule_width}\n", style=HAIRLINE)
    for index, (key, value) in enumerate(rows):
        text.append(" ")
        text.append(key, style=TEXT_MUTED)
        text.append(" " * (key_width - cell_len(key) + 2))
        text.append(value, style=f"bold {TEXT_PRIMARY}")
        if index < len(rows) - 1:
            text.append("\n")
    return text


# ------------------------------------------------------------------
# 键帽提示行
# ------------------------------------------------------------------
def keycap(key: str) -> Text:
    """按键名亮绿加粗；描述用中性弱化色，身份靠键帽而不是整行染色。"""
    return Text(key, style=f"bold {GREEN_BRIGHT}")


def build_hint_line(parts: Iterable[tuple[str, str] | str]) -> Text:
    """把 (键, 说明) 或裸文本拼成一行提示，分隔符按终端 Unicode/ASCII。

    键名穿亮绿，说明穿弱化灰，分隔点更弱——三级层次而不是一行同色。
    """
    g = _g()
    line = Text()
    for index, part in enumerate(parts):
        if index:
            line.append(g.sep_tight, style=TEXT_DIM)
        if isinstance(part, str):
            line.append(part, style=TEXT_MUTED)
        else:
            key, desc = part
            line += keycap(key)
            if desc:
                line.append(f" {desc}", style=TEXT_MUTED)
    return line


# ------------------------------------------------------------------
# 进度条（自适应宽度）
# ------------------------------------------------------------------
def _format_duration(seconds: int) -> str:
    """把秒数格式化为 m:ss 或 h:mm:ss，用于进度条「剩余时间」。"""
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def build_progress_text(
    description: str,
    completed: int,
    total: int,
    *,
    spin_frame: int | None = None,
    bar_width: int = 30,
) -> Text:
    """挂课进度双行：摘要行 + 进度轨。

    spin_frame 与秒数解耦：桥接层以约 10Hz 递增，转圈才流畅；
    未传时回退 completed（兼容旧调用）。bar_width 由调用方按可用
    宽度收敛（窄终端缩短轨道而不是折行）。
    """
    cs = progress_charset()
    total = max(1, int(total))
    completed = max(0, min(int(completed), total))
    ratio = completed / total
    remaining = total - completed
    bar_width = max(10, int(bar_width))
    filled = max(0, min(bar_width, int(round(ratio * bar_width))))
    frame = int(completed if spin_frame is None else spin_frame)
    spin = cs.spinner[frame % len(cs.spinner)]

    bar = Text()
    # 第一行：状态摘要
    bar.append(f" {spin} ", style=f"bold {GREEN_BRIGHT}")
    bar.append(str(description or "处理中"), style=f"bold {TEXT_PRIMARY}")
    bar.append(cs.sep, style=TEXT_DIM)
    bar.append(f"{ratio * 100:5.1f}%", style=f"bold {GREEN_BRIGHT}")
    bar.append(cs.sep, style=TEXT_DIM)
    bar.append(f"{completed}/{total}s", style=TEXT_DIM)
    bar.append(cs.sep, style=TEXT_DIM)
    if remaining > 0:
        bar.append(f"剩余 {_format_duration(remaining)}", style=f"bold {SUCCESS}")
    else:
        bar.append("完成", style=f"bold {SUCCESS}")
    bar.append("\n")
    # 第二行：进度轨（完成段亮绿，未完成段是主色同族的深绿轨）
    bar.append("   ", style="")
    if cs.bracket_l:
        bar.append(cs.bracket_l, style=HAIRLINE)
    if filled > 0:
        bar.append(cs.track_done * filled, style=f"bold {GREEN_BRIGHT}")
    if filled < bar_width:
        bar.append(cs.track_todo * (bar_width - filled), style=TRACK)
    if cs.bracket_r:
        bar.append(cs.bracket_r, style=HAIRLINE)
    if remaining > 0:
        bar.append(cs.tail_run, style=f"dim {GREEN}")
    else:
        bar.append(cs.tail_done, style=f"bold {SUCCESS}")
    return bar
