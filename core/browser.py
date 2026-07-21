from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from playwright.async_api import async_playwright

from core.config import (
    BROWSER_ARGS,
    BROWSER_CHANNEL,
    BROWSER_TYPE,
    COOKIES_FILE,
    MYLEARNING_HOME,
)
from core.file_ops import load_cookies
from core.page_overlays import prepare_page_after_navigation_async


_CONTROLLER_PAGES: dict[int, object] = {}
_CONTEXT_HEADLESS: dict[int, bool] = {}
_START_MAXIMIZED_ARG = "--start-maximized"
BROWSER_STEALTH_INIT_SCRIPT = """
(() => {
  try {
    Object.defineProperty(Navigator.prototype, "webdriver", {
      configurable: true,
      get: () => false,
    });
  } catch (_) {}

  try {
    Object.defineProperty(navigator, "webdriver", {
      configurable: true,
      get: () => false,
    });
  } catch (_) {}
})();
"""


def _get_browser_launcher(playwright):
    try:
        return getattr(playwright, BROWSER_TYPE)
    except AttributeError as exc:
        raise ValueError(f"不支持的浏览器类型: {BROWSER_TYPE}") from exc


def build_browser_launch_options(
    *,
    headless: bool,
    slow_mo=None,
    extra_args: list[str] | None = None,
):
    options = {"headless": headless}

    if BROWSER_TYPE == "chromium":
        args = list(BROWSER_ARGS)
        if not headless and _START_MAXIMIZED_ARG not in args:
            args.append(_START_MAXIMIZED_ARG)
        if extra_args:
            for arg in extra_args:
                if arg not in args:
                    args.append(arg)
        if args:
            options["args"] = args
        if BROWSER_CHANNEL:
            options["channel"] = BROWSER_CHANNEL

    if slow_mo is not None:
        options["slow_mo"] = slow_mo
    return options


async def maximize_browser_window_for_page(page, *, headless: bool) -> None:
    if headless or BROWSER_TYPE != "chromium":
        return

    try:
        client = await page.context.new_cdp_session(page)
        window_info = await client.send("Browser.getWindowForTarget")
        await client.send(
            "Browser.setWindowBounds",
            {
                "windowId": window_info["windowId"],
                "bounds": {"windowState": "maximized"},
            },
        )
    except Exception:
        pass


def build_browser_context_options(*, headless: bool) -> dict[str, object]:
    if headless:
        return {}
    return {"no_viewport": True}


def apply_sync_browser_stealth(context) -> None:
    add_init_script = getattr(context, "add_init_script", None)
    if callable(add_init_script):
        add_init_script(BROWSER_STEALTH_INIT_SCRIPT)


async def apply_async_browser_stealth(context) -> None:
    add_init_script = getattr(context, "add_init_script", None)
    if callable(add_init_script):
        await add_init_script(BROWSER_STEALTH_INIT_SCRIPT)


async def launch_async_browser(playwright, *, headless: bool, slow_mo=None, extra_args=None):
    browser_launcher = _get_browser_launcher(playwright)
    return await browser_launcher.launch(
        **build_browser_launch_options(
            headless=headless,
            slow_mo=slow_mo,
            extra_args=extra_args,
        )
    )


def launch_sync_browser(playwright, *, headless: bool, slow_mo=None, extra_args=None):
    browser_launcher = _get_browser_launcher(playwright)
    return browser_launcher.launch(
        **build_browser_launch_options(
            headless=headless,
            slow_mo=slow_mo,
            extra_args=extra_args,
        )
    )


def is_target_closed_exception(exc: BaseException) -> bool:
    message = str(exc).lower()
    return exc.__class__.__name__ == "TargetClosedError" or any(
        marker in message
        for marker in (
            "target page, context or browser has been closed",
            "browser has been closed",
        )
    )


def get_context_browser(context):
    browser = getattr(context, "browser", None)
    if callable(browser):
        try:
            return browser()
        except Exception:
            return None
    return browser


def is_browser_connected(context) -> bool:
    browser = get_context_browser(context)
    if browser is None:
        return False

    is_connected = getattr(browser, "is_connected", None)
    if callable(is_connected):
        try:
            return bool(is_connected())
        except Exception:
            return False
    return False


def get_page_context(page):
    context = getattr(page, "context", None)
    if callable(context):
        try:
            return context()
        except Exception:
            return None
    return context


def is_page_browser_connected(page) -> bool:
    context = get_page_context(page)
    if context is None:
        return False
    return is_browser_connected(context)


def _is_page_closed(page) -> bool:
    is_closed = getattr(page, "is_closed", None)
    if callable(is_closed):
        try:
            return bool(is_closed())
        except Exception:
            return False
    return False


async def _open_controller_page(context, *, headless: bool = False):
    page = await context.new_page()
    await maximize_browser_window_for_page(page, headless=headless)
    await page.goto(MYLEARNING_HOME, wait_until="load")
    # 主控页也常被推广弹窗挡住，先关掉再挂着
    await prepare_page_after_navigation_async(page)
    return page


def _schedule_controller_page_restore(context, closed_page) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_restore_controller_page_if_needed(context, closed_page))


async def _restore_controller_page_if_needed(context, closed_page) -> None:
    current_page = _CONTROLLER_PAGES.get(id(context))
    if current_page is not closed_page:
        return
    if not is_browser_connected(context):
        return
    try:
        replacement_page = await _open_controller_page(
            context,
            headless=_CONTEXT_HEADLESS.get(id(context), False),
        )
    except Exception:
        return
    _remember_controller_page(context, replacement_page)


def _remember_controller_page(context, page) -> None:
    _CONTROLLER_PAGES[id(context)] = page
    on = getattr(page, "on", None)
    if callable(on):
        on("close", lambda: _schedule_controller_page_restore(context, page))


async def ensure_controller_page(context):
    controller_page = _CONTROLLER_PAGES.get(id(context))
    if controller_page is not None and not _is_page_closed(controller_page):
        return controller_page
    if not is_browser_connected(context):
        return None
    controller_page = await _open_controller_page(
        context,
        headless=_CONTEXT_HEADLESS.get(id(context), False),
    )
    _remember_controller_page(context, controller_page)
    return controller_page


def is_controller_page(context, page) -> bool:
    """是否为该 context 的常驻主控页（含恢复后的实例）。"""
    if page is None:
        return False
    controller = _CONTROLLER_PAGES.get(id(context))
    return controller is not None and page is controller


def release_controller_page(context) -> None:
    _CONTROLLER_PAGES.pop(id(context), None)
    _CONTEXT_HEADLESS.pop(id(context), None)


@asynccontextmanager
async def create_browser_context(
    cookies_path=COOKIES_FILE, headless=False, slow_mo=None
):
    """浏览器初始化上下文管理器, 封装重复的启动/认证/关闭流程"""

    cookies = load_cookies(cookies_path)

    async with async_playwright() as p:
        browser = await launch_async_browser(p, headless=headless, slow_mo=slow_mo)
        context = await browser.new_context(
            **build_browser_context_options(headless=headless)
        )
        await apply_async_browser_stealth(context)
        _CONTEXT_HEADLESS[id(context)] = headless
        await context.add_cookies(cookies)

        # 保留一个常驻主控页，避免课程页关闭后浏览器直接退出。
        controller_page = await _open_controller_page(
            context,
            headless=headless,
        )
        _remember_controller_page(context, controller_page)

        try:
            yield browser, context
        finally:
            release_controller_page(context)
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass
