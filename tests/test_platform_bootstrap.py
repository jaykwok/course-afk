from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from core.auth.access import CredentialUnavailableError
from core.auth.credential import AccountProfile
from core.browser import bootstrap as b, session

COURSE="https://kc.zhixueyun.com/#/study/course/detail/11111111-1111-4111-8111-111111111111"


class FakePage:
    def __init__(self,context):
        self.context=context;self.url="about:blank";self.closed=False
        self.close=AsyncMock(side_effect=self._close)
        self.wait_for_url=AsyncMock()
        self.goto=AsyncMock(side_effect=self._goto)
        self.on=lambda *args: None
        self.remove_listener=lambda *args: None
        self.locator=lambda *args: SimpleNamespace(count=AsyncMock(return_value=1))
    def _close(self):self.closed=True
    def _goto(self,url,**kwargs):self.url=url


class FakeContext:
    def __init__(self):self.pages=[]
    async def new_page(self):
        p=FakePage(self);self.pages.append(p);return p


class PlatformBootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        b._sessions.clear()

    def mocks(self,account="center:one"):
        self.enterContext(patch("core.auth.credential.load_credential_metadata",return_value=SimpleNamespace(account_name="center:one")))
        self.profile=self.enterContext(patch("core.auth.credential.extract_account_profile_from_context",new=AsyncMock(return_value=AccountProfile("name",account))))
        self.enterContext(patch("core.browser.overlays.prepare_page_after_navigation_async",new=AsyncMock()))

    async def test_each_new_context_activates_once_then_first_verified_target_closes_home(self):
        self.mocks()
        for _ in range(2):
            c=FakeContext()
            await b.bootstrap_platform_session(c,Path("fake"))
            await b.bootstrap_platform_session(c,Path("fake"))
            home=c.pages[0]
            home.goto.assert_awaited_once()
            home.close.assert_not_awaited()
            p=await c.new_page()
            await b.navigate_platform_page(p,COURSE,wait_until="load")
            home.close.assert_awaited_once()
            self.assertTrue(b.is_session_internal_page(c,c.pages[1]))
            self.assertFalse(b.is_session_internal_page(c,p))
            p2=await c.new_page();await b.navigate_platform_page(p2,COURSE)
            home.close.assert_awaited_once()
            b.release_platform_session(c)
        self.assertEqual(self.profile.await_count,2)

    async def test_mismatched_account_never_opens_business_target(self):
        self.mocks(account="center:two")
        c=FakeContext()
        with self.assertRaises(CredentialUnavailableError):
            await b.bootstrap_platform_session(c,"fake")
        self.assertEqual(len(c.pages),1)
        self.assertNotIn(id(c),b._sessions)

    async def test_authentication_failure_is_batch_blocking_and_preserves_pending(self):
        from core.workflow.errors import error_outcome
        result=error_outcome("task",CredentialUnavailableError("请重新登录"))
        self.assertEqual(result.status,"BLOCKED")
        self.assertEqual(result.stop_scope,"batch")

    async def test_wrong_target_is_not_accepted_or_navigation_replayed(self):
        self.mocks();c=FakeContext();await b.bootstrap_platform_session(c,"fake")
        p=await c.new_page()
        p.goto=AsyncMock(side_effect=lambda *a,**k:None) # stays about:blank
        with patch.object(b,"_RESOURCE_NAVIGATION_TIMEOUT_SECONDS",.02):
            with self.assertRaises(CredentialUnavailableError):
                await b.navigate_platform_page(p,COURSE)
        p.goto.assert_awaited_once()
        c.pages[0].close.assert_not_awaited()

    async def test_saved_credentials_bootstrap_but_explicit_login_and_management_do_not(self):
        for cookies_path in (None,"placeholder"):
            c=FakeContext()
            c.add_cookies=AsyncMock();c.add_init_script=AsyncMock();c.close=AsyncMock()
            browser=SimpleNamespace(new_context=AsyncMock(return_value=c),close=AsyncMock())
            manager=SimpleNamespace(__aenter__=AsyncMock(return_value=object()),__aexit__=AsyncMock())
            with (patch.object(session,"async_playwright",return_value=manager),
                  patch.object(session,"own_playwright_driver",return_value=None),
                  patch.object(session,"launch_async_browser",new=AsyncMock(return_value=browser)),
                  patch.object(session,"load_cookies",return_value=[]),
                  patch.object(b,"bootstrap_platform_session",new=AsyncMock()) as bootstrap):
                async with session.create_browser_context(cookies_path=cookies_path):pass
            self.assertEqual(bootstrap.await_count,int(cookies_path is not None))
            c.close.assert_awaited_once();browser.close.assert_awaited_once()

    def test_resource_identity_includes_subject_source(self):
        self.assertFalse(b._target_matches(COURSE,COURSE.replace("11111111","22222222")))
        self.assertFalse(b._target_matches(COURSE+"/6/1/source",COURSE))
        self.assertTrue(b._target_matches(COURSE,COURSE+"?tracking=1"))

    def test_entry_destination_distinguishes_catalog_routes_from_login_landings(self):
        for url in ("http://127.0.0.1:1234/audit-catalog#/modules/all",
                    "https://portal.example.invalid/catalog?returnUrl=/login",
                    "https://portal.example.invalid/#/catalog?returnUrl=/login",
                    "https://portal.example.invalid/catalog/sso/introduction",
                    "https://portal.example.invalid/catalog/authorization",
                    "https://portal.example.invalid/catalog/login-course"):
            self.assertTrue(b._is_entry_destination(url), url)
        for url in ("about:blank", "https://portal.example.invalid/login",
                    "https://portal.example.invalid/p5/login.html",
                    "https://portal.example.invalid/#/login/code",
                    "https://portal.example.invalid/#/%6cogin/code",
                    "https://sso.example.invalid/auth", "https://passport.example.invalid/"):
            self.assertFalse(b._is_entry_destination(url), url)
