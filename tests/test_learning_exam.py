import unittest
from unittest.mock import AsyncMock, patch


class FakeLocator:
    def __init__(self, *, count=1, text="", wait_error=None, children=None):
        self._count = count
        self._text = text
        self._wait_error = wait_error
        self._children = children or {}

    async def count(self):
        return self._count

    async def wait_for(self, state="visible", timeout=0):
        if self._wait_error:
            raise self._wait_error

    async def inner_text(self, timeout=None):
        return self._text

    def locator(self, selector):
        return self._children[selector]


class FakePage:
    def __init__(self, locators):
        self._locators = locators

    async def wait_for_timeout(self, _milliseconds):
        return None

    def locator(self, selector):
        return self._locators[selector]


class FakeTextLocator:
    def __init__(self, text=""):
        self._text = text

    @property
    def last(self):
        return self

    async def wait_for(self, state="visible", timeout=0):
        return None

    async def inner_text(self, timeout=None):
        return self._text

    async def click(self):
        return None


class FakeChapterBox:
    def __init__(self, section_type="9", box_text="第四章节: 息壤慧政智能体测试"):
        self._section_type = section_type
        self._box_text = box_text

    async def get_attribute(self, name):
        if name == "data-sectiontype":
            return self._section_type
        return None

    def locator(self, selector):
        if selector == ".text-overflow":
            return FakeTextLocator(text=self._box_text)
        if selector == ".section-item-wrapper":
            return FakeTextLocator()
        raise KeyError(selector)


class FakeChapterListLocator:
    def __init__(self, boxes):
        self._boxes = boxes

    @property
    def last(self):
        return self

    async def wait_for(self, state="visible", timeout=0):
        return None

    async def count(self):
        return len(self._boxes)

    def nth(self, index):
        return self._boxes[index]


class FakeCoursePage:
    def __init__(self):
        self.main_frame = object()
        self.url = "https://kc.zhixueyun.com/#/study/course/detail/test-course"
        self._chapter_list = FakeChapterListLocator([FakeChapterBox()])

    async def wait_for_load_state(self, _state):
        return None

    def locator(self, selector):
        if selector == "dl.chapter-list-box.required":
            return self._chapter_list
        if selector == "span.course-title-text":
            return FakeTextLocator(text="测试课程")
        raise KeyError(selector)


class FakeListTextLocator:
    def __init__(self, texts=None):
        self._texts = texts or []

    async def all_inner_texts(self):
        return self._texts


class FakeSubjectExamItem:
    def __init__(self, status_texts=None):
        self._status_texts = status_texts or []

    def locator(self, selector):
        if selector == "span.finished-status":
            return FakeListTextLocator(self._status_texts)
        raise KeyError(selector)


class LearningExamTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_exam_passed_treats_go_exam_row_as_pending_without_error(self):
        from core.learning_exam import check_exam_passed

        page = FakePage(
            {
                ".neer-status": FakeLocator(count=0),
                "div.tab-container table.table": FakeLocator(count=1),
                "div.tab-container table.table tbody tr:first-child": FakeLocator(
                    count=1,
                    text="第1次  去考试",
                    children={
                        "td:nth-child(4)": FakeLocator(
                            count=0,
                            wait_error=RuntimeError(
                                "Locator.wait_for: Timeout 1500ms exceeded."
                            ),
                        )
                    },
                ),
            }
        )

        with patch("core.learning_exam.logging.error") as mock_error:
            result = await check_exam_passed(page)

        self.assertFalse(result)
        mock_error.assert_not_called()

    async def test_check_exam_passed_treats_no_exam_record_row_as_pending_without_error(self):
        from core.learning_exam import check_exam_passed

        page = FakePage(
            {
                ".neer-status": FakeLocator(count=0),
                "div.tab-container table.table": FakeLocator(count=1),
                "div.tab-container table.table tbody tr:first-child": FakeLocator(
                    count=1,
                    text="暂无考试记录",
                    children={
                        "td:nth-child(4)": FakeLocator(
                            count=1,
                            wait_error=RuntimeError(
                                "Locator.wait_for: Timeout 1500ms exceeded."
                            ),
                        )
                    },
                ),
            }
        )

        with (
            patch("core.learning_exam.logging.info") as mock_info,
            patch("core.learning_exam.logging.error") as mock_error,
        ):
            result = await check_exam_passed(page)

        self.assertFalse(result)
        mock_error.assert_not_called()
        mock_info.assert_any_call("考试状态: 暂无考试记录")

    async def test_course_learning_checks_exam_status_only_once_for_exam_section(self):
        from core.learning_flows import course_learning

        page = FakeCoursePage()
        mock_check = AsyncMock(return_value=False)

        with (
            patch("core.learning_flows.check_permission", new=AsyncMock(return_value=True)),
            patch("core.learning_flows.handle_rating_popup", new=AsyncMock(return_value=False)),
            patch("core.learning_flows._is_course_completed", new=AsyncMock(return_value=False)),
            patch("core.learning_flows.check_exam_passed", new=mock_check),
            patch("core.learning_exam.check_exam_passed", new=mock_check),
            patch("core.learning_exam.append_exam_url"),
        ):
            await course_learning(page)

        self.assertEqual(mock_check.await_count, 1)

    async def test_handle_subject_exam_item_saves_exam_paper_url_for_pending_exam(self):
        from core.learning_flows import handle_subject_exam_item

        learn_item = FakeSubjectExamItem()
        exam_url = "https://kc.zhixueyun.com/#/exam/exam/answer-paper/subject-exam"

        with (
            patch("core.learning_flows.get_course_url", new=AsyncMock(return_value=exam_url)),
            patch("core.learning_flows.append_exam_url") as mock_append,
        ):
            result = await handle_subject_exam_item(learn_item)

        self.assertEqual(result, exam_url)
        mock_append.assert_called_once_with(exam_url)

    async def test_handle_subject_exam_item_skips_completed_exam(self):
        from core.learning_flows import handle_subject_exam_item

        learn_item = FakeSubjectExamItem(status_texts=["已完成"])

        with (
            patch("core.learning_flows.get_course_url", new=AsyncMock()),
            patch("core.learning_flows.append_exam_url") as mock_append,
        ):
            result = await handle_subject_exam_item(learn_item)

        self.assertIsNone(result)
        mock_append.assert_not_called()


if __name__ == "__main__":
    unittest.main()
