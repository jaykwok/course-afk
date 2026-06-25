"""TUI 冒烟测试：用 Textual 的 Pilot(headless) 驱动真实的 launcher.main() 循环。

覆盖端到端桥接链路：
  工作线程 launcher.main() -> patched show_title/render_dashboard/show_menu
  -> call_from_thread 挂载 OptionScreen -> Pilot 选择「退出」
  -> dismiss(10) -> Queue.get 放行 -> main() 返回 -> app.exit()

安全措施：patch core.config.run_async 为空操作，确保即便导航落点不对，
也绝不会触发真实 Playwright 浏览器自动化。
"""

from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import patch

from textual.widgets import OptionList

import core.ui as cli_ui
from core.abort import UserCancelRequested
from core.tui_app import (
    CourseTuiApp,
    MultilineScreen,
    OptionScreen,
    PauseScreen,
    YesNoScreen,
)
from core.tui_bridge import TuiFrontend


class _CapturingApp(CourseTuiApp):
    """记录工作线程结果，便于断言 main() 是否正常返回。"""

    def _spawn_launcher_thread(self) -> None:
        import launcher

        def target() -> None:
            try:
                launcher.main()
                self._worker_result = "ok"
            except BaseException as exc:  # noqa: BLE001
                self._worker_result = f"error:{exc!r}"
                self._safe_emit_error(f"运行出错：{exc}")
            finally:
                self._safe_exit()

        thread = threading.Thread(
            target=target, name="course-launcher-test", daemon=True
        )
        self._test_worker = thread
        self._worker_result = None
        thread.start()


async def _wait_for(predicate, pilot, *, timeout: float = 8.0) -> bool:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        await pilot.pause()
        try:
            if predicate():
                return True
        except Exception:
            pass
    try:
        return bool(predicate())
    except Exception:
        return False


class _PromptApp(CourseTuiApp):
    """不启动 launcher 工作线程；测试自行驱动提示。"""

    def _spawn_launcher_thread(self) -> None:
        pass


