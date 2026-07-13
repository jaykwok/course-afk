from __future__ import annotations

import logging


# 在页面自身的执行环境里递归遍历开放的 Shadow DOM。知学云的 AI 升级提示是
# z-index 很高的自定义元素，关闭按钮位于 shadowRoot 中，普通 locator 无法直接发现。
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

  const closePattern = /(^|[-_])(close|dismiss|cancel)([-_]|$)|关闭|取消|稍后|知道了/i;
  const exactCloseText = /^(×|✕|✖|x|关闭|取消|稍后|我知道了|知道了)$/i;
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
            logging.debug(f"检查页面顶层弹窗失败: {exc}")
            break
        if not result:
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
        if not result:
            break
        dismissed += 1
        logging.info(
            "已关闭页面顶层弹窗: "
            f"{result.get('tag', '')}.{result.get('className', '')}"
        )
        page.wait_for_timeout(settle_milliseconds)
    return dismissed
