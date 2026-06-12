import unittest


class FakeLocator:
    def __init__(self, *, text=None, children=None, items=None):
        self._text = text
        self._children = children or {}
        self._items = items

    async def count(self):
        if self._items is not None:
            return len(self._items)
        return 1 if self._text is not None or self._children else 0

    def nth(self, index):
        return self._items[index]

    def locator(self, selector):
        return self._children.get(selector, FakeLocator(items=[]))

    async def inner_text(self):
        if self._text is None:
            raise AssertionError("inner_text() called on locator without text")
        return self._text


class QuestionParserTests(unittest.IsolatedAsyncioTestCase):
    async def test_extract_options_with_selector_prefers_existing_preview_list_structure(self):
        from core.question_parser import extract_options_with_selector

        locator = FakeLocator(
            children={
                ".preview-list dd": FakeLocator(
                    items=[
                        FakeLocator(
                            children={
                                ".option-num": FakeLocator(text="A."),
                                ".answer-options": FakeLocator(text="选项一"),
                            }
                        ),
                        FakeLocator(
                            children={
                                ".option-num": FakeLocator(text="B."),
                                ".answer-options": FakeLocator(text="选项二"),
                            }
                        ),
                    ]
                ),
            }
        )

        options, click_selector = await extract_options_with_selector(locator, "single")

        self.assertEqual(
            options,
            [
                {"label": "A", "text": "选项一"},
                {"label": "B", "text": "选项二"},
            ],
        )
        self.assertEqual(click_selector, ".preview-list dd")

    async def test_extract_options_with_selector_falls_back_to_option_item_structure(self):
        from core.question_parser import extract_options_with_selector

        locator = FakeLocator(
            children={
                ".preview-list dd": FakeLocator(items=[]),
                ".option-item": FakeLocator(
                    items=[
                        FakeLocator(
                            children={
                                ".label": FakeLocator(text="A."),
                                ".content": FakeLocator(text="备选项一"),
                            }
                        ),
                        FakeLocator(
                            children={
                                ".label": FakeLocator(text="B."),
                                ".content": FakeLocator(text="备选项二"),
                            }
                        ),
                    ]
                ),
            }
        )

        options, click_selector = await extract_options_with_selector(locator, "single")

        self.assertEqual(
            options,
            [
                {"label": "A", "text": "备选项一"},
                {"label": "B", "text": "备选项二"},
            ],
        )
        self.assertEqual(click_selector, ".option-item")


if __name__ == "__main__":
    unittest.main()
