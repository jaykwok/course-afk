from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import logging
import re
import sys

from playwright.async_api import async_playwright

from core.abort import BrowserCertificateError
from core.config import (
    BROWSER_ARGS,
    BROWSER_CHANNEL,
    BROWSER_TYPE,
    COOKIES_FILE,
    MYLEARNING_HOME,
)
from core.file_ops import load_cookies
from core.browser.overlays import prepare_page_after_navigation_async
from core.runtime import close_safely, own_playwright_driver


_CONTROLLER_PAGES: dict[int, object] = {}
# 心跳页关闭时登记停止请求；它不证明浏览器物理进程已经断开。
# 当前课程可完成，但不能开下一门。整窗关闭也会触发这页的 close；
# Edge 后台进程仍连接时，必须以此状态阻止继续工作。
_CONTEXT_WINDOW_CLOSED: dict[int, bool] = {}
_START_MAXIMIZED_ARG = "--start-maximized"
# 反自动化探测脚本（context.add_init_script，document_start 在每个 frame 执行）。
#
# 只修自动化真正改坏的痕迹，不伪造本来就真实的指纹：本项目强制可视浏览器 +
# 真实 msedge/chrome 通道，plugins / languages / window.chrome / WebGL 等都是
# 真值，硬去 hook 反而会造出「假得过头」的新特征。故此处只处理三件事：
#   1) Function.prototype.toString 伪装——补丁函数的源码是最直接的破绽；
#   2) navigator.webdriver——原型级 getter，且不在实例上留 own property；
#   3) 同源子 frame——init script 未必落到 JS 动态建的 about:blank/srcdoc，
#      检测脚本惯用 iframe.contentWindow 取「干净」的 navigator。
#
# 仍无法在页面内消除的：CDP Runtime 域开启后的 Error.stack 探针、
# --disable-blink-features 之外的启动参数指纹——那些得在浏览器侧解决。
BROWSER_STEALTH_INIT_SCRIPT = """
(() => {
  "use strict";

  const patchedRealms = new WeakSet();

  const applyStealth = (win) => {
    if (!win || patchedRealms.has(win)) return;

    // 跨源 window：下面任一属性访问都会抛 SecurityError，交给调用方吞掉
    const functionProto = win.Function.prototype;
    const nav = win.navigator;
    if (!functionProto || !nav) return;
    patchedRealms.add(win);

    // --- 1. toString 伪装 ---------------------------------------------
    // 补丁函数直接 toString() 会吐出 "() => false"，检测脚本一眼看穿。
    // 维护「函数 -> 原生源码串」映射，并用 Proxy 接管 toString 本身，
    // 连 Function.prototype.toString.toString() 也返回原生串。
    const nativeSources = new win.WeakMap();
    const rawToString = functionProto.toString;

    const markNative = (fn, source) => {
      try {
        nativeSources.set(fn, source);
      } catch (_) {}
      return fn;
    };

    const maskedToString = new win.Proxy(rawToString, {
      apply(target, thisArg, args) {
        try {
          if (typeof thisArg === "function" && nativeSources.has(thisArg)) {
            return nativeSources.get(thisArg);
          }
        } catch (_) {}
        return Reflect.apply(target, thisArg, args);
      },
    });
    markNative(maskedToString, "function toString() { [native code] }");
    try {
      Object.defineProperty(functionProto, "toString", {
        value: maskedToString,
        writable: true,
        enumerable: false,
        configurable: true,
      });
    } catch (_) {}

    // 造一个「看起来像 WebIDL 原生 getter」的取值函数
    const nativeGetter = (prop, impl) => {
      const getter = function () {
        return impl.call(this);
      };
      try {
        Object.defineProperty(getter, "name", {
          value: "get " + prop,
          configurable: true,
        });
      } catch (_) {}
      return markNative(getter, "function get " + prop + "() { [native code] }");
    };

    // --- 2. navigator.webdriver ---------------------------------------
    // 真实 Chrome：Navigator.prototype 上一个返回 false 的原生 getter，
    // 实例上没有同名 own property。旧写法额外在实例上 defineProperty，
    // Object.getOwnPropertyNames(navigator) 一查就露馅，故先删掉再补原型。
    try {
      delete nav.webdriver;
    } catch (_) {}

    const navigatorProto = win.Navigator
      ? win.Navigator.prototype
      : Object.getPrototypeOf(nav);
    if (navigatorProto) {
      try {
        Object.defineProperty(navigatorProto, "webdriver", {
          configurable: true,
          enumerable: true,
          get: nativeGetter("webdriver", () => false),
          set: undefined,
        });
      } catch (_) {}
    }

    // --- 3. 同源子 frame 逃逸 ------------------------------------------
    // 取 contentWindow / contentDocument 时顺手给子 realm 打同样的补丁。
    const hookFrameWindow = (target, prop, resolveWindow) => {
      if (!target) return;
      const descriptor = Object.getOwnPropertyDescriptor(target, prop);
      if (!descriptor || typeof descriptor.get !== "function") return;
      const originalGet = descriptor.get;
      const patchedGet = nativeGetter(prop, function () {
        const value = originalGet.call(this);
        try {
          applyStealth(resolveWindow(value));
        } catch (_) {}
        return value;
      });
      try {
        Object.defineProperty(target, prop, {
          configurable: true,
          enumerable: descriptor.enumerable,
          get: patchedGet,
          set: descriptor.set,
        });
      } catch (_) {}
    };

    const iframeProto = win.HTMLIFrameElement
      ? win.HTMLIFrameElement.prototype
      : null;
    hookFrameWindow(iframeProto, "contentWindow", (value) => value);
    hookFrameWindow(iframeProto, "contentDocument", (value) =>
      value ? value.defaultView : null
    );
  };

  try {
    applyStealth(window);
  } catch (_) {}
})();
"""
_HEADLESS_DISABLED_MESSAGE = "项目禁止使用 headless 浏览器，请使用可视浏览器运行"


