"""Real visible browser and synthetic pages on every supported CI runner."""

from pathlib import Path
import asyncio
import unittest

from core.browser.session import create_browser_context
from core.browser.overlays import dismiss_topmost_overlays_async
from core.learning.playback import ensure_course_started, read_course_playback_state


class VisibleCourseTests(unittest.IsolatedAsyncioTestCase):
    async def test_R9_interaction_appearing_during_watch_is_skipped_before_resume(self):
        from unittest.mock import AsyncMock, patch
        from core.learning.handlers import _watch_video_playback

        async with create_browser_context(cookies_path=None, controller=False) as (
            _,
            context,
        ):
            page = await context.new_page()
            await page.route("**/*", lambda route: route.abort())
            await page.set_content("""<dl id="section" class="focus"><div class="section-item-wrapper">Video</div></dl>
              <div class="fullScreen-content" style="height:300px"><video style="width:300px;height:150px"></video></div>
              <script>
                window.started=true; window.resumed=false; window.skips=0;
                const video=document.querySelector('video');
                Object.defineProperty(video,'readyState',{get:()=>4});
                Object.defineProperty(video,'paused',{get:()=>!window.started});
                Object.defineProperty(video,'duration',{get:()=>300});
                video.play=async()=>{
                  if(document.querySelector('#exercise')) throw new Error('interaction gate');
                  window.started=true; window.resumed=true;
                };
                window.showExercise=()=>{
                  window.started=false;
                  const gate=document.createElement('div'); gate.id='exercise';
                  gate.innerHTML='<div class="split-section-detail-header--interact">互动练习</div><button onclick="window.skips++;this.parentElement.remove()">跳 过</button>';
                  document.querySelector('.fullScreen-content').append(gate);
                };
              </script>""")
            shown = False

            async def server(*args):
                nonlocal shown
                if not shown:
                    await page.evaluate("window.showExercise()")
                    shown = True
                    return None
                return (
                    {"finish_status": 2}
                    if await page.evaluate("window.resumed")
                    else None
                )

            with (
                patch("core.learning.handlers.next_slice", return_value=0.01),
                patch(
                    "core.learning.progress.read_server_section_state",
                    new=AsyncMock(side_effect=server),
                ),
                patch(
                    "core.learning.flows.handle_rating_popup",
                    new=AsyncMock(return_value=False),
                ),
                patch(
                    "core.learning.popups.handle_archive_continue_popup",
                    new=AsyncMock(return_value=False),
                ),
            ):
                await asyncio.wait_for(
                    _watch_video_playback(
                        page, page.locator("#section"), watch_seconds=5
                    ),
                    timeout=10,
                )
            self.assertEqual(await page.evaluate("window.skips"), 1)
            self.assertTrue(await page.evaluate("window.resumed && window.started"))
            self.assertEqual(await page.locator("#exercise").count(), 0)

    async def test_business_dialogs_survive_generic_cleanup_and_start_is_read_back(
        self,
    ):
        async with create_browser_context(cookies_path=None, controller=False) as (
            _,
            context,
        ):
            page = await context.new_page()
            await page.route("**/*", lambda route: route.abort())
            try:
                for content in (
                    '<ul class="ant-rate"><li>1</li></ul><button>确 定</button>',
                    "<p>该课程已归档，是否继续学习？</p><button>继续学习</button>",
                    "<p>确认交卷</p><button>确 定</button>",
                ):
                    await page.set_content(
                        '<div role="dialog" style="position:fixed;z-index:1000"><button class="close" onclick="this.parentElement.remove()">×</button>'
                        + content
                        + "</div>"
                    )
                    await dismiss_topmost_overlays_async(page, max_count=1)
                    self.assertEqual(await page.locator("[role=dialog]").count(), 1)
                await page.set_content("""<dl id="section" class="focus"><div class="section-item-wrapper">文档</div></dl>
                    <div class="fullScreen-content" style="height:100px">Synthetic document</div>
                    <div class="register-mask-layer"><button id="section-registerMask" onclick="this.parentElement.remove()">开始学习</button></div>""")
                box = page.locator("#section")
                state = await ensure_course_started(page, box)
                self.assertFalse(state["start_gate_visible"])
                self.assertTrue(state["target_focused"])
                self.assertTrue(
                    (await read_course_playback_state(page, box))["content_visible"]
                )
            except BaseException:
                output = Path("build/ci")
                output.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(output / "synthetic-course.png"))
                raise
