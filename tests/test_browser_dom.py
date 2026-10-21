"""Offline browser integration using synthetic DOM; no credentials or platform traffic."""
import os
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright, Error
from core.exam.actions import read_existing_answers, select_answers
from core.exam.parsing import extract_multi_questions_data
from core.runtime import close_safely, own_playwright_driver


class BrowserDomTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_option_readback_and_incomplete_question_detection(self):
        manager = async_playwright()
        browser = None
        job = None
        try:
            playwright = await manager.start()
            job = own_playwright_driver(playwright)
            if os.name == "nt":
                self.assertIsNotNone(job, "pinned Playwright driver adapter must expose a pid")
            try:
                browser = await playwright.chromium.launch(headless=True, channel="msedge" if os.name == "nt" else None)
            except Error as exc:
                if "executable" in str(exc).lower() and ("exist" in str(exc).lower() or "distribution" in str(exc).lower()):
                    self.skipTest("Install Edge/Playwright Chromium for offline DOM integration")
                raise
            page = await browser.new_page()
            await page.route("**/*", lambda route: route.abort())
            await page.set_content('''<dl class="preview-list">
                <dd onclick="this.querySelector('input').checked = !this.querySelector('input').checked"><input type="checkbox" checked>A. 甲</dd>
                <dd onclick="this.querySelector('input').checked = !this.querySelector('input').checked"><input type="checkbox">B. 乙</dd>
            </dl>''')
            question = {"type": "multiple", "text": "题干", "options": [{"label": "A", "text": "甲"}, {"label": "B", "text": "乙"}]}
            with patch("core.exam.actions.pause_between", new=AsyncMock()):
                self.assertTrue(await select_answers(page, question, ["A", "B"], "offline"))
                self.assertTrue(await select_answers(page, question, ["A", "B"], "offline"))
            self.assertEqual(await page.locator("input:checked").count(), 2)
            self.assertEqual(await read_existing_answers(page, question), ["A", "B"])
            # Match the live judge-question structure: click target is a span,
            # while the checked radio lives in its containing dd.
            await page.set_content('''<div data-dynamic-key="judge"><dl class="preview-list">
                <dd><input type="radio" checked><span class="pointer">正确</span></dd>
                <dd><input type="radio"><span class="pointer">错误</span></dd>
            </dl></div>''')
            judge = {"type": "judge", "text": "题干", "options": [{"label": "T", "text": "正确"}, {"label": "F", "text": "错误"}], "option_click_selector": ".preview-list dd .pointer"}
            self.assertEqual(await read_existing_answers(page, judge, selector_prefix='[data-dynamic-key="judge"] '), ["T"])
            await page.set_content('''<div class="question-type-item" data-dynamic-key="one"><span class="o-score">单选题（1分）</span><div class="stem-content-main">可读题目</div><dl class="preview-list"><dd>A. 甲</dd><dd>B. 乙</dd></dl></div>
                <div class="question-type-item" data-dynamic-key="two"><span class="o-score">单选题（1分）</span><div class="stem-content-main"></div></div>''')
            self.assertEqual(await extract_multi_questions_data(page), [], "one unreadable question must invalidate the whole paper")
        finally:
            if browser is not None:
                await close_safely(browser.close())
            await close_safely(manager.__aexit__())
            if job is not None:
                await close_safely(job.terminate())


if __name__ == "__main__":
    unittest.main()
