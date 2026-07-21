"""菜单数字键约定：每页最多 10 项；第 10 项键位为 0（而非 10）。"""

from __future__ import annotations

MAX_MENU_OPTIONS = 10


def ensure_menu_option_count(count: int) -> None:
    if count < 1:
        raise ValueError("菜单至少需要 1 个选项")
    if count > MAX_MENU_OPTIONS:
        raise ValueError(
            f"菜单选项不能超过 {MAX_MENU_OPTIONS} 个，请合并同类项并使用二级菜单"
        )


def menu_key_for_index(index_1based: int, total: int) -> str:
    """1-based 序号 → 显示/按键标签。第 10 项固定为「0」。"""
    ensure_menu_option_count(total)
    if not (1 <= index_1based <= total):
        raise ValueError(f"无效菜单序号: {index_1based} (共 {total} 项)")
    if total == MAX_MENU_OPTIONS and index_1based == MAX_MENU_OPTIONS:
        return "0"
    return str(index_1based)


def menu_key_labels(total: int) -> list[str]:
    ensure_menu_option_count(total)
    return [menu_key_for_index(i, total) for i in range(1, total + 1)]


def menu_keys_hint(total: int) -> str:
    """提示文案，如「1-9/0」或「1-3」。"""
    ensure_menu_option_count(total)
    if total == MAX_MENU_OPTIONS:
        return "1-9/0"
    if total == 1:
        return "1"
    return f"1-{total}"


def parse_menu_key(raw: str, total: int) -> int | None:
    """
    将用户输入映射为 1-based 选项序号。

    - 「0」在恰好 10 项时表示第 10 项
    - 「1」…「9」对应前 9 项
    - CLI 仍兼容输入「10」表示第 10 项
    """
    ensure_menu_option_count(total)
    text = (raw or "").strip()
    if not text:
        return None
    if text == "0":
        return MAX_MENU_OPTIONS if total == MAX_MENU_OPTIONS else None
    if text.isdigit():
        value = int(text)
        if 1 <= value <= total:
            return value
    return None
