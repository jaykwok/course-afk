import unittest
from unittest.mock import AsyncMock, MagicMock

from core.learning.popups import handle_archive_continue_popup, handle_rating_popup


class _FakeLocator:
    def __init__(
        self,
        *,
        visible=True,
        count=1,
        inner_text="",
        click=None,
        children=None,
    ):
        self._visible = visible
        self._count = count
        self._inner_text = inner_text
        self._click = click or AsyncMock()
        self._children = children or {}
        self.first = self

    def filter(self, **kwargs):
        return self

    def locator(self, selector):
        if selector in self._children:
            return self._children[selector]
        return self

    def get_by_text(self, text, exact=False):
        return self._children.get(f"text:{text}", _FakeLocator(count=0, visible=False))

    async def wait_for(self, state="visible", timeout=0):
        if state == "visible" and not self._visible:
            raise TimeoutError("not visible")
        if state == "hidden":
            return None
        return None

    async def count(self):
        return self._count

    async def inner_text(self, timeout=None):
        return self._inner_text

    async def click(self):
        return await self._click()


class ArchivePopupTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_archive_transition_page_clicks_continue(self):
        """主路径：.study-transition-page 上的 div.btn「继续学习」。"""
        click = AsyncMock()
        continue_btn = _FakeLocator(visible=True, count=1, click=click)
        transition = _FakeLocator(
            visible=True,
            count=1,
            children={"div.btn": continue_btn},
        )
        # ant-modal 路径不应被调用到 wait（transition 已成功）
        ant = _FakeLocator(visible=False, count=0)

        def locator(sel):
            if "study-transition-page" in sel:
                return transition
            if "ant-modal" in sel:
                return ant
            if sel == "[id$='goOnStudy']":
                return _FakeLocator(count=0)
            return _FakeLocator(count=0)

        page = MagicMock()
        page.locator = MagicMock(side_effect=locator)
        page.get_by_role = MagicMock(return_value=_FakeLocator(count=0))
        page.wait_for_timeout = AsyncMock()
        page.wait_for_load_state = AsyncMock()

        handled = await handle_archive_continue_popup(page)

        self.assertTrue(handled)
        click.assert_awaited()

    async def test_handle_archive_ant_modal_clicks_continue_learning(self):
        """兼容路径：ant-modal 弹窗。"""
        click = AsyncMock()
        dialog = _FakeLocator(visible=True, count=1, click=click)
        transition = _FakeLocator(visible=False, count=0)

        def locator(sel):
            if "study-transition-page" in sel:
                return transition
            if "ant-modal" in sel:
                return dialog
            return _FakeLocator(count=0)

        page = MagicMock()
        page.locator = MagicMock(side_effect=locator)
        page.get_by_role = MagicMock(return_value=_FakeLocator(count=0))
        page.wait_for_timeout = AsyncMock()
        page.wait_for_load_state = AsyncMock()

        handled = await handle_archive_continue_popup(page)

        self.assertTrue(handled)
        click.assert_awaited()

    async def test_handle_archive_returns_false_when_no_dialog(self):
        invisible = _FakeLocator(visible=False, count=0)
        page = MagicMock()
        page.locator = MagicMock(return_value=invisible)

        handled = await handle_archive_continue_popup(page)

        self.assertFalse(handled)

    async def test_rating_popup_routes_archive_dialog_to_continue(self):
        click = AsyncMock()
        # rating 先看到 ant-modal，文案含归档 → 转交 handle_archive
        dialog = _FakeLocator(
            visible=True,
            count=1,
            inner_text="该课程已归档，是否继续学习？\n取消\n继续学习",
            click=click,
        )
        transition = _FakeLocator(visible=False, count=0)

        def locator(sel):
            if "study-transition-page" in sel:
                return transition
            return dialog

        page = MagicMock()
        page.locator = MagicMock(side_effect=locator)
        page.get_by_role = MagicMock(return_value=_FakeLocator(count=0))
        page.wait_for_timeout = AsyncMock()
        page.wait_for_load_state = AsyncMock()

        handled = await handle_rating_popup(page)

        self.assertTrue(handled)
        click.assert_awaited()


if __name__ == "__main__":
    unittest.main()
