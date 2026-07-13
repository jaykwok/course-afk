"""TUI 冒烟测试：用 Textual 的 Pilot(headless) 驱动真实的 launcher.main() 循环。

覆盖端到端桥接链路：
  工作线程 launcher.main() -> patched show_title/render_dashboard/show_menu
  -> call_from_thread 挂载 OptionScreen -> Pilot 选择「退出」
  -> resolve_prompt(10) -> Queue.get 放行 -> main() 返回 -> app.exit()

安全措施：patch core.config.run_async 为空操作，确保即便导航落点不对，
也绝不会触发真实 Playwright 浏览器自动化。
"""

from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import patch

from textual.widgets import Button, OptionList, Static, TextArea

import core.ui as cli_ui
from core.abort import UserCancelRequested
from core.tui_app import (
    BusyScreen,
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
    def test_busy_screen_persists_and_updates_until_result_prompt(self):
        """浏览器任务期间保持状态页，随后无空档交接到结果提示。"""

        async def scenario() -> None:
            release = threading.Event()
            outcome = {}

            def caller() -> None:
                try:
                    cli_ui.begin_operation("课程学习", "正在打开浏览器")
                    outcome["busy_mounted"] = True
                    release.wait(timeout=8.0)
                    cli_ui.show_info("正在汇总处理结果")
                    cli_ui.pause("处理完成")
                    outcome["done"] = True
                except BaseException as exc:  # noqa: BLE001
                    outcome["error"] = repr(exc)

            worker = threading.Thread(target=caller, daemon=True)
            with patch("core.config.setup_logging"):
                app = _PromptApp()
                frontend = TuiFrontend(app)
                frontend.install()
                try:
                    async with app.run_test(size=(90, 22)) as pilot:
                        worker.start()
                        self.assertTrue(
                            await _wait_for(
                                lambda: isinstance(app.screen, BusyScreen), pilot
                            )
                        )
                        self.assertTrue(outcome.get("busy_mounted"))
                        self.assertIsNone(app.focused)
                        release.set()

                        # BusyScreen 保持到 PauseScreen 原位替换，不能露出底层。
                        loop = asyncio.get_event_loop()
                        deadline = loop.time() + 8.0
                        while not isinstance(app.screen, PauseScreen):
                            self.assertIsInstance(app.screen, BusyScreen)
                            self.assertLess(loop.time(), deadline)
                            await pilot.pause()
                        await pilot.press("enter")
                        self.assertTrue(
                            await _wait_for(
                                lambda: not worker.is_alive(), pilot, timeout=8.0
                            )
                        )
                finally:
                    release.set()
                    frontend.restore()

            self.assertTrue(outcome.get("done"), outcome)

        asyncio.run(asyncio.wait_for(scenario(), timeout=30.0))

    def test_link_confirmation_and_result_summary_screens(self):
        """浏览器前显示分类确认，浏览器后显示单按钮结果页。"""

        async def scenario() -> None:
            outcome: dict = {}
            rows = [(f"分类 {index}", str(index)) for index in range(1, 11)]

            def caller() -> None:
                try:
                    outcome["confirmed"] = cli_ui.prompt_summary_confirmation(
                        "链接解析确认",
                        rows,
                        "确认继续处理？",
                        default="Y",
                    )
                    handle = cli_ui.prepare_pause_with_summary(
                        "链接解析完成",
                        rows,
                        "解析完成，请确认结果",
                    )
                    outcome["result_mounted"] = True
                    cli_ui.wait_prepared_prompt(handle)
                    outcome["result_ok"] = True
                except BaseException as exc:  # noqa: BLE001
                    outcome["error"] = repr(exc)

            worker = threading.Thread(target=caller, daemon=True)
            with patch("core.config.setup_logging"):
                app = _PromptApp()
                frontend = TuiFrontend(app)
                frontend.install()
                try:
                    async with app.run_test(size=(90, 22)) as pilot:
                        worker.start()
                        self.assertTrue(
                            await _wait_for(
                                lambda: isinstance(app.screen, YesNoScreen), pilot
                            )
                        )
                        app.screen.query_one("#yn-details", Static)
                        self.assertEqual(app.focused.id, "yes")
                        await pilot.press("right")
                        self.assertEqual(app.focused.id, "no")
                        await pilot.press("left")
                        self.assertEqual(app.focused.id, "yes")
                        await pilot.press("down")
                        self.assertEqual(app.focused.id, "no")
                        await pilot.press("up")
                        self.assertEqual(app.focused.id, "yes")
                        await pilot.press("right")
                        self.assertEqual(app.focused.id, "no")
                        await pilot.press("enter")

                        self.assertTrue(
                            await _wait_for(
                                lambda: isinstance(app.screen, PauseScreen), pilot
                            )
                        )
                        app.screen.query_one("#pause-details", Static)
                        self.assertTrue(outcome.get("result_mounted"))
                        ok_button = app.screen.query_one("#continue", Button)
                        self.assertIn("OK", str(ok_button.label))
                        self.assertIs(app.focused, ok_button)
                        dialog = app.screen.query_one("#pause-dialog")
                        self.assertLessEqual(
                            dialog.region.y + dialog.region.height,
                            app.screen.size.height,
                        )
                        self.assertLessEqual(
                            ok_button.region.y + ok_button.region.height,
                            dialog.region.y + dialog.region.height,
                        )
                        await pilot.click("#continue")

                        self.assertTrue(
                            await _wait_for(
                                lambda: not worker.is_alive(), pilot, timeout=8.0
                            )
                        )
                finally:
                    frontend.restore()

            self.assertEqual(outcome.get("confirmed"), False)
            self.assertEqual(outcome.get("result_ok"), True)
            self.assertNotIn("error", outcome)

        asyncio.run(asyncio.wait_for(scenario(), timeout=30.0))

    def test_escape_interrupts_workflow_or_exits_at_top_level(self):
        """无弹窗时，Esc 优先中断任务；空闲主菜单则退出。"""
        app = _PromptApp()
        with (
            patch("core.config.interrupt_running_async", return_value=True) as interrupt,
            patch.object(app, "exit") as exit_app,
        ):
            app.action_go_back()
            interrupt.assert_called_once_with()
            exit_app.assert_not_called()

        with (
            patch("core.config.interrupt_running_async", return_value=False),
            patch.object(app, "exit") as exit_app,
        ):
            app.action_go_back()
            exit_app.assert_called_once_with()

    def test_multiline_ctrl_enter_submits(self):
        """输入框有焦点时，Ctrl+Enter 的传统终端编码也应提交。"""

        async def scenario() -> None:
            outcome: dict = {}

            def caller() -> None:
                try:
                    outcome["value"] = cli_ui.prompt_multiline_input(
                        [f"操作说明 {index}" for index in range(1, 6)]
                    )
                except BaseException as exc:  # noqa: BLE001
                    outcome["error"] = repr(exc)

            worker = threading.Thread(target=caller, daemon=True)

            with patch("core.config.setup_logging"):
                app = _PromptApp()
                frontend = TuiFrontend(app)
                frontend.install()
                try:
                    async with app.run_test(size=(90, 22)) as pilot:
                        worker.start()
                        self.assertTrue(
                            await _wait_for(
                                lambda: isinstance(app.screen, MultilineScreen),
                                pilot,
                            ),
                            f"multiline 模态屏未出现: {outcome}",
                        )
                        text_area = app.screen.query_one("#ml-text", TextArea)
                        dialog = app.screen.query_one("#ml-dialog")
                        submit_button = app.screen.query_one("#submit", Button)
                        cancel_button = app.screen.query_one("#cancel", Button)
                        self.assertIs(app.focused, text_area)
                        await pilot.press("tab")
                        self.assertIs(app.focused, submit_button)
                        await pilot.press("tab")
                        self.assertIs(app.focused, cancel_button)
                        await pilot.press("shift+tab")
                        self.assertIs(app.focused, submit_button)
                        text_area.focus()
                        self.assertGreaterEqual(text_area.region.height, 3)
                        self.assertLessEqual(
                            dialog.region.y + dialog.region.height,
                            app.screen.size.height,
                        )
                        for button in (submit_button, cancel_button):
                            self.assertLessEqual(
                                button.region.y + button.region.height,
                                dialog.region.y + dialog.region.height,
                            )
                        text_area.text = "https://www.mylearning.cn/course"
                        text_area.focus()
                        # 传统终端把 Ctrl+Enter 编码成 LF，即 Textual 的 ctrl+j。
                        await pilot.press("ctrl+j")
                        self.assertTrue(
                            await _wait_for(
                                lambda: not worker.is_alive(), pilot, timeout=8.0
                            ),
                            "Ctrl+Enter 未提交多行输入",
                        )
                finally:
                    frontend.restore()

            self.assertEqual(
                outcome.get("value"),
                "https://www.mylearning.cn/course",
                f"Ctrl+Enter 提交结果异常: {outcome}",
            )

        asyncio.run(asyncio.wait_for(scenario(), timeout=20.0))

    def test_prompt_screens_round_trip(self):
        """覆盖 yes/no、multiline(取消)、pause 三种模态屏的连续交接。"""

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

                        # multiline 挂载前必须始终保留旧模态屏，不能露出底层。
                        loop = asyncio.get_event_loop()
                        deadline = loop.time() + 8.0
                        while not isinstance(app.screen, MultilineScreen):
                            self.assertIsInstance(app.screen, YesNoScreen)
                            self.assertLess(loop.time(), deadline)
                            await pilot.pause()

                        # multiline -> 点「取消」
                        await pilot.click("#cancel")

                        # pause 挂载前同样保持 multiline，不出现日志或空白帧。
                        deadline = loop.time() + 8.0
                        while not isinstance(app.screen, PauseScreen):
                            self.assertIsInstance(app.screen, MultilineScreen)
                            self.assertLess(loop.time(), deadline)
                            await pilot.pause()

                        # pause -> 点「继续」
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

    def test_escape_returns_from_every_prompt_type(self):
        """Esc 应统一取消选择、确认、多行输入和暂停界面。"""

        async def scenario() -> None:
            outcomes: dict[str, str] = {}

            def run_prompt(name, prompt) -> None:
                try:
                    prompt()
                    outcomes[name] = "returned"
                except UserCancelRequested:
                    outcomes[name] = "cancelled"
                except BaseException as exc:  # noqa: BLE001
                    outcomes[name] = f"error:{exc!r}"

            def caller() -> None:
                run_prompt("choice", lambda: cli_ui.prompt_choice("选择", ["A"]))
                run_prompt("yesno", lambda: cli_ui.prompt_yes_no("确认？"))
                run_prompt(
                    "multiline",
                    lambda: cli_ui.prompt_multiline_input(["请粘贴链接"]),
                )
                run_prompt("pause", lambda: cli_ui.pause("完成"))

            worker = threading.Thread(target=caller, daemon=True)

            with patch("core.config.setup_logging"):
                app = _PromptApp()
                frontend = TuiFrontend(app)
                frontend.install()
                try:
                    async with app.run_test(size=(100, 30)) as pilot:
                        worker.start()
                        for screen_type in (
                            OptionScreen,
                            YesNoScreen,
                            MultilineScreen,
                            PauseScreen,
                        ):
                            self.assertTrue(
                                await _wait_for(
                                    lambda expected=screen_type: isinstance(
                                        app.screen, expected
                                    ),
                                    pilot,
                                ),
                                f"{screen_type.__name__} 未出现",
                            )
                            await pilot.press("escape")

                        self.assertTrue(
                            await _wait_for(
                                lambda: not worker.is_alive(), pilot, timeout=8.0
                            ),
                            "Esc 依次返回后工作线程未结束",
                        )
                finally:
                    frontend.restore()

            self.assertEqual(
                outcomes,
                {
                    "choice": "cancelled",
                    "yesno": "cancelled",
                    "multiline": "cancelled",
                    "pause": "cancelled",
                },
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
            import core.launcher_controller as launcher_controller

            with (
                patch("core.config.setup_logging"),
                patch.object(launcher_controller, "run_async", return_value=None),
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
                        menu_screen.query_one("#opt-status", Static)
                        main_content = app.query_one("#main")
                        self.assertEqual(main_content.styles.visibility, "visible")
                        self.assertIn(
                            "ESC 退出",
                            str(menu_screen.query_one("#opt-hint", Static).render()),
                        )
                        dialog = menu_screen.query_one("#opt-dialog")
                        screen_width = menu_screen.size.width
                        screen_height = menu_screen.size.height
                        self.assertLessEqual(
                            abs((dialog.region.x * 2 + dialog.region.width) - screen_width),
                            2,
                            "主菜单没有水平居中",
                        )
                        self.assertLessEqual(
                            abs((dialog.region.y * 2 + dialog.region.height) - screen_height),
                            2,
                            "主菜单没有垂直居中",
                        )
                        option_list = menu_screen.query_one("#opt-list", OptionList)
                        self.assertIs(app.focused, option_list)
                        for _ in range(9):
                            await pilot.press("down")
                        self.assertEqual(option_list.highlighted, 9)
                        await pilot.press("enter")
                        self.assertIs(
                            app._held_prompt_screen,
                            menu_screen,
                            "菜单选择后没有保持上一帧",
                        )

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
