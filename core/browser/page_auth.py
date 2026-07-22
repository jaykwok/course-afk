from __future__ import annotations

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
    return await page.evaluate(_GET_AUTHORIZATION_SCRIPT)


async def wait_for_authorization_header(
    page,
    status_callback=None,
    *,
    empty_message: str = "页面未拿到登录令牌，请确认 cookies 仍有效",
) -> str:
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
    if status_callback:
        status_callback(empty_message)
    return ""


async def fetch_json(page, url: str, *, headers: dict[str, str]) -> object:
    response = await page.context.request.get(url, headers=headers)
    if not response.ok:
        body = await response.text()
        raise RuntimeError(f"请求失败 {response.status}: {body[:200]}")
    return await response.json()
