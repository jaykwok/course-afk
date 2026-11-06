"""Visible offline UI acceptance; all business operations are synthetic."""
import asyncio
import json
from pathlib import Path
import unittest

from core.app.runtime import ApplicationRuntime
from core.browser.session import create_browser_context
from core.web.server import LocalServer


class VisibleWebTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_page_refresh_prompts_readonly_tab_cancel_and_exit(self):
        answers=[]
        def dispatcher(kind,ui):
            if kind=="links":
                value=ui.prompt_multiline_input(["请输入测试链接"],title="测试添加链接")
                answers.append(value)
                if ui.prompt_summary_confirmation("解析确认",[("链接数","1")],default="N"):
                    answers.append(ui.prompt_choice("选择方式",["全部解析","手动选择"]))
                ui.show_summary("解析结果",[("状态","模拟完成 <script>unsafe</script>")])
                ui.pause()
            elif kind=="exam":
                answers.append(ui.prompt_yes_no("AI考试是否自动交卷？",default="N"))
                ui.pause()
            else:
                ui.pause()
        runtime=ApplicationRuntime(dispatcher=dispatcher);runtime.start()
        server=LocalServer(runtime);await server.start()
        errors=[]
        try:
            async with create_browser_context(cookies_path=None) as (_,context):
                page=await context.new_page()
                page.on("pageerror",lambda exc:errors.append(str(exc)))
                page.on("close",lambda:runtime.request_exit(graceful=True))
                # Block every external request, including accidental platform I/O.
                await context.route("**/*",lambda route:route.continue_() if route.request.url.startswith(server.origin+"/") else route.abort())
                await page.set_viewport_size({"width":1365,"height":1024})
                await page.goto(server.launch_url)
                await page.get_by_text("已连接 · 控制窗口",exact=True).wait_for()
                self.assertEqual(await page.locator("[data-kind]").count(),6)
                await page.locator('[data-kind="links"]').click()
                await page.locator("#link-input").fill("https://example.invalid/course")
                # Refresh preserves the pending prompt; no duplicate operation.
                operation=runtime.control.operation_id
                prompt=runtime.session.snapshot()["prompt"]["id"]
                await page.reload()
                await page.get_by_text("已连接 · 控制窗口",exact=True).wait_for()
                await page.locator("#link-input").wait_for()
                self.assertEqual(runtime.control.operation_id,operation)
                self.assertEqual(runtime.session.snapshot()["prompt"]["id"],prompt)
                await page.locator("#link-input").fill("https://example.invalid/course")
                await page.locator("#prompt").get_by_role("button",name="继续",exact=True).click()
                await page.get_by_role("heading",name="解析确认",exact=True).wait_for()
                await page.locator("#prompt").get_by_role("button",name="确认",exact=True).click()
                await page.locator("#prompt").get_by_role("button",name="全部解析",exact=True).click()
                await page.locator("#summary").get_by_text("模拟完成 <script>unsafe</script>",exact=True).wait_for()
                self.assertEqual(await page.locator("#summary script").count(),0)
                output=Path(__file__).resolve().parents[1]/"build/ci/web-desktop.png"
                output.parent.mkdir(parents=True,exist_ok=True)
                await page.screenshot(path=str(output),full_page=True)
                # A second page with the same session cookie gets snapshots only.
                reader=await context.new_page()
                # Browser tab duplication may copy sessionStorage. The tab lock
                # must still keep this copied control identity read-only.
                copied=await page.evaluate("({client:sessionStorage.getItem('client'),csrf:sessionStorage.getItem('csrf')})")
                await reader.add_init_script("sessionStorage.setItem('client',"+json.dumps(copied["client"])+");sessionStorage.setItem('csrf',"+json.dumps(copied["csrf"])+");")
                await reader.goto(server.origin)
                await reader.get_by_text("已连接 · 只读",exact=True).wait_for()
                self.assertFalse(await reader.locator('[data-kind="learn"]').is_enabled())
                await reader.close()
                self.assertFalse(runtime.control.exit_requested)
                self.assertTrue(runtime.connected)
                await page.locator("#prompt").get_by_role("button",name="返回总览",exact=True).click()
                await page.locator('[data-kind="exam"]').click()
                await page.get_by_text("AI考试是否自动交卷？",exact=True).wait_for()
                self.assertFalse(runtime.session.snapshot()["prompt"]["default"])
                await page.locator("#prompt").get_by_role("button",name="不继续",exact=True).click()
                await page.locator("#prompt").get_by_role("button",name="返回总览",exact=True).click()
                await page.locator('[data-kind="references"]').click()
                await page.locator("#prompt").get_by_role("button",name="返回总览",exact=True).wait_for()
                await page.locator("#cancel").click()
                await page.locator('[data-kind="learn"]').wait_for(state="visible")
                async with asyncio.timeout(4):
                    while runtime.control.operation_id:await asyncio.sleep(.02)
                await page.set_viewport_size({"width":390,"height":844})
                await page.screenshot(path=str(output.with_name("web-mobile.png")),full_page=True)
                self.assertFalse(await page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                await page.locator("#exit").click()
                async with asyncio.timeout(3):
                    while not runtime.control.exit_requested:await asyncio.sleep(.02)
                self.assertEqual(answers,["https://example.invalid/course",1,False])
                self.assertEqual(errors,[])
        finally:
            await runtime.close();await server.close()

    async def test_native_management_close_finishes_only_current_learning_activity(self):
        import threading
        started=threading.Event();finish=threading.Event();calls=[]
        def dispatcher(kind,ui):
            started.set();finish.wait(5)
            calls.append("current-finished")
            if ui.control.can_dispatch():calls.append("next-started")
        runtime=ApplicationRuntime(dispatcher=dispatcher);runtime.start()
        server=LocalServer(runtime);await server.start()
        try:
            async with create_browser_context(cookies_path=None) as (_,context):
                page=await context.new_page()
                page.on("close",lambda:runtime.request_exit(graceful=True))
                await page.goto(server.launch_url)
                await page.get_by_text("已连接 · 控制窗口",exact=True).wait_for()
                await page.locator('[data-kind="learn"]').click()
                async with asyncio.timeout(3):
                    while not started.is_set():await asyncio.sleep(.02)
                # Visibility/background changes never signal exit.
                await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
                self.assertFalse(runtime.control.exit_requested)
                await page.close()
                self.assertTrue(runtime.control.exit_requested)
                self.assertFalse(runtime.control.cancelled)
                finish.set()
                async with asyncio.timeout(3):
                    while runtime.control.operation_id:await asyncio.sleep(.02)
                self.assertEqual(calls,["current-finished"])
        finally:
            finish.set();await runtime.close();await server.close()
