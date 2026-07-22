import unittest
from unittest.mock import patch


class _FakeClickTarget:
    def __init__(self, clicks, error=None):
        self._clicks = clicks
        self._error = error

    @property
    def first(self):
        return self

    async def click(self, timeout=0):
        if self._error:
            raise self._error
        self._clicks.append(timeout)


class _FakeSubmitClickTarget:
    def __init__(self, selector, click_calls):
        self._selector = selector
        self._click_calls = click_calls

    @property
    def last(self):
        return self

    async def click(self, timeout=0):
        self._click_calls.append((self._selector, timeout))

    async def count(self):
        return 1


class _FakeCollectionLocator:
    def __init__(self, selector, calls, error=None):
        self._selector = selector
        self._calls = calls
        self._error = error

    def nth(self, index):
        self._calls.append((self._selector, index))
        return _FakeClickTarget([], error=self._error)


class _FakePage:
    def __init__(self, click_error=None):
        self.locator_calls = []
        self._click_error = click_error

    def locator(self, selector):
        self.locator_calls.append(selector)
        return _FakeCollectionLocator(
            selector,
            self.locator_calls,
            error=self._click_error,
        )

    async def wait_for_timeout(self, _milliseconds):
        return None


class _FakeSubmitPage:
    def __init__(self):
        self.locator_calls = []
        self.click_calls = []

    def locator(self, selector):
        self.locator_calls.append(selector)
        return _FakeSubmitClickTarget(selector, self.click_calls)

    async def wait_for_timeout(self, _milliseconds):
        return None


class ExamActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_select_answers_routes_empty_choice_answers_without_claiming_fill_blank(self):
        from core.exam.actions import MANUAL_EXAM_FILE, select_answers

        question_data = {
            "index": 0,
            "type": "single",
            "text": "示例公司的英文缩写是什么？",
            "options": [
                {"label": "A", "text": "CT"},
                {"label": "B", "text": "CU"},
            ],
        }

        with (
            patch("core.exam.actions.logging.info") as mock_info,
            patch("core.exam.actions.append_manual_exam_entry") as mock_append_manual,
        ):
            result = await select_answers(
                object(),
                question_data,
                [],
                "https://example.com/exam",
            )

        self.assertFalse(result)
        mock_append_manual.assert_called_once_with(
            "https://example.com/exam",
            reason="ai_no_answer",
            reason_text="没有获取到有效答案, 可能是 AI 作答失败或题目解析不完整",
            ai_failed_model_config=None,
            file_path=MANUAL_EXAM_FILE,
        )
        messages = [call.args[0] for call in mock_info.call_args_list]
        self.assertTrue(any("没有获取到有效答案" in message for message in messages))
        self.assertFalse(any("填空" in message for message in messages))

    async def test_select_answers_uses_option_click_selector_when_present(self):
        from core.exam.actions import select_answers

        page = _FakePage()
        question_data = {
            "index": 0,
            "type": "single",
            "text": "示例公司的英文缩写是什么？",
            "options": [
                {"label": "A", "text": "CT"},
                {"label": "B", "text": "CU"},
            ],
            "option_click_selector": ".option-item",
        }

        result = await select_answers(
            page,
            question_data,
            ["B"],
            "https://example.com/exam",
            selector_prefix="[data-dynamic-key='item-1'] ",
        )

        self.assertTrue(result)
        self.assertIn(
            "[data-dynamic-key='item-1'] .option-item",
            page.locator_calls,
        )
        self.assertIn(
            ("[data-dynamic-key='item-1'] .option-item", 1),
            page.locator_calls,
        )

    async def test_select_answers_returns_false_when_click_fails(self):
        from core.exam.actions import select_answers

        page = _FakePage(click_error=RuntimeError("click failed"))
        question_data = {
            "index": 0,
            "type": "single",
            "text": "示例公司的英文缩写是什么？",
            "options": [
                {"label": "A", "text": "CT"},
                {"label": "B", "text": "CU"},
            ],
        }

        result = await select_answers(
            page,
            question_data,
            ["A"],
            "https://example.com/exam",
        )

        self.assertFalse(result)

    async def test_select_answers_raises_user_abort_when_exam_was_auto_submitted(self):
        from core.abort import UserAbortRequested
        from core.exam.actions import select_answers

        page = _FakePage(
            click_error=RuntimeError("您好，已超过考试时长，考试已自动提交")
        )
        question_data = {
            "index": 0,
            "type": "single",
            "text": "示例公司的英文缩写是什么？",
            "options": [
                {"label": "A", "text": "CT"},
                {"label": "B", "text": "CU"},
            ],
        }

        with self.assertRaises(UserAbortRequested) as ctx:
            await select_answers(
                page,
                question_data,
                ["A"],
                "https://example.com/exam",
                selector_prefix="[data-dynamic-key='item-1'] ",
            )

        self.assertIn("自动交卷", str(ctx.exception))

    async def test_submit_exam_uses_modal_close_button_instead_of_broad_text_selector(self):
        from core.exam.actions import submit_exam

        page = _FakeSubmitPage()

        await submit_exam(page)

        self.assertEqual(
            page.locator_calls,
            [
                "text=我要交卷",
                "button:has-text('确 定')",
                "[data-region='modal:modal'] .btn.white.border:has-text('确定')",
            ],
        )
        self.assertNotIn("text=确定", page.locator_calls)


if __name__ == "__main__":
    unittest.main()
