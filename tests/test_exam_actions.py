import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from core.abort import UserCancelRequested
from core.exam.actions import read_existing_answers, select_answers, submit_exam


def question(kind="single", labels=("A", "B")):
    return {"type": kind, "text": "测试题干", "options": [{"label": label, "text": label} for label in labels]}


class Option:
    def __init__(self, page, index):
        self.page, self.index = page, index

    async def evaluate(self, _script):
        return self.index in self.page.selected

    async def click(self, **_kwargs):
        if self.page.error:
            raise self.page.error
        self.page.clicked.append(self.index)
        if self.page.ignore_click:
            return
        if self.page.multiple:
            self.page.selected.symmetric_difference_update({self.index})
        else:
            self.page.selected = {self.index}


class Options:
    def __init__(self, page):
        self.page = page

    async def count(self):
        return self.page.count

    def nth(self, index):
        return Option(self.page, index)

    async def input_value(self):
        return self.page.value


class Page:
    def __init__(self, *, multiple=False, selected=(), count=2, error=None, ignore_click=False):
        self.multiple = multiple
        self.selected = set(selected)
        self.count, self.error, self.ignore_click = count, error, ignore_click
        self.clicked, self.selectors = [], []
        self.value = ""
        self.ignore_fill = False

    def locator(self, selector):
        self.selectors.append(selector)
        return Options(self)

    async def wait_for_timeout(self, _ms):
        await asyncio.sleep(0)

    async def fill(self, _selector, value):
        if not self.ignore_fill:
            self.value = value


class ExamActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_answer_readback_does_not_modify_any_options(self):
        page = Page(multiple=True, selected={0, 1})
        self.assertEqual(await read_existing_answers(page, question("multiple")), ["A", "B"])
        self.assertEqual(page.clicked, [])
        self.assertIsNone(await read_existing_answers(Page(count=1), question()))

    async def test_no_answer_review_identifies_question_and_concrete_failure(self):
        data = {**question(), "index": 6, "answer_failure_reason": "AI 响应未完成（AiOutputError）"}
        with patch("core.exam.actions.append_manual_exam_entry") as append:
            self.assertFalse(await select_answers(object(), data, [], "exam"))
        message = append.call_args.kwargs["reason_text"]
        self.assertIn("第 7 题", message)
        self.assertIn("AI 响应未完成", message)

    async def asyncSetUp(self):
        self.pause = patch("core.exam.actions.pause_between", new=AsyncMock())
        self.pause.start()
        self.addCleanup(self.pause.stop)

    async def test_empty_answers_route_to_manual_without_recording_model_failure(self):
        with patch("core.exam.actions.append_manual_exam_entry") as append:
            self.assertFalse(await select_answers(object(), question(), [], "exam"))
        self.assertEqual(append.call_args.kwargs["reason"], "ai_no_answer")
        self.assertIsNone(append.call_args.kwargs["ai_failed_model_config"])

    async def test_multiple_selection_applies_diff_and_is_idempotent(self):
        page = Page(multiple=True, selected={0})
        self.assertTrue(await select_answers(page, question("multiple"), ["A", "B"], "exam"))
        self.assertEqual(page.selected, {0, 1})
        self.assertEqual(page.clicked, [1])
        self.assertTrue(await select_answers(page, question("multiple"), ["A", "B"], "exam"))
        self.assertEqual(page.clicked, [1])

    async def test_multiple_selection_clears_extraneous_answers(self):
        page = Page(multiple=True, selected={0, 1})
        self.assertTrue(await select_answers(page, question("multiple"), ["B"], "exam"))
        self.assertEqual(page.selected, {1})
        self.assertEqual(page.clicked, [0])

    async def test_labels_are_resolved_in_display_order(self):
        page = Page(selected={1})
        data = {**question(labels=("B", "A")), "option_click_selector": ".option-item"}
        self.assertTrue(await select_answers(page, data, ["B"], "exam", selector_prefix="#question "))
        self.assertEqual(page.clicked, [0])
        self.assertEqual(page.selected, {0})
        self.assertIn("#question .option-item", page.selectors)

    async def test_radio_already_correct_is_not_toggled(self):
        page = Page(selected={0})
        self.assertTrue(await select_answers(page, question(), ["A"], "exam"))
        self.assertEqual(page.clicked, [])

    async def test_dom_count_mismatch_never_clicks(self):
        page = Page(count=1)
        self.assertFalse(await select_answers(page, question(), ["A"], "exam"))
        self.assertEqual(page.clicked, [])

    async def test_ignored_click_is_not_reported_as_success(self):
        page = Page(ignore_click=True)
        self.assertFalse(await select_answers(page, question(), ["A"], "exam"))

    async def test_ordering_requires_full_permutation_and_readback(self):
        page = Page()
        self.assertTrue(await select_answers(page, question("ordering"), ["B", "A"], "exam"))
        self.assertEqual(page.value, "BA")
        page.ignore_fill = True
        self.assertFalse(await select_answers(page, question("ordering"), ["A", "B"], "exam"))
        with patch("core.exam.actions.append_manual_exam_entry"):
            self.assertFalse(await select_answers(page, question("ordering"), ["A"], "exam"))

    async def test_click_failure_is_unverified(self):
        self.assertFalse(await select_answers(Page(error=RuntimeError("click failed")), question(), ["A"], "exam"))

    async def test_platform_auto_submission_stops_for_reconciliation(self):
        page = Page(error=RuntimeError("您好，已超过考试时长，考试已自动提交"))
        with self.assertRaisesRegex(UserCancelRequested, "自动交卷"):
            await select_answers(page, question(), ["A"], "exam")

    async def test_cancellation_is_not_converted_to_invalid_answer(self):
        with self.assertRaises(asyncio.CancelledError):
            await select_answers(Page(error=asyncio.CancelledError()), question(), ["A"], "exam")

    async def test_submission_requires_visible_result_receipt(self):
        calls = []
        class Target:
            @property
            def last(self):
                return self
            async def click(self):
                calls.append(("click", self.selector))
            async def wait_for(self, **kwargs):
                calls.append(("wait", self.selector, kwargs))
        class SubmitPage:
            def locator(self, selector):
                target = Target()
                target.selector = selector
                return target
        await submit_exam(SubmitPage())
        self.assertEqual([call[0] for call in calls], ["click", "click", "wait", "click"])
        self.assertIn("modal:modal", calls[-1][1])
        self.assertEqual(calls[-2][2], {"state": "visible", "timeout": 30000})

    async def test_missing_result_receipt_propagates(self):
        target = AsyncMock()
        target.last = target
        target.wait_for.side_effect = TimeoutError
        class SubmitPage:
            def locator(self, _selector):
                return target
        with self.assertRaises(TimeoutError):
            await submit_exam(SubmitPage())


if __name__ == "__main__":
    unittest.main()
