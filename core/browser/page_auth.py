from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from core.browser.overlays import dismiss_topmost_overlays_async

_AUTH_WAIT_MILLISECONDS = 15000
_AUTH_POLL_MILLISECONDS = 500

_GET_AUTHORIZATION_SCRIPT = """
() => {
  const cookie = (document.cookie.match(/(?:^|; )authorization=([^;]+)/) || [])[1];
  if (cookie) return decodeURIComponent(cookie);
  try {
    const saveCookie = localStorage.getItem("save_cookie") || "";
    const fromSave = (saveCookie.match(/(?:^|;\\s*)authorization=([^;]+)/) || [])[1];
    if (fromSave) return decodeURIComponent(fromSave);
  } catch (_) {}
  try {
    const token = JSON.parse(localStorage.getItem("token") || "{}").access_token;
    if (token) return `Bearer__${token}`;
  } catch (_) {}
  const saas = localStorage.getItem("saas-token");
  if (saas) return `Bearer__${saas}`;
  return "";
}
"""


async def get_authorization_header(page) -> str:
    """读取页面鉴权头，格式为 Bearer__{access_token}（双下划线）。"""
    async with asyncio.timeout(15):
        return await page.evaluate(_GET_AUTHORIZATION_SCRIPT)


async def wait_for_authorization_header(
    page,
    status_callback=None,
    *,
    empty_message: str = "页面未拿到登录令牌，请确认 cookies 仍有效",
) -> str:
    try:
        async with asyncio.timeout(_AUTH_WAIT_MILLISECONDS / 1000):
            auth = await _poll_authorization_header(page)
    except TimeoutError:
        auth = ""
    if not auth and status_callback:
        status_callback(empty_message)
    return auth


async def _poll_authorization_header(page) -> str:
    elapsed = 0
    while elapsed <= _AUTH_WAIT_MILLISECONDS:
        try:
            await dismiss_topmost_overlays_async(page)
            auth = (await get_authorization_header(page) or "").strip()
        except Exception as exc:
            text = str(exc).lower()
            if (
                "execution context was destroyed" in text
                or "navigation" in text
            ):
                try:
                    await page.wait_for_load_state("load")
                except Exception:
                    pass
                await page.wait_for_timeout(_AUTH_POLL_MILLISECONDS)
                elapsed += _AUTH_POLL_MILLISECONDS
                continue
            raise
        if auth:
            return auth
        if elapsed >= _AUTH_WAIT_MILLISECONDS:
            break
        await page.wait_for_timeout(_AUTH_POLL_MILLISECONDS)
        elapsed += _AUTH_POLL_MILLISECONDS
    return ""


async def fetch_json(
    page, url: str, *, headers: dict[str, str], method: str = "GET", data=None,
) -> object:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "kc.zhixueyun.com" or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443):
        raise ValueError("API 地址不在允许的站点范围")
    if method not in {"GET", "POST"}:
        raise ValueError("API 请求方法仅支持 GET / POST")
    response = None
    try:
        async with asyncio.timeout(35):
            options = dict(headers=headers, timeout=30000, max_redirects=0)
            if method == "POST":
                response = await page.context.request.post(url, data=data, **options)
            else:
                response = await page.context.request.get(url, **options)
            if not response.ok:
                raise RuntimeError(f"API 请求失败 HTTP {response.status}")
            result = await response.json()
            if not isinstance(result, (dict, list)):
                raise ValueError("API 响应不是 JSON 对象或数组")
            return result
    finally:
        if response is not None:
            from core.runtime import close_safely
            await close_safely(response.dispose(), label="API response")
