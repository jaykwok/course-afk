from __future__ import annotations

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
from core.browser.overlays import prepare_page_after_navigation_async


_CONTROLLER_PAGES: dict[int, object] = {}
_CONTEXT_HEADLESS: dict[int, bool] = {}
# 心跳页（常驻主控页）关闭闩锁。设计语义：该页被关 = 浏览器整窗被关。
# 「关闭单个课程标签页」与「关闭整窗」在异常层面无法判别，靠这页常驻来区分：
# 课程页关了它还在（继续挂课），它关了就是整窗关闭（停止并保存剩余链接）。
# Edge 后台模式在窗口关闭后进程仍存活（is_connected 恒 True），必须用本
# 闩锁短路连接判断，否则整窗关闭会被误判成「仅关标签」而继续下一门。
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
    _ensure_visible_browser(headless)
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
    """心跳页是否已被关闭（= 浏览器整窗已被用户关闭）。"""
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
    """心跳页被关：标记整窗关闭（见 _CONTEXT_WINDOW_CLOSED 的设计说明）。"""
    _CONTEXT_WINDOW_CLOSED[id(context)] = True


def _remember_controller_page(context, page) -> None:
    _CONTROLLER_PAGES[id(context)] = page
    on = getattr(page, "on", None)
    if callable(on):
        # 心跳语义：这页被关就认定整窗被关——不重开、不恢复，
        # 由各流程的 UserCancelRequested 路径停止并保存剩余链接。
        on("close", lambda: _mark_controller_window_closed(context))


async def ensure_controller_page(context):
    """确保常驻心跳页可用；心跳页已关闭（= 整窗关闭）时返回 None，不重开。"""
    if _CONTEXT_WINDOW_CLOSED.get(id(context)):
        return None
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
    _CONTEXT_WINDOW_CLOSED.pop(id(context), None)


@asynccontextmanager
async def create_browser_context(
    cookies_path=COOKIES_FILE, headless=False, slow_mo=None
):
    """浏览器初始化上下文管理器, 封装重复的启动/认证/关闭流程"""

    _ensure_visible_browser(headless)

    cookies = load_cookies(cookies_path)

    async with async_playwright() as p:
        browser = await launch_async_browser(p, headless=headless, slow_mo=slow_mo)
        context = await browser.new_context(
            **build_browser_context_options(headless=headless)
        )
        await apply_async_browser_stealth(context)
        _CONTEXT_HEADLESS[id(context)] = headless
        await context.add_cookies(cookies)

        # 保留一个常驻心跳页（mylearning 首页）：课程页逐门开关它始终在场，
        # 浏览器不会因「最后一页被关」而退出；它自己被关则说明用户关掉了
        # 整窗——关闭事件会置位 _CONTEXT_WINDOW_CLOSED，各流程据此停止。
        controller_page = await _open_controller_page(
            context,
            headless=headless,
        )
        _remember_controller_page(context, controller_page)

        try:
            yield browser, context
        finally:
            try:
                await context.close()
            except Exception:
                pass
            finally:
                # context.close() 会同步触发心跳页的 close 回调并置位窗口关闭
                # 闩锁；必须在回调之后统一清理，否则每轮正常退出都会重新
                # 遗留一个以旧 context id 为键的闩锁。
                release_controller_page(context)
            try:
                await browser.close()
            except Exception:
                pass
