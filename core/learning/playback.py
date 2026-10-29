"""读取当前章节的真实开始遮罩和媒体状态，通过平台控件恢复学习。"""
from __future__ import annotations

import asyncio
import logging

from core.abort import CoursePlaybackNotStartedError
from core.browser.session import is_target_closed_exception


_PLAYBACK_STATE_SCRIPT = r"""
targetId => {
  const visible = element => {
    if (!element) return false;
    const style = getComputedStyle(element), rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' &&
      style.visibility !== 'hidden' && Number(style.opacity || 1) > 0;
  };
  const target = document.getElementById(targetId);
  let timerSeconds = null;
  try {
    const timer = window.require?.('./app/util/business/timer');
    if (typeof timer?.getValue === 'function') {
      const value = timer.getValue();
      if (Number.isFinite(value)) timerSeconds = value;
    }
  } catch (_) {}
  return {
    available: true,
    target_found: Boolean(target),
    target_focused: Boolean(target?.classList.contains('focus')),
    document_focused: document.hasFocus(),
    visibility: document.visibilityState,
    timer_seconds: timerSeconds,
    start_gate_visible: Array.from(document.querySelectorAll('.register-mask-layer')).some(visible),
    content_visible: Array.from(document.querySelectorAll('[class*="fullScreen-content"]')).some(visible),
    videos: Array.from(document.querySelectorAll('video')).filter(visible).map(video => ({
      paused: video.paused, ended: video.ended, ready_state: video.readyState,
      current_time: Number.isFinite(video.currentTime) ? video.currentTime : null,
      duration: Number.isFinite(video.duration) ? video.duration : null,
    })),
  };
}
"""

_RESUME_VISIBLE_MEDIA_SCRIPT = r"""
async targetId => {
  if (!document.getElementById(targetId)?.classList.contains('focus')) return false;
  const video = Array.from(document.querySelectorAll('video')).find(element => {
    const r = element.getBoundingClientRect(), s = getComputedStyle(element);
    return r.width > 0 && r.height > 0 && s.display !== 'none' &&
      s.visibility !== 'hidden' && element.readyState >= 1 && element.paused && !element.ended;
  });
  if (!video) return false;
  try { await video.play(); return !video.paused; } catch (_) { return false; }
}
"""


async def read_course_playback_state(page, box) -> dict:
    """只读探针；不访问账号信息，不根据已有进度猜测正在播放。"""
    try:
        async with asyncio.timeout(5):
            target_id = await box.get_attribute('id')
            if not target_id:
                return {"available": False, "error": "target_id_missing"}
            state = await page.evaluate(_PLAYBACK_STATE_SCRIPT, target_id)
            return state if isinstance(state, dict) else {"available": False, "error": "invalid_state"}
    except Exception as exc:
        if is_target_closed_exception(exc):
            raise
        return {"available": False, "error": type(exc).__name__}


def _paused_media(state: dict) -> bool:
    return any(video.get('paused') and not video.get('ended') and video.get('ready_state', 0) >= 1
               for video in state.get('videos', []) if isinstance(video, dict))


async def ensure_course_started(page, box, *, attempts: int = 3) -> dict:
    """点击真正绑定 studyRegister 的按钮，并读回状态；不直接提交学习进度。"""
    state = {}
    actions = []
    for attempt in range(max(1, attempts)):
        state = await read_course_playback_state(page, box)
        if state.get('available') is not True:
            break
        try:
            if not state.get('target_found') or not state.get('target_focused'):
                await box.locator('.section-item-wrapper').click(timeout=3000)
                actions.append('reselect_target')
            elif state.get('start_gate_visible'):
                # 平台事件绑在 registerMask 图片 ID，点击遮罩空白不保证触发。
                button = page.locator(".register-mask-layer:visible [id$='registerMask']").first
                await button.click(timeout=3000)
                actions.append('click_start_button')
                # 注册与播放器加载会竞态；重新激活同一节，触发其正常计时初始化。
                await box.locator('.section-item-wrapper').click(timeout=3000)
                actions.append('reselect_after_start')
                logging.info("检测到开始学习遮罩，已点击实际开始按钮并重新激活目标章节")
            elif _paused_media(state):
                controls = page.locator('.video-js.vjs-paused .vjs-play-control:visible')
                if await controls.count():
                    await controls.first.click(timeout=3000)
                else:
                    await page.evaluate(_RESUME_VISIBLE_MEDIA_SCRIPT, await box.get_attribute('id'))
                actions.append('resume_video')
            elif state.get('content_visible') and not state.get('videos') and state.get('document_focused') is False:
                # 平台文档计时器在 window.blur 时暂停，恢复真实页面焦点。
                await page.bring_to_front()
                actions.append('focus_document')
            elif state.get('content_visible'):
                if actions:
                    logging.info("目标章节开始遮罩/暂停状态已解除（操作: %s）", ', '.join(actions))
                return state
            else:
                # 章节 focus 常先于内容挂载；给真实内容加载时间，再读回状态。
                await page.locator("[class*='fullScreen-content']").first.wait_for(state='visible', timeout=8000)
                actions.append('wait_content')
            await page.wait_for_timeout(500)
        except Exception as exc:
            if is_target_closed_exception(exc):
                raise
            state['action_error'] = type(exc).__name__
            logging.debug("课程开始操作未确认（第 %s 次）: %s", attempt + 1, type(exc).__name__)
            await page.wait_for_timeout(500)
    # 最后一次点击后再读回，不能把 click() 返回当作已经开始。
    state = await read_course_playback_state(page, box)
    if (state.get('available') is True and state.get('target_focused') and
        not state.get('start_gate_visible') and state.get('content_visible') and not _paused_media(state) and
        (state.get('videos') or state.get('document_focused') is not False)):
        return state
    raise CoursePlaybackNotStartedError(detail={"playback_state": state, "start_actions": actions})