class TuiSmokeTests(unittest.TestCase):
    def test_prompt_screens_round_trip(self):
        """覆盖 yes/no、multiline(取消)、pause 三种模态屏的 dismiss 回调。"""

        async def scenario() -> None:
            outcomes: dict = {}

            def caller() -> None:
                try:
                    outcomes["yesno"] = cli_ui.prompt_yes_no("自动交卷？", default="N")
                except BaseException as exc:  # noqa: BLE001
                    outcomes["yesno_err"] = repr(exc)
                try:
                    cli_ui.prompt_multiline_input(["请粘贴链接"])
                    outcomes["multiline"] = "submitted"
                except UserCancelRequested:
                    outcomes["multiline"] = "cancelled"
                except BaseException as exc:  # noqa: BLE001
                    outcomes["multiline_err"] = repr(exc)
                try:
                    cli_ui.pause("完成")
                    outcomes["pause"] = "ok"
                except BaseException as exc:  # noqa: BLE001
                    outcomes["pause_err"] = repr(exc)

            worker = threading.Thread(target=caller, daemon=True)

            with patch("core.config.setup_logging"):
                app = _PromptApp()
                frontend = TuiFrontend(app)
                frontend.install()
                try:
                    async with app.run_test(size=(120, 40)) as pilot:
                        worker.start()

                        # yes/no -> 点「是」
                        self.assertTrue(
                            await _wait_for(
                                lambda: isinstance(app.screen, YesNoScreen), pilot
                            ),
                            "yes/no 模态屏未出现",
                        )
                        await pilot.click("#yes")
                        await pilot.pause()

                        # multiline -> 点「取消」
                        self.assertTrue(
                            await _wait_for(
                                lambda: isinstance(app.screen, MultilineScreen),
                                pilot,
                            ),
                            "multiline 模态屏未出现",
                        )
                        await pilot.click("#cancel")
                        await pilot.pause()

                        # pause -> 点「继续」
                        self.assertTrue(
                            await _wait_for(
                                lambda: isinstance(app.screen, PauseScreen), pilot
                            ),
                            "pause 模态屏未出现",
                        )
                        await pilot.click("#continue")
                        await pilot.pause()

                        self.assertTrue(
                            await _wait_for(
                                lambda: not worker.is_alive(), pilot, timeout=8.0
                            ),
                            "提示驱动线程未结束",
                        )
                finally:
                    frontend.restore()

            self.assertTrue(outcomes.get("yesno"), f"yes/no 结果异常: {outcomes}")
            self.assertEqual(
                outcomes.get("multiline"), "cancelled", f"multiline 结果异常: {outcomes}"
            )
            self.assertEqual(
                outcomes.get("pause"), "ok", f"pause 结果异常: {outcomes}"
            )

        asyncio.run(asyncio.wait_for(scenario(), timeout=30.0))

    def test_ctrl_c_at_cancellable_prompt_returns_to_menu(self):
        """Ctrl+C 命中工作流模态提示（是/否）时取消该提示，返回主菜单而非退出。"""

        async def scenario() -> None:
            outcomes: dict = {}

            def caller() -> None:
                try:
                    outcomes["yesno"] = cli_ui.prompt_yes_no("自动交卷？", default="N")
                except UserCancelRequested:
                    outcomes["yesno"] = "cancelled"
                except BaseException as exc:  # noqa: BLE001
                    outcomes["yesno"] = f"error:{exc!r}"

            worker = threading.Thread(target=caller, daemon=True)

            with patch("core.config.setup_logging"):
                app = _PromptApp()
                frontend = TuiFrontend(app)
                frontend.install()
                try:
                    async with app.run_test(size=(100, 30)) as pilot:
                        worker.start()
                        self.assertTrue(
                            await _wait_for(
                                lambda: isinstance(app.screen, YesNoScreen), pilot
                            ),
                            "yes/no 模态屏未出现",
                        )
                        # 模拟在提示上按 Ctrl+C
                        app.action_request_quit()
                        await pilot.pause()
                        self.assertTrue(
                            await _wait_for(
                                lambda: not worker.is_alive(), pilot, timeout=8.0
                            ),
                            "Ctrl+C 取消提示后工作线程未结束",
                        )
                finally:
                    frontend.restore()

            self.assertEqual(
                outcomes.get("yesno"), "cancelled", f"yes/no 结果异常: {outcomes}"
            )

        asyncio.run(asyncio.wait_for(scenario(), timeout=30.0))

    def test_wait_with_progress_updates_then_clears(self):
        """异步 wait_with_progress 桥接：在工作线程自己的 asyncio 循环上更新进度行。"""

        async def scenario() -> None:
            finished: dict = {}
            progress_calls: list = []
            clear_calls: list = []

            def worker_target() -> None:
                try:
                    asyncio.run(
                        cli_ui.wait_with_progress(2, description="进度测试")
                    )
                    finished["ok"] = True
                except BaseException as exc:  # noqa: BLE001
                    finished["err"] = repr(exc)

            worker = threading.Thread(target=worker_target, daemon=True)

            with patch("core.config.setup_logging"):
                app = _PromptApp()
                real_set = app.set_progress
                real_clear = app.clear_progress
                app.set_progress = lambda *a, _r=real_set: (
                    progress_calls.append(a),
                    _r(*a),
                )[1]
                app.clear_progress = lambda *a, _r=real_clear: (
                    clear_calls.append(a),
                    _r(*a),
                )[1]
                frontend = TuiFrontend(app)
                frontend.install()
                try:
                    async with app.run_test(size=(100, 30)) as pilot:
                        worker.start()
                        loop = asyncio.get_event_loop()
                        deadline = loop.time() + 10.0
                        while loop.time() < deadline:
                            await pilot.pause()
                            if finished:
                                break
                finally:
                    frontend.restore()

            self.assertTrue(
                finished.get("ok"), f"wait_with_progress 未正常结束: {finished}"
            )
            self.assertTrue(
                any(c and "进度测试" in str(c[0]) for c in progress_calls),
                f"进度行未被更新: {progress_calls}",
            )
            self.assertTrue(clear_calls, "进度行结束后未清空")

        asyncio.run(asyncio.wait_for(scenario(), timeout=20.0))

    def test_menu_to_quit_runs_full_bridge_and_exits_cleanly(self):
        async def scenario() -> None:
            # 安全网：即便菜单落点不在「退出」，也绝不启动浏览器自动化
            with (
                patch("core.config.setup_logging"),
                patch("core.config.run_async", return_value=None),
            ):
                app = _CapturingApp()
                frontend = TuiFrontend(app)
                frontend.install()
                try:
                    async with app.run_test(size=(120, 40)) as pilot:
                        # 1. 仪表盘 / 日志组件就位
                        app.query_one("#dashboard")
                        app.query_one("#log")

                        # 2. 工作线程把主菜单模态屏挂上来
                        appeared = await _wait_for(
                            lambda: isinstance(app.screen, OptionScreen), pilot
                        )
                        self.assertTrue(
                            appeared, "主菜单模态屏未在预期时间内出现"
                        )

                        # 3. 确定性高亮到最后一项（退出）并确认
                        menu_screen = app.screen
                        self.assertIsInstance(menu_screen, OptionScreen)
                        option_list = menu_screen.query_one("#opt-list", OptionList)
                        option_list.action_last()
                        option_list.focus()
                        await pilot.pause()
                        await pilot.press("enter")

                        # 4. 工作线程应因「退出」分支而正常结束
                        worker = app._test_worker
                        terminated = await _wait_for(
                            lambda: not worker.is_alive(), pilot, timeout=10.0
                        )
                        self.assertTrue(
                            terminated, "选择退出后工作线程未结束"
                        )
                finally:
                    frontend.restore()

            self.assertEqual(
                app._worker_result, "ok", "launcher.main() 未能正常返回"
            )

        asyncio.run(asyncio.wait_for(scenario(), timeout=30.0))


if __name__ == "__main__":
    unittest.main()
