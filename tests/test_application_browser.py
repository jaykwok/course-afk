"""Real Chromium tabs with offline platform responses; no real credentials."""
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

from core.app.runtime import ApplicationRuntime
from core.auth.access import CredentialUnavailableError
from core.auth.credential import AccountProfile, save_credential_bundle
from core.browser.application import application_browser, platform_cookies
from core.browser.bootstrap import navigate_platform_page, _sessions
from core.browser.session import create_browser_context
from core.config import MYLEARNING_HOME
from core.discovery import manual
from core.web.server import LocalServer

COURSE = "https://kc.zhixueyun.com/#/study/course/detail/11111111-1111-4111-8111-111111111111/6/1/subject"


async def window_id(context, page):
    cdp = await context.new_cdp_session(page)
    try:
        return (await cdp.send("Browser.getWindowForTarget"))["windowId"]
    finally:
        await cdp.detach()


class ApplicationBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = AsyncExitStack()
        self.addAsyncCleanup(self.stack.aclose)
        self.tmp = self.stack.enter_context(TemporaryDirectory())
        self.path = Path(self.tmp) / "cookies.json"
        self.save()
        self.runtime = ApplicationRuntime(dispatcher=lambda kind, ui: ui.pause())
        self.runtime.start()
        self.stack.push_async_callback(self.runtime.close)
        self.server = LocalServer(self.runtime)
        await self.server.start()
        self.stack.push_async_callback(self.server.close)
        self.owner = await self.stack.enter_async_context(application_browser())
        self.context, self.page = self.owner.context, self.owner.management
        self.homes = self.profiles = 0

        async def route(r):
            url = r.request.url
            if url.startswith(self.server.origin + "/"):
                await r.continue_()
            elif url == MYLEARNING_HOME:
                self.homes += 1
                await r.fulfill(content_type="text/html", body="<h1>Home</h1>")
            elif url.startswith("https://center.mylearning.cn/PC/home"):
                await r.fulfill(content_type="text/html", body="<script>localStorage.setItem('token','offline')</script>")
            elif url == "https://center.mylearning.cn/api/personal/user/info":
                self.profiles += 1
                await r.fulfill(json={"code": 0, "data": {"id": "offline", "name": "Offline"}})
            elif urlsplit(url).hostname in {"kc.zhixueyun.com", "cms.mylearning.cn"}:
                await r.fulfill(content_type="text/html", body="<h1>Course</h1><dl class='chapter-list-box'></dl>")
            else:
                await r.abort()

        await self.context.route("**/*", route)
        await self.page.goto(self.server.launch_url)
        await self.page.get_by_text("已连接 · 控制窗口", exact=True).wait_for()

    def save(self, value="first", account="center:offline"):
        save_credential_bundle(datetime.now().astimezone(), AccountProfile("Offline", account),
            [{"name": "platform-session", "value": value, "domain": ".mylearning.cn", "path": "/", "secure": True}], cookies_path=self.path)

    async def target(self, context):
        page = await context.new_page()
        await navigate_platform_page(page, COURSE, wait_until="load")
        return page

    async def test_same_window_including_popup_and_reuses_verified_session_across_activities(self):
        manager_window = await window_id(self.context, self.page)
        copied = await self.page.evaluate("({client:sessionStorage.getItem('client'),csrf:sessionStorage.getItem('csrf')})")
        reader = await self.context.new_page()
        import json
        await reader.add_init_script("Object.entries(" + json.dumps(copied) + ").forEach(([k,v])=>sessionStorage.setItem(k,v))")
        await reader.goto(self.server.origin)
        await reader.get_by_text("已连接 · 只读", exact=True).wait_for()
        self.assertEqual(await window_id(self.context, reader), manager_window)
        await reader.close()
        for _ in range(2):
            async with create_browser_context(self.path) as (browser, context):
                self.assertIs(browser, self.owner.browser)
                self.assertIs(context, self.context)
                for p in context.pages:
                    self.assertEqual(await window_id(context, p), manager_window)
                target = await self.target(context)
                async with target.expect_popup() as popup_info:
                    await target.evaluate("window.open('https://cms.mylearning.cn/catalog','_blank','popup,width=400,height=300')")
                popup = await popup_info.value
                await popup.wait_for_load_state()
                self.assertEqual(await window_id(context, popup), manager_window)
                self.assertIsNone(_sessions[id(context)].home)
            self.assertTrue(target.is_closed())
            self.assertTrue(popup.is_closed())
            self.assertEqual(context.pages, [self.page])
            self.assertFalse(self.page.is_closed())
        self.assertEqual((self.homes, self.profiles), (1, 1))

    async def test_changed_credential_and_cancelled_login_clear_platform_storage_keep_web_control(self):
        async with create_browser_context(self.path) as (_, context):
            target = await self.target(context)
            await target.evaluate("localStorage.setItem('old-account','secret'); sessionStorage.setItem('old','secret')")
            await target.evaluate("""async () => {
                await caches.open('old-account-cache');
                await new Promise((resolve,reject) => {
                    const request = indexedDB.open('old-account-database',1);
                    request.onsuccess=()=>{request.result.close();resolve()};
                    request.onerror=()=>reject(request.error);
                });
            }""")
        local_cookie = next(c for c in await context.cookies() if c['name'] == 'course_web')
        self.assertNotIn('course_web', [c['name'] for c in await platform_cookies(context)])
        self.assertNotIn('platform-session', await self.page.evaluate('document.cookie'))
        self.save('updated')
        async with create_browser_context(self.path) as (_, context):
            target = await self.target(context)
            self.assertIsNone(await target.evaluate("localStorage.getItem('old-account')"))
            self.assertIsNone(await target.evaluate("sessionStorage.getItem('old')"))
            self.assertEqual(await target.evaluate("caches.keys()"), [])
            self.assertEqual(await target.evaluate("indexedDB.databases().then(dbs=>dbs.map(db=>db.name))"), [])
            self.assertEqual(next(c['value'] for c in await context.cookies() if c['name'] == 'platform-session'), 'updated')
        self.assertEqual(self.homes, 2)
        async with create_browser_context(cookies_path=None) as (_, context):
            self.assertNotIn('platform-session', [c['name'] for c in await context.cookies()])
            login = await context.new_page()
            await login.goto('https://cms.mylearning.cn/catalog')
            await login.evaluate("localStorage.setItem('login-attempt','discard')")
        self.assertEqual(local_cookie, next(c for c in await context.cookies() if c['name'] == 'course_web'))
        async with create_browser_context(self.path) as (_, context):
            login = await context.new_page()
            await login.goto('https://cms.mylearning.cn/catalog')
            self.assertIsNone(await login.evaluate("localStorage.getItem('login-attempt')"))
        self.assertEqual(self.homes, 3)
        await self.page.reload()
        await self.page.get_by_text('已连接 · 控制窗口', exact=True).wait_for()

    async def test_changed_account_mismatch_never_yields_and_leaves_management(self):
        async with create_browser_context(self.path) as (_, context):
            await self.target(context)
        self.save('second', account='center:other')
        entered = False
        with self.assertRaises(CredentialUnavailableError):
            async with create_browser_context(self.path):
                entered = True
        self.assertFalse(entered)
        self.assertIsNone(self.owner.version)
        self.assertNotIn(id(self.context), _sessions)
        self.assertEqual(self.context.pages, [self.page])

    async def test_cancel_manual_collector_detaches_listener_before_next_activity(self):
        started = asyncio.Event()
        async def collect():
            async with create_browser_context(self.path) as (_, context):
                started.set()
                await manual.collect_learning_links_from_entry_urls(['https://cms.mylearning.cn/catalog'], context=context)
        task = asyncio.create_task(collect())
        await asyncio.wait_for(started.wait(), 10)
        async with asyncio.timeout(5):
            while not any(p.url == 'https://cms.mylearning.cn/catalog' for p in self.context.pages):
                await asyncio.sleep(.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.context.pages, [self.page])
        with patch.object(manual, '_wait_for_manual_popup_record_url', new=AsyncMock()) as stale_handler:
            async with create_browser_context(self.path) as (_, context):
                target = await self.target(context)
                await asyncio.sleep(.2)
                self.assertFalse(target.is_closed())
                stale_handler.assert_not_awaited()

    async def test_wrong_loop_rejected_without_launching_second_browser(self):
        async def wrong_loop():
            async with create_browser_context(self.path):
                self.fail('wrong loop obtained browser')
        with self.assertRaisesRegex(RuntimeError, '事件循环'):
            await asyncio.to_thread(lambda: asyncio.run(wrong_loop()))
        self.assertEqual(self.context.pages, [self.page])

    async def test_popup_arriving_during_teardown_is_closed_by_same_lease(self):
        late = []
        async with create_browser_context(self.path) as (_, context):
            target = await self.target(context)
            original_close = target.close
            async def delayed_close():
                late.append(await context.new_page())
                await original_close()
            target.close = delayed_close
        self.assertEqual(len(late), 1)
        self.assertTrue(late[0].is_closed())
        self.assertEqual(context.pages, [self.page])

    async def test_delayed_sso_home_rechecks_account_and_reopens_readonly_resource_once(self):
        opened = 0
        async def delayed(route):
            nonlocal opened
            opened += 1
            body = ("<script>setTimeout(()=>location.href='https://kc.zhixueyun.com/home',50)</script>"
                    if opened == 1 else "<dl class='chapter-list-box'></dl>")
            await route.fulfill(content_type="text/html", body=body)
        await self.context.route('https://kc.zhixueyun.com/delayed', delayed)
        target_url = COURSE.replace('zhixueyun.com/', 'zhixueyun.com/delayed')
        async with create_browser_context(self.path) as (_, context):
            target = await context.new_page()
            await navigate_platform_page(target, target_url, wait_until='domcontentloaded')
            self.assertEqual(target.url, target_url)
            self.assertEqual(opened, 2)
            self.assertEqual(self.profiles, 2)
            self.assertEqual(await target.locator('dl.chapter-list-box').count(), 1)

    async def test_failed_target_blocks_same_lease_and_reinitializes_next_lease(self):
        from core.browser import bootstrap
        async def login_redirect(route):
            await route.fulfill(content_type="text/html", body="<script>location.href='/login'</script>")
        await self.context.route('https://kc.zhixueyun.com/refused', login_redirect)
        url = COURSE.replace('zhixueyun.com/', 'zhixueyun.com/refused')
        async with create_browser_context(self.path) as (_, context):
            target = await context.new_page()
            with patch.object(bootstrap, '_RESOURCE_NAVIGATION_TIMEOUT_SECONDS', .25):
                with self.assertRaises(CredentialUnavailableError):
                    await navigate_platform_page(target, url, wait_until='domcontentloaded')
            self.assertIsNone(self.owner.version)
            with self.assertRaises(CredentialUnavailableError):
                await navigate_platform_page(target, COURSE)
        async with create_browser_context(self.path) as (_, context):
            target = await self.target(context)
            self.assertEqual(target.url, COURSE)
        self.assertEqual(self.homes, 2)
