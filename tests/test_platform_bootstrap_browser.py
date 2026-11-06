"""Visible simulated SSO: every HTTPS response is fulfilled locally."""
from datetime import datetime
import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
import unittest
from unittest.mock import patch

from aiohttp import web

from core.auth.credential import AccountProfile, save_credential_bundle
from core.browser.bootstrap import bootstrap_platform_session, navigate_platform_page, release_platform_session
from core.browser.session import create_browser_context
from core.config import MYLEARNING_HOME
from core.auth.access import CredentialUnavailableError
from core.browser import bootstrap
from core.discovery import manual


class VisibleBootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_cookie_activation_profile_and_first_course_in_same_context(self):
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/"cookies.json"
            cookie={"name":"saved-test","value":"local-only","domain":".mylearning.cn","path":"/","secure":True}
            save_credential_bundle(datetime.now().astimezone(),AccountProfile("模拟账号","center:offline"),[cookie],cookies_path=path)
            async with create_browser_context(cookies_path=None) as (_,context):
                activated=False;homes=0;profile_reads=0
                async def route(request_route):
                    nonlocal activated,homes,profile_reads
                    url=request_route.request.url
                    if url==MYLEARNING_HOME:
                        self.assertTrue(any(c["name"]=="saved-test" for c in await context.cookies()))
                        activated=True;homes+=1
                        await request_route.fulfill(content_type="text/html; charset=utf-8",body="<h1>模拟平台首页</h1>")
                    elif url.startswith("https://center.mylearning.cn/PC/home"):
                        await request_route.fulfill(content_type="text/html",body="<script>localStorage.setItem('token','offline-token')</script>")
                    elif url=="https://center.mylearning.cn/api/personal/user/info":
                        self.assertTrue(activated);profile_reads+=1
                        await request_route.fulfill(json={"code":0,"data":{"id":"offline","name":"模拟账号"}})
                    elif urlsplit(url).hostname=="kc.zhixueyun.com":
                        if not activated:
                            await request_route.fulfill(status=302,headers={"Location":MYLEARNING_HOME})
                        else:
                            await request_route.fulfill(content_type="text/html; charset=utf-8",body="<h1>模拟目标课程</h1><dl class='chapter-list-box'></dl>")
                    else:
                        await request_route.abort()
                await context.route("**/*",route)
                await context.add_cookies([cookie])
                try:
                    await bootstrap_platform_session(context,path)
                    home=next(p for p in context.pages if p.url==MYLEARNING_HOME)
                    for course_id in ("11111111-1111-4111-8111-111111111111","22222222-2222-4222-8222-222222222222"):
                        target=await context.new_page()
                        url="https://kc.zhixueyun.com/#/study/course/detail/"+course_id
                        await navigate_platform_page(target,url,wait_until="load")
                        self.assertEqual(target.url,url)
                        self.assertTrue(home.is_closed())
                        self.assertEqual(await target.locator("h1").inner_text(),"模拟目标课程")
                        await target.close()
                    self.assertEqual((homes,profile_reads),(1,1))
                finally:
                    release_platform_session(context)


class ManualEntryNavigationTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the production manual collector with a registered session gate."""

    async def asyncSetUp(self):
        self.stack = AsyncExitStack()
        self.addAsyncCleanup(self.stack.aclose)
        self.close_waiting = asyncio.Event()
        self.entry_pages = []
        app = web.Application()

        async def redirect(request):
            destinations = {
                "/audit-entry": "/audit-catalog#/modules/all",
                "/entry-to-login": "/login",
                "/entry-with-login-frame": "/portal-login",
                "/entry-with-sso": "/login?continue=catalog",
                "/resource": "/other#/study/course/detail/11111111-1111-4111-8111-111111111111/6/0/subject",
            }
            raise web.HTTPFound(destinations[request.path])

        async def content(request):
            if request.path == "/login":
                body = "<h1>Sign in</h1>"
                if request.query.get("continue"):
                    body += "<script>setTimeout(() => location.href='/audit-catalog', 100)</script>"
            elif request.path == "/portal-login":
                body = '<h1>Portal</h1><iframe id="esurfingloginiframe" src="about:blank"></iframe>'
            else:
                body = "<h1>Authenticated catalog</h1>"
            return web.Response(text=body, content_type="text/html")

        for path in ("/audit-entry", "/entry-to-login", "/entry-with-login-frame", "/entry-with-sso", "/resource"):
            app.router.add_get(path, redirect)
        for path in ("/audit-catalog", "/login", "/portal-login", "/other"):
            app.router.add_get(path, content)
        runner = web.AppRunner(app)
        await runner.setup()
        self.stack.push_async_callback(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.origin = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        self.browser, self.context = await self.stack.enter_async_context(create_browser_context(cookies_path=None))

        async def local_only(route):
            if route.request.url.startswith(self.origin + "/"):
                await route.continue_()
            else:
                await route.abort()

        await self.context.route("**/*", local_only)
        self.home = await self.context.new_page()
        anchor = await self.context.new_page()
        bootstrap._sessions[id(self.context)] = bootstrap.PlatformSession(self.home, anchor, "offline-verified")
        self.stack.callback(release_platform_session, self.context)

        def watch_entry(page):
            self.entry_pages.append(page)
            original = page.wait_for_event

            async def wait_for_event(event, **kwargs):
                if event == "close":
                    self.close_waiting.set()
                return await original(event, **kwargs)

            self.stack.enter_context(patch.object(page, "wait_for_event", side_effect=wait_for_event))

        self.context.on("page", watch_entry)

    async def collect(self, entry):
        return await manual.collect_learning_links_from_entry_urls([self.origin + entry], context=self.context)

    async def verify_catalog_stays_open(self, entry, destination):
        pending = asyncio.create_task(self.collect(entry))
        try:
            await asyncio.wait_for(self.close_waiting.wait(), 5)
            page = self.entry_pages[0]
            self.assertEqual(page.url, self.origin + destination)
            self.assertEqual(await page.locator("h1").inner_text(), "Authenticated catalog")
            self.assertFalse(pending.done(), "manual selection must wait for the user's close")
            self.assertTrue(self.home.is_closed())
            self.assertTrue(self.browser.is_connected())
            await page.close()
            self.assertEqual(await asyncio.wait_for(pending, 3), (0, 0, 0))
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def test_manual_entry_http_redirect_to_catalog_keeps_selection_open(self):
        await self.verify_catalog_stays_open("/audit-entry", "/audit-catalog#/modules/all")

    async def test_manual_entry_allows_automatic_sso_return_to_catalog(self):
        await self.verify_catalog_stays_open("/entry-with-sso", "/audit-catalog")

    async def test_manual_entry_stuck_on_login_route_is_rejected(self):
        with patch.object(bootstrap, "_ENTRY_NAVIGATION_TIMEOUT_SECONDS", .25):
            with self.assertRaises(CredentialUnavailableError):
                await self.collect("/entry-to-login")
        self.assertFalse(self.home.is_closed())
        self.assertFalse(self.close_waiting.is_set())

    async def test_manual_entry_visible_login_iframe_is_rejected(self):
        with patch.object(bootstrap, "_ENTRY_NAVIGATION_TIMEOUT_SECONDS", .25):
            with self.assertRaises(CredentialUnavailableError):
                await self.collect("/entry-with-login-frame")
        self.assertFalse(self.home.is_closed())
        self.assertFalse(self.close_waiting.is_set())

    async def test_known_course_source_identity_remains_strict_in_manual_collector(self):
        def shorten_resource_wait(page):
            original = page.wait_for_url

            async def wait_for_url(predicate, **kwargs):
                return await original(predicate, **{**kwargs, "timeout": 250})

            self.stack.enter_context(patch.object(page, "wait_for_url", side_effect=wait_for_url))

        self.context.on("page", shorten_resource_wait)
        resource = "/resource#/study/course/detail/11111111-1111-4111-8111-111111111111/6/1/subject"
        with patch.object(bootstrap, "_RESOURCE_NAVIGATION_TIMEOUT_SECONDS", .25):
            with self.assertRaises(CredentialUnavailableError):
                await self.collect(resource)
        self.assertFalse(self.home.is_closed())
        self.assertFalse(self.close_waiting.is_set())
