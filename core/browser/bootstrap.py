"""Per-business-context cookie activation and verified first navigation."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import re
from urllib.parse import unquote, urlsplit

from core.auth.access import CredentialUnavailableError
from core.config import MYLEARNING_HOME
from core.runtime import close_safely


@dataclass
class PlatformSession:
    home: object
    anchor: object
    account_name: str
    valid: bool = True
    temporary_pages: set = field(default_factory=set)


_sessions: dict[int, PlatformSession] = {}
_ENTRY_NAVIGATION_TIMEOUT_SECONDS = 30
_RESOURCE_NAVIGATION_TIMEOUT_SECONDS = 30
_LOGIN_ROUTE = re.compile(
    r"(?:^|/)(?:login|signin|sign-in|esurfinglogin)(?:[/.?]|$)"
    r"|^/?(?:sso|oauth2?|authorize|authorization)(?:[/.?]|$)",
    re.I,
)
_ENTRY_LOGIN_VISIBLE_SCRIPT = """() =>
    [...document.querySelectorAll('#esurfingloginiframe, input[type="password"], [autocomplete="current-password"]')]
    .some(element => {
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden' &&
            Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
    })
"""


async def bootstrap_platform_session(context, cookies_path, *, anchor=None):
    from core.auth.credential import load_credential_metadata, extract_account_profile_from_context
    from core.browser.overlays import prepare_page_after_navigation_async
    metadata = load_credential_metadata(cookies_path)
    if not metadata or not metadata.account_name.strip():
        raise CredentialUnavailableError("凭证缺少稳定账号标识，请在账号与登录中重新登录")
    if id(context) in _sessions:
        if not _sessions[id(context)].valid:
            raise CredentialUnavailableError("平台登录态已失效，请重新初始化或登录")
        return
    home = await context.new_page()
    try:
        async with asyncio.timeout(65):
            await home.goto(MYLEARNING_HOME, wait_until="domcontentloaded", timeout=30000)
            await home.wait_for_url(MYLEARNING_HOME, wait_until="load", timeout=30000)
            await prepare_page_after_navigation_async(home)
            profile = await extract_account_profile_from_context(context)
            if profile.account_name != metadata.account_name:
                raise CredentialUnavailableError("浏览器登录账号与任务凭证不一致，已停止派发；请重新登录")
            # An inert placeholder keeps visible Chromium alive between business
            # tabs. It has no platform authority and no close-to-stop callback.
            anchor = anchor if anchor is not None else await context.new_page()
            _sessions[id(context)] = PlatformSession(home, anchor, profile.account_name)
    except CredentialUnavailableError:
        raise
    except Exception as exc:
        from core.browser.session import raise_for_certificate_error
        raise_for_certificate_error(exc)
        raise CredentialUnavailableError("登录态初始化失败或超时，请重新登录；当前及剩余任务保留") from exc


def is_session_internal_page(context, page):
    from core.browser.application import current_browser
    owner = current_browser()
    if owner and owner.context is context and owner._management_page(page):
        return True
    state = _sessions.get(id(context))
    return bool(state and (page in (state.home, state.anchor) or page in state.temporary_pages))


def register_internal_page(context, page):
    state = _sessions.get(id(context))
    if state:
        state.temporary_pages.add(page)


def unregister_internal_page(context, page):
    state = _sessions.get(id(context))
    if state:
        state.temporary_pages.discard(page)


def release_platform_session(context):
    _sessions.pop(id(context), None)


def _navigation_unavailable(page, state, message):
    state.valid = False
    from core.browser.application import current_browser
    owner = current_browser()
    if owner and owner.context is page.context:
        owner.version = None
    return CredentialUnavailableError(message)


def _target_matches(expected, actual):
    """A known resource must retain its ID, tenant and subject-source identity."""
    from core.tasks.identity import parse_entry
    wanted, got = parse_entry(expected, "navigation"), parse_entry(actual, "navigation")
    return wanted.key.kind != "entry" and wanted.key == got.key


def _is_entry_destination(url):
    """Portal redirects may change routes, but auth landing routes are not ready."""
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        return False
    if parsed.hostname.lower().split(".")[0] in {"login", "sso", "passport", "esurfinglogin"}:
        return False
    # Redirect query values and catalog topics named after auth protocols are
    # data, not evidence that the browser is on a sign-in route.
    routes = (parsed.path, parsed.fragment.split("?", 1)[0])
    return not any(_LOGIN_ROUTE.search(unquote(route)) for route in routes)


async def _wait_for_entry_navigation(page):
    # A generic portal has no resource ID to match. Let normal HTTP/SPA/SSO
    # redirects finish, checking explicit login routes and visible login UI.
    # The account prerequisite remains bootstrap_platform_session's verified ID.
    async with asyncio.timeout(_ENTRY_NAVIGATION_TIMEOUT_SECONDS):
        while True:
            if _is_entry_destination(str(page.url)):
                try:
                    if (not await page.evaluate(_ENTRY_LOGIN_VISIBLE_SCRIPT)
                            and _is_entry_destination(str(page.url))):
                        return
                except Exception as exc:
                    # A client redirect can destroy the old document while its
                    # read-only login check runs. Observe the replacement page.
                    if "execution context was destroyed" not in str(exc).lower():
                        raise
            await asyncio.sleep(.1)


async def _wait_for_course_navigation(page, url, state, ready):
    """Wait through the knowledge site's delayed SPA SSO and resource load."""
    from core.tasks.identity import parse_entry
    from core.auth.credential import extract_account_profile_from_context
    reopened = False
    async with asyncio.timeout(_RESOURCE_NAVIGATION_TIMEOUT_SECONDS):
        while True:
            actual = str(page.url)
            if _target_matches(url, actual):
                # A matching initial URL can still be an unauthenticated SPA
                # shell. Course data/DOM is the read-only readiness evidence.
                if ready() or await page.locator("dl.chapter-list-box").count():
                    return
            elif (not reopened and urlsplit(actual).hostname == "kc.zhixueyun.com"
                  and parse_entry(actual, "navigation").key.kind == "entry"
                  and _is_entry_destination(actual)):
                # Some SSO flows finish at the knowledge homepage and discard
                # the resource URL. Recheck the account, then reopen only this
                # GET navigation once. No business action has been dispatched.
                await _wait_for_entry_navigation(page)
                profile = await extract_account_profile_from_context(page.context)
                if profile.account_name != state.account_name:
                    raise _navigation_unavailable(page, state, "认证回跳后的账号与任务凭证不一致，已停止派发")
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                reopened = True
            await asyncio.sleep(.1)


