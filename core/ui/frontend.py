"""A frontend is passed to controllers; progress follows the operation context."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Protocol


class Frontend(Protocol):
    def show_title(self, title: str, subtitle: str | None = None): ...
    def render_dashboard(self, state): ...
    def show_menu(self, options: list[str]) -> int: ...
    def prompt_choice(
        self, title: str, options: list[str], prompt: str = "请选择"
    ) -> int: ...
    def prompt_yes_no(self, message: str, default: str = "N") -> bool: ...
    def show_info(self, message: str): ...
    def show_success(self, message: str): ...
    def show_warning(self, message: str): ...
    def show_error(self, message: str): ...
    def pause(self, message: str = "按回车返回主菜单"): ...
    def prepare_menu_loading(self): ...
    def begin_operation(self, title: str, message: str): ...
    def show_summary(self, title: str, rows: list[tuple[str, str]]): ...
    def prompt_summary_confirmation(
        self, title, rows, message="确认继续处理？", default="Y"
    ) -> bool: ...
    def prompt_multiline_input(
        self,
        messages,
        *,
        title="手动选择课程 / 录入链接",
        cancel_message="已取消手动选择课程 / 录入链接",
    ) -> str: ...
    def prepare_pause_with_summary(
        self, title, rows, message="查看完成后返回主菜单"
    ): ...
    def wait_prepared_prompt(self, handle): ...
    async def wait_with_progress(self, duration: int, description: str = "处理中"): ...


_frontend = ContextVar("operation_frontend", default=None)


def current_frontend() -> Frontend | None:
    return _frontend.get()


@contextmanager
def use_frontend(frontend: Frontend | None):
    token = _frontend.set(frontend)
    try:
        yield frontend
    finally:
        _frontend.reset(token)


async def wait_with_progress(duration: int, description: str = "处理中"):
    frontend = _frontend.get()
    if frontend is None:
        await asyncio.sleep(max(0, duration))
    else:
        await frontend.wait_with_progress(duration, description=description)