def _ensure_visible_browser(headless: bool) -> None:
    if headless:
        raise ValueError(_HEADLESS_DISABLED_MESSAGE)


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
    _ensure_visible_browser(headless)
    options = {"headless": headless}

    if BROWSER_TYPE == "chromium":
        args = list(BROWSER_ARGS)
        if _START_MAXIMIZED_ARG not in args:
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
    _ensure_visible_browser(headless)
    return {"no_viewport": True}




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
    if _CONTEXT_WINDOW_CLOSED.get(id(context)):
        return False
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


def is_controller_window_closed(context) -> bool:
    """是否因心跳页关闭而登记了停止请求。"""
    return _CONTEXT_WINDOW_CLOSED.get(id(context), False)


def get_controller_page(context):
    """当前心跳页（未注册或已释放时为 None）。"""
    return _CONTROLLER_PAGES.get(id(context))


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


def _mark_controller_window_closed(context) -> None:
    """心跳页被关：登记停止请求。"""
    _CONTEXT_WINDOW_CLOSED[id(context)] = True


def _remember_controller_page(context, page) -> None:
    _CONTROLLER_PAGES[id(context)] = page
    on = getattr(page, "on", None)
    if callable(on):
        # 不重开心跳页；各流程据此停止并保存剩余链接。
        on("close", lambda: _mark_controller_window_closed(context))


async def ensure_controller_page(context):
    """确保常驻心跳页可用；已登记关闭时返回 None，不重开。"""
    if _CONTEXT_WINDOW_CLOSED.get(id(context)):
        return None
    controller_page = _CONTROLLER_PAGES.get(id(context))
    if controller_page is not None and not _is_page_closed(controller_page):
        return controller_page
    if not is_browser_connected(context):
        return None
    controller_page = await _open_controller_page(context)
    _remember_controller_page(context, controller_page)
    return controller_page


def is_controller_page(context, page) -> bool:
    """是否为该 context 的常驻主控页。"""
    if page is None:
        return False
    controller = _CONTROLLER_PAGES.get(id(context))
    return controller is not None and page is controller


def release_controller_page(context) -> None:
    _CONTROLLER_PAGES.pop(id(context), None)
    _CONTEXT_WINDOW_CLOSED.pop(id(context), None)


def raise_for_certificate_error(exc: Exception) -> None:
    """将浏览器证书错误转换为可恢复错误，保持 TLS 校验启用。"""
    if isinstance(exc, BrowserCertificateError):
        raise exc
    match = re.search(r"net::(ERR_CERT_[A-Z_]+)", str(exc))
    if match:
        raise BrowserCertificateError(match.group(1)) from exc


@asynccontextmanager
async def create_browser_context(
    cookies_path=COOKIES_FILE, headless=False, slow_mo=None, *, controller=True
):
    """浏览器初始化上下文管理器, 封装重复的启动/认证/关闭流程"""

    _ensure_visible_browser(headless)

    cookies = load_cookies(cookies_path) if cookies_path is not None else []
    manager = async_playwright()
    browser = context = driver_job = None
    try:
        async with asyncio.timeout(90):
            p = await asyncio.wait_for(manager.__aenter__(), 30)
            driver_job = own_playwright_driver(p)
            browser = await asyncio.wait_for(launch_async_browser(p, headless=headless, slow_mo=slow_mo), 30)
            context = await asyncio.wait_for(browser.new_context(**build_browser_context_options(headless=headless)), 30)
            await apply_async_browser_stealth(context)
            await context.add_cookies(cookies)

            # 保留一个常驻心跳页：课程页逐门开关时它始终在场，关闭它登记停止请求。
            if controller:
                controller_page = await _open_controller_page(context, headless=headless)
                _remember_controller_page(context, controller_page)
        yield browser, context
    except Exception as exc:
        raise_for_certificate_error(exc)
        raise
    finally:
        exception_info = sys.exc_info()
        if driver_job is None:
            # __aenter__ may start a transport before timing out or being
            # cancelled. The manager still owns that partially started driver.
            try:
                driver_job = own_playwright_driver(manager)
            except Exception:
                logging.exception("无法附加启动失败的 Playwright driver，继续执行关闭")
        async def cleanup():
            if context is not None:
                try:
                    await close_safely(context.close(), label="browser context")
                finally:
                    release_controller_page(context)
            if browser is not None:
                await close_safely(browser.close(), label="browser")
            await close_safely(manager.__aexit__(*exception_info), label="Playwright driver")
        try:
            await close_safely(cleanup(), timeout=18, label="browser lifecycle")
        finally:
            if context is not None:
                release_controller_page(context)
            if driver_job is not None:
                await close_safely(driver_job.terminate(), label="browser process tree")