async def navigate_platform_page(page, url, **kwargs):
    """Only read-only navigation is checked; never replay enrollment or submit."""
    state = _sessions.get(id(page.context)) if getattr(page, "context", None) else None
    if state and not state.valid:
        raise CredentialUnavailableError("平台登录态已失效，未开始业务操作；请重新初始化或登录")
    if state:
        kwargs.setdefault("timeout", 30000)
    from core.tasks.identity import parse_entry
    from playwright.async_api import TimeoutError as NavigationTimeout
    course = bool(state and urlsplit(url).hostname == "kc.zhixueyun.com"
                  and parse_entry(url, "navigation").key.kind == "course")
    course_id = parse_entry(url, "navigation").key.business_id if course else ""
    resource_ready = False
    def response_received(response):
        nonlocal resource_ready
        parsed = urlsplit(response.url)
        if (parsed.hostname == "kc.zhixueyun.com"
                and parsed.path.endswith("/course-front/info/" + course_id)
                and response.status >= 400 and response.status != 401):
            # Let the business layer classify a loaded denial/not-found page.
            # A successful response still requires the actual course DOM.
            resource_ready = True
    if course:
        page.on("response", response_received)
    try:
        result = await page.goto(url, **kwargs)
        if course:
            await _wait_for_course_navigation(page, url, state, lambda: resource_ready)
    except (TimeoutError, NavigationTimeout):
        if state:
            raise _navigation_unavailable(page, state, "课程页未在认证后就绪，未开始业务操作；请重新登录或核对链接") from None
        raise
    finally:
        if course:
            page.remove_listener("response", response_received)
    if state:
        from core.tasks.identity import parse_entry
        known_resource = parse_entry(url, "navigation").key.kind != "entry"
        try:
            if known_resource:
                # DOMContentLoaded alone can precede an SSO client redirect.
                await page.wait_for_url(lambda actual: _target_matches(url, str(actual)), timeout=30000,
                                        wait_until="domcontentloaded")
            else:
                await _wait_for_entry_navigation(page)
        except Exception as exc:
            from playwright.async_api import TimeoutError as NavigationTimeout
            if isinstance(exc, (TimeoutError, NavigationTimeout)):
                raise _navigation_unavailable(page, state, "目标页未在认证后就绪，未开始业务操作；请重新登录或核对链接") from exc
            raise
        if known_resource and not _target_matches(url, str(page.url)):
            raise _navigation_unavailable(page, state, "目标页被登录流程重定向，未开始业务操作；请重新登录")
        if state.home is not None:
            home, state.home = state.home, None
            await close_safely(home.close(), label="temporary platform home")
    return result
