"""Application-owned window; operations lease tabs, never browsers or loops."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
from pathlib import Path
import re
from urllib.parse import urlsplit

from core.runtime import close_safely

_owner = None
_application_required = False


def current_browser():
    return _owner


def require_application_browser(required):
    global _application_required
    _application_required = required


def isolated_browser_allowed():
    return not _application_required


def is_local_cookie(cookie):
    return cookie.get("domain", "").lstrip(".").lower() in {"127.0.0.1", "localhost", "::1"}


async def platform_cookies(context):
    """Never persist the Web control session alongside platform credentials."""
    return [cookie for cookie in await context.cookies() if not is_local_cookie(cookie)]


# A platform popup is still a tab in the owned window. Retain privacy flags but
# omit geometry/popup features that otherwise request a separate native window.
_TAB_POPUPS = """(() => {
  const open = window.open;
  window.open = function(url, target, features) {
    const privacy = String(features || '').split(',').filter(x =>
      /^(noopener|noreferrer)(=|$)/i.test(x.trim())).join(',');
    return open.call(this, url, target, privacy);
  };
})();"""


class ApplicationBrowser:
    def __init__(self, browser, context, management):
        self.browser, self.context, self.management = browser, context, management
        self.loop = asyncio.get_running_loop()
        self.lock = asyncio.Lock()
        self.version = None
        self.unusable = False
        self.origins = set()
        self.owned_pages = None
        self.context.on("page", self._page_created)

    def _check_loop(self):
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("浏览器活动必须运行于应用事件循环")

    def _management_page(self, page):
        if page is self.management:
            return True
        url = urlsplit(page.url)
        management = urlsplit(self.management.url)
        return bool(url.netloc and (url.scheme, url.netloc) == (management.scheme, management.netloc))

    def _remember_origin(self, frame):
        url = urlsplit(frame.url)
        if url.scheme in {"https", "http"} and url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            self.origins.add(f"{url.scheme}://{url.netloc}")

    def _page_created(self, page):
        if self.owned_pages is not None:
            self.owned_pages.add(page)
        page.on("framenavigated", self._remember_origin)

    async def _drain_tabs(self):
        # Capture stays enabled while close commands run: a popup delivered
        # during teardown belongs to this lease, never to the following one.
        while True:
            pending = [p for p in self.owned_pages or ()
                       if not self._management_page(p) and not p.is_closed()]
            if not pending:
                return
            for page in pending:
                await page.close()

    async def invalidate(self):
        """Drop platform storage with all business tabs closed; keep local auth."""
        async with asyncio.timeout(20):
            await self._clear_platform_storage()

    async def _clear_platform_storage(self):
        from core.browser.bootstrap import release_platform_session
        self._check_loop()
        self.version = None
        release_platform_session(self.context)
        for page in tuple(self.context.pages):
            if not self._management_page(page):
                await page.close()
        # Storage.clearDataForOrigin also removes service workers, IndexedDB and
        # caches. Session storage disappears with the business tabs above.
        if self.origins:
            cdp = await self.context.new_cdp_session(self.management)
            try:
                for origin in tuple(self.origins):
                    await cdp.send("Storage.clearDataForOrigin", {"origin": origin, "storageTypes": "all"})
                await cdp.send("Network.clearBrowserCache")
            finally:
                await cdp.detach()
            self.origins.clear()
        await self.context.clear_cookies(domain=re.compile(r"^(?!\.?127\.0\.0\.1$|\.?localhost$|\[?::1\]?$).+"))

    @asynccontextmanager
    async def lease(self, cookies_path):
        from core.auth.access import CredentialUnavailableError
        from core.browser.bootstrap import bootstrap_platform_session, _sessions
        from core.file_ops import load_cookies
        self._check_loop()
        async with self.lock:
            if self.unusable:
                raise RuntimeError("上轮业务标签未完整关闭，请退出程序后重新启动")
            self.owned_pages = set()
            login = cookies_path is None
            try:
                if login:
                    await self.invalidate()
                else:
                    path = Path(cookies_path)
                    version = (str(path.resolve()), hashlib.sha256(path.read_bytes()).digest())
                    if version != self.version:
                        await self.invalidate()
                        await self.context.add_cookies([c for c in load_cookies(path) if not is_local_cookie(c)])
                        await bootstrap_platform_session(self.context, path, anchor=self.management)
                        self.version = version
                yield self.browser, self.context
            except CredentialUnavailableError:
                self.version = None
                state = _sessions.get(id(self.context))
                if state:
                    state.valid = False
                raise
            finally:
                try:
                    await close_safely(self._drain_tabs(), timeout=18, label="activity tabs")
                finally:
                    pages, self.owned_pages = self.owned_pages, None
                    if any(not self._management_page(p) and not p.is_closed() for p in pages or ()):
                        self.unusable = True
                state = _sessions.get(id(self.context))
                if state and state.home and state.home.is_closed():
                    state.home = None
                if login and not self.management.is_closed():
                    # Saved credentials are reloaded and verified on the next
                    # business lease; failed/cancelled logins restore the old file.
                    await self.invalidate()
                if not self.management.is_closed():
                    await close_safely(self.management.bring_to_front(), label="management tab")


@asynccontextmanager
async def application_browser():
    global _owner
    from core.browser.session import create_isolated_browser_context
    if _owner is not None:
        raise RuntimeError("应用浏览器已启动")
    async with create_isolated_browser_context(cookies_path=None) as (browser, context):
        await context.add_init_script(_TAB_POPUPS)
        # A seeded persistent Chromium context opens one initial blank tab.
        management = context.pages[0] if context.pages else await context.new_page()
        owner = ApplicationBrowser(browser, context, management)
        _owner = owner
        try:
            yield owner
        finally:
            _owner = None
            context.remove_listener("page", owner._page_created)


async def invalidate_application_credentials():
    if _owner is not None:
        async with _owner.lock:
            await _owner.invalidate()
