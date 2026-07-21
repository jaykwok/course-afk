from __future__ import annotations

import logging


# 推广/遮罩顶层关闭：递归 Shadow DOM，点高 z-index 可见关闭钮。
# 不绑定具体推广文案。业务评分弹窗见 learning_popups；考试交卷勿用本模块。
DISMISS_TOPMOST_OVERLAY_SCRIPT = """
() => {
  // __courseAfkDismissTopmostOverlay: 便于诊断与测试识别该页面脚本。
  const isVisible = element => {
    if (!(element instanceof Element)) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none" &&
      style.visibility !== "hidden" &&
      Number(style.opacity || 1) > 0 &&
      rect.width > 0 && rect.height > 0;
  };

  const composedParent = element =>
    element.parentElement || element.getRootNode()?.host || null;

  const effectiveZIndex = element => {
    let current = element;
    let highest = 0;
    while (current) {
      const value = Number.parseInt(getComputedStyle(current).zIndex, 10);
      if (Number.isFinite(value)) highest = Math.max(highest, value);
      current = composedParent(current);
    }
    return highest;
  };

  const allElements = [];
  const visit = root => {
    for (const element of root.querySelectorAll("*")) {
      allElements.push(element);
      if (element.shadowRoot) visit(element.shadowRoot);
    }
  };
  visit(document);

  const closePattern =
    /(^|[-_])(close|dismiss|cancel)([-_]|$)|关闭|取消|稍后|知道了|icon-close|btn-close|el-icon-close/i;
  // 角标常见纯「X」/「×」
  const exactCloseText = /^(×|✕|✖|x|X|关闭|取消|稍后|我知道了|知道了)$/;
  const candidates = allElements
    .filter(isVisible)
    .filter(element => {
      const descriptor = [
        element.id,
        String(element.className?.baseVal || element.className || ""),
        element.getAttribute("aria-label"),
        element.getAttribute("title"),
        element.getAttribute("data-action"),
      ].filter(Boolean).join(" ");
      const text = String(element.textContent || "").trim();
      return closePattern.test(descriptor) || exactCloseText.test(text);
    })
    .map(element => ({ element, zIndex: effectiveZIndex(element) }))
    .filter(item => item.zIndex >= 100)
    .sort((left, right) => right.zIndex - left.zIndex);

  if (!candidates.length) return null;
  const target = candidates[0].element;
  if (typeof target.click === "function") {
    target.click();
  } else {
    target.dispatchEvent(new MouseEvent("click", {
      bubbles: true,
      cancelable: true,
      composed: true,
      view: window,
    }));
  }
  return {
    tag: target.tagName.toLowerCase(),
    className: String(target.className?.baseVal || target.className || ""),
    zIndex: candidates[0].zIndex,
  };
}
"""


def _is_navigation_context_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        "execution context was destroyed" in text
        or "most likely because of a navigation" in text
        or "target closed" in text
        or "target page, context or browser has been closed" in text
    )


async def dismiss_topmost_overlays_async(
    page,
    *,
    max_count: int = 3,
    settle_milliseconds: int = 200,
) -> int:
    """按层级逐个关闭页面顶层弹窗，包括开放 Shadow DOM 内的关闭按钮。"""

    dismissed = 0
    for _ in range(max_count):
        try:
            result = await page.evaluate(DISMISS_TOPMOST_OVERLAY_SCRIPT)
        except Exception as exc:
            if _is_navigation_context_error(exc):
                # SPA 跳转会销毁 evaluate 上下文，等 load 后再试
                logging.debug(f"关闭弹窗时遇导航，等待后重试: {exc}")
                try:
                    await page.wait_for_load_state("load")
                    await page.wait_for_timeout(settle_milliseconds)
                except Exception:
                    break
                continue
            logging.debug(f"检查页面顶层弹窗失败: {exc}")
            break
        if not isinstance(result, dict):
            break
        dismissed += 1
        logging.info(
            "已关闭页面顶层弹窗: "
            f"{result.get('tag', '')}.{result.get('className', '')}"
        )
        await page.wait_for_timeout(settle_milliseconds)
    return dismissed


def dismiss_topmost_overlays_sync(
    page,
    *,
    max_count: int = 3,
    settle_milliseconds: int = 200,
) -> int:
    """同步版顶层弹窗处理，用于登录凭证刷新流程。"""

    dismissed = 0
    for _ in range(max_count):
        try:
            result = page.evaluate(DISMISS_TOPMOST_OVERLAY_SCRIPT)
        except Exception as exc:
            logging.debug(f"检查页面顶层弹窗失败: {exc}")
            break
        if not isinstance(result, dict):
            break
        dismissed += 1
        logging.info(
            "已关闭页面顶层弹窗: "
            f"{result.get('tag', '')}.{result.get('className', '')}"
        )
        page.wait_for_timeout(settle_milliseconds)
    return dismissed


async def prepare_page_after_navigation_async(
    page,
    *,
    rounds: int = 3,
    max_per_round: int = 5,
    settle_milliseconds: int = 200,
    between_round_milliseconds: int = 400,
    status_callback=None,
) -> int:
    """
    进页后的调度层：多轮调用 dismiss_topmost_overlays（同一套关闭逻辑）。

    弹窗常晚于 load 出现，故多轮并短暂等待；连续两轮未关到则结束。
    考试交卷/答题确认等路径请勿调用，以免误关业务弹窗。
    """

    total = 0
    empty_streak = 0
    for round_i in range(rounds):
        closed = await dismiss_topmost_overlays_async(
            page,
            max_count=max_per_round,
            settle_milliseconds=settle_milliseconds,
        )
        total += closed
        if closed:
            empty_streak = 0
            if status_callback:
                status_callback(f"已关闭 {closed} 个页面弹窗，继续操作")
        else:
            empty_streak += 1
            if empty_streak >= 2:
                break
        if round_i + 1 < rounds:
            await page.wait_for_timeout(between_round_milliseconds)
    return total


def prepare_page_after_navigation_sync(
    page,
    *,
    rounds: int = 3,
    max_per_round: int = 5,
    settle_milliseconds: int = 200,
    between_round_milliseconds: int = 400,
    status_callback=None,
) -> int:
    """同步版进页调度：多轮调用 dismiss_topmost_overlays。"""

    total = 0
    empty_streak = 0
    for round_i in range(rounds):
        closed = dismiss_topmost_overlays_sync(
            page,
            max_count=max_per_round,
            settle_milliseconds=settle_milliseconds,
        )
        total += closed
        if closed:
            empty_streak = 0
            if status_callback:
                status_callback(f"已关闭 {closed} 个页面弹窗，继续操作")
        else:
            empty_streak += 1
            if empty_streak >= 2:
                break
        if round_i + 1 < rounds:
            page.wait_for_timeout(between_round_milliseconds)
    return total


async def goto_and_prepare_async(
    page,
    url: str,
    *,
    wait_until: str = "load",
    timeout: float | None = None,
    **prepare_kwargs,
) -> int:
    """goto 后做进页弹窗清理。考试流程请用普通 goto，不要用本函数。"""

    goto_kwargs: dict = {"wait_until": wait_until}
    if timeout is not None:
        goto_kwargs["timeout"] = timeout
    await page.goto(url, **goto_kwargs)
    return await prepare_page_after_navigation_async(page, **prepare_kwargs)
