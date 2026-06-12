import unittest
from unittest.mock import AsyncMock, patch


class _FakeNextButton:
    def __init__(self, class_name="next-disabled"):
        self._class_name = class_name
        self.click_calls = 0

    async def get_attribute(self, name):
        if name == "class":
            return self._class_name
        return None

    async def click(self):
        self.click_calls += 1


class _FakePage:
    def __init__(self, next_button=None):
        self._next_button = next_button or _FakeNextButton()

    async def wait_for_load_state(self, _state):
        return None

    async def wait_for_timeout(self, _milliseconds):
        return None

    async def wait_for_event(self, _event, timeout=0):
        return None

    def locator(self, selector):
        if selector == ".single-btn-next":
            return self._next_button
        raise KeyError(selector)


class _FakeLocatorWithCount:
    def __init__(self, *, count=0, click_calls=None):
        self._count = count
        self._click_calls = click_calls if click_calls is not None else []

    @property
    def last(self):
        return self

    async def count(self):
        return self._count

    async def click(self):
        self._click_calls.append("clicked")


class _FakeManualSubmitPage:
    def __init__(self, close_button_count=1):
        self.click_calls = []
        self.waits = []
        self._close_button = _FakeLocatorWithCount(
            count=close_button_count,
            click_calls=self.click_calls,
        )

    def is_closed(self):
        return False

    def locator(self, selector):
        if selector == "[data-region='modal:modal'] .btn.white.border:has-text('确定')":
            return self._close_button
        raise KeyError(selector)

    async def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)


class _FakePopupStartButton:
    def __init__(self):
        self.click_calls = 0

    async def click(self):
        self.click_calls += 1


class _FakePopupContext:
    def __init__(self, popup):
        self.value = AsyncMock(return_value=popup)()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePopupLauncherPage:
    def __init__(self, popup):
        self.url = "https://example.com/course"
        self._popup = popup
        self.start_button = _FakePopupStartButton()

    def expect_popup(self):
        return _FakePopupContext(self._popup)

    def locator(self, selector):
        if selector == ".btn.new-radius":
            return self.start_button
        raise KeyError(selector)


class _FakeClosedPopupPage:
    def __init__(self):
        self.wait_for_event = AsyncMock(return_value=None)

    def is_closed(self):
        return True


class ExamFlowLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_ai_exam_logs_single_question_options_for_frontend_display(self):
        from core.exam_flow import ai_exam

        question_data = {
            "type": "single",
            "text": "测试单题",
            "options": [
                {"label": "A", "text": "选项一"},
                {"label": "B", "text": "选项二"},
            ],
        }
        page = _FakePage()

        with (
            patch("core.exam_flow.close_exam_notice_if_present", new=AsyncMock()),
            patch("core.exam_flow.detect_exam_mode", new=AsyncMock(return_value="single")),
            patch(
                "core.exam_flow.extract_single_question_data",
                new=AsyncMock(return_value=question_data),
            ),
            patch("core.exam_flow.get_ai_answers", new=AsyncMock(return_value=["A"])),
            patch("core.exam_flow.select_answers", new=AsyncMock()),
            patch("core.exam_flow.submit_exam", new=AsyncMock()),
            patch("core.exam_flow.logging.info") as mock_info,
        ):
            await ai_exam(object(), "test-model", page, "https://example.com/exam")

        mock_info.assert_any_call("当前题目: 测试单题")
        mock_info.assert_any_call("题目选项:\nA. 选项一\nB. 选项二")

    async def test_ai_exam_logs_multi_question_options_for_frontend_display(self):
        from core.exam_flow import ai_exam

        question_data = {
            "index": 0,
            "item_id": "item-1",
            "type": "single",
            "text": "测试多题",
            "options": [
                {"label": "A", "text": "甲"},
                {"label": "B", "text": "乙"},
            ],
        }
        page = _FakePage()

        with (
            patch("core.exam_flow.close_exam_notice_if_present", new=AsyncMock()),
            patch("core.exam_flow.detect_exam_mode", new=AsyncMock(return_value="multi")),
            patch(
                "core.exam_flow.extract_multi_questions_data",
                new=AsyncMock(return_value=[question_data]),
            ),
            patch("core.exam_flow.get_ai_answers", new=AsyncMock(return_value=["A"])),
            patch("core.exam_flow.select_answers", new=AsyncMock()),
            patch(
                "core.exam_flow._wait_for_manual_submit_completion",
                new=AsyncMock(),
            ),
            patch("core.exam_flow.logging.info") as mock_info,
        ):
            await ai_exam(
                object(),
                "test-model",
                page,
                "https://example.com/exam",
                auto_submit=False,
            )

        mock_info.assert_any_call("处理题目 1: 测试多题")
        mock_info.assert_any_call("题目 1 选项:\nA. 甲\nB. 乙")

    async def test_ai_exam_disables_auto_submit_for_single_fill_blank_question(self):
        from core.exam_flow import ai_exam

        question_data = {
            "type": "fill_blank",
            "text": "测试填空题",
            "options": [],
        }
        page = _FakePage()

        with (
            patch("core.exam_flow.close_exam_notice_if_present", new=AsyncMock()),
            patch("core.exam_flow.detect_exam_mode", new=AsyncMock(return_value="single")),
            patch(
                "core.exam_flow.extract_single_question_data",
                new=AsyncMock(return_value=question_data),
            ),
            patch("core.exam_flow.get_ai_answers", new=AsyncMock(return_value=[])),
            patch("core.exam_flow.select_answers", new=AsyncMock()),
            patch("core.exam_flow.submit_exam", new=AsyncMock()) as mock_submit_exam,
            patch(
                "core.exam_flow._wait_for_manual_submit_completion",
                new=AsyncMock(),
            ) as mock_wait_manual_submit,
            patch("core.exam_flow.logging.info") as mock_info,
        ):
            await ai_exam(object(), "test-model", page, "https://example.com/exam", auto_submit=True)

        mock_submit_exam.assert_not_awaited()
        mock_wait_manual_submit.assert_awaited_once_with(page)
        mock_info.assert_any_call("检测到需要人工处理的题目，已自动切换为手动交卷")

    async def test_ai_exam_disables_auto_submit_for_multi_question_without_valid_answers(self):
        from core.exam_flow import ai_exam

        question_data = {
            "index": 0,
            "item_id": "item-1",
            "type": "single",
            "text": "测试多题",
            "options": [
                {"label": "A", "text": "甲"},
                {"label": "B", "text": "乙"},
            ],
        }
        page = _FakePage()

        with (
            patch("core.exam_flow.close_exam_notice_if_present", new=AsyncMock()),
            patch("core.exam_flow.detect_exam_mode", new=AsyncMock(return_value="multi")),
            patch(
                "core.exam_flow.extract_multi_questions_data",
                new=AsyncMock(return_value=[question_data]),
            ),
            patch("core.exam_flow.get_ai_answers", new=AsyncMock(return_value=[])),
            patch("core.exam_flow.select_answers", new=AsyncMock()),
            patch("core.exam_flow.submit_exam", new=AsyncMock()) as mock_submit_exam,
            patch(
                "core.exam_flow._wait_for_manual_submit_completion",
                new=AsyncMock(),
            ) as mock_wait_manual_submit,
            patch("core.exam_flow.logging.info") as mock_info,
        ):
            await ai_exam(object(), "test-model", page, "https://example.com/exam", auto_submit=True)

        mock_submit_exam.assert_not_awaited()
        mock_wait_manual_submit.assert_awaited_once_with(page)
        mock_info.assert_any_call("检测到需要人工处理的题目，已自动切换为手动交卷")

    async def test_wait_for_manual_submit_completion_closes_result_modal_when_present(self):
        from core.exam_flow import _wait_for_manual_submit_completion

        page = _FakeManualSubmitPage(close_button_count=1)

        await _wait_for_manual_submit_completion(page)

        self.assertEqual(page.click_calls, ["clicked"])
        self.assertEqual(page.waits, [500])

    async def test_wait_for_finish_test_does_not_wait_for_close_when_popup_already_closed(self):
        from core.exam_flow import wait_for_finish_test

        client = object()
        popup = _FakeClosedPopupPage()
        page = _FakePopupLauncherPage(popup)

        with patch("core.exam_flow.ai_exam", new=AsyncMock(return_value=None)) as mock_ai_exam:
            await wait_for_finish_test(
                client,
                "test-model",
                page,
                auto_submit=False,
            )

        mock_ai_exam.assert_awaited_once_with(
            client,
            "test-model",
            popup,
            page.url,
            auto_submit=False,
            ai_model_config=None,
        )
        popup.wait_for_event.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
