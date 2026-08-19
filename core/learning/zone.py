from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from core.browser.session import create_browser_context
from core.config import ZHIXUEYUN_COURSE_PREFIX, ZHIXUEYUN_SUBJECT_PREFIX
from core.file_ops import is_compliant_url_regex, normalize_url
from core.links import is_ctexpert_case_pool_url, unique_urls
from core.browser.overlays import (
    dismiss_topmost_overlays_async,
    prepare_page_after_navigation_async,
)
from core.discovery.subject_parse import enqueue_learning_links_with_subject_expand


# 实勘（cms topic / 学习专区）:
# - 非知学云 class API；链接主要在静态/半静态 HTML 中
# - 需点「更多 / 查看更多」展开后才能收全
# - 弹窗先关再读 DOM（通用 dismiss，不绑文案）
LEARNING_ZONE_LINK_WAIT_MILLISECONDS = 15000
LEARNING_ZONE_LINK_POLL_MILLISECONDS = 500
_MORE_CLICK_MAX = 20
_MORE_CLICK_WAIT_MS = 1200
_CASE_POOL_PAGE_MAX = 200
_CASE_POOL_PAGE_WAIT_MS = 250
_CASE_POOL_PAGE_SETTLE_MS = 500
_CASE_POOL_PAGE_CHANGE_TIMEOUT_MS = 10000
_CASE_POOL_AUTH_WAIT_MS = 60000
_CASE_POOL_AUTH_POLL_MS = 250
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_DETAIL_IN_TEXT = re.compile(
    rf"(?:https?://kc\.zhixueyun\.com)?/?#?/study/(course|subject)/detail/({_UUID})",
    re.IGNORECASE,
)
_APP_RESOURCE = re.compile(
    rf"businessType=([12]).{{0,80}}?businessId=({_UUID})"
    rf"|businessId=({_UUID}).{{0,80}}?businessType=([12])",
    re.IGNORECASE | re.DOTALL,
)

_CLICK_MORE_SCRIPT = """
() => {
  // 精确匹配，避免「更多」误点到其它含字控件
  const exact = new Set(['查看更多', '加载更多', '更多', '展开更多', '显示更多']);
  const prefixOk = ['查看更多', '加载更多', '展开更多', '显示更多'];
  for (const el of document.querySelectorAll('a,button,span,div,li')) {
    const t = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');
    if (!t || t.length > 16) continue;
    const hit = exact.has(t) || prefixOk.some(l => t.startsWith(l) && t.length <= l.length + 4);
    if (!hit) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    el.click();
    return { ok: true, text: t.slice(0, 24) };
  }
  return { ok: false };
}
"""

# ctexpert casePool 的卡片不是 <a>：Vue 模板通过 openUrl(item.url) 打开链接。
# 只读取组件的 $data/$props，并只返回可能包含知学云课程信息的字符串，避免
# 遍历 Vue 内部对象或把页面中的登录状态带回 Python。
_READ_RUNTIME_LEARNING_LINK_VALUES_SCRIPT = r"""
() => {
  const roots = new Set();
  const app = document.querySelector('#app');
  if (app && app.__vue__) roots.add(app.__vue__);
  for (const el of document.querySelectorAll('*')) {
    if (el.__vue__) roots.add(el.__vue__);
  }

  const results = [];
  const seen = new WeakSet();
  let visited = 0;
  const maxVisited = 50000;
  const looksUseful = (value) => {
    const text = String(value || '');
    return /(?:study\/(?:course|subject)\/detail\/|business(?:Type|Id)=)/i.test(text);
  };

  const walk = (value, depth) => {
    if (visited++ >= maxVisited || depth > 10 || value == null) return;
    if (typeof value === 'string') {
      if (looksUseful(value)) results.push(value);
      return;
    }
    if (typeof value !== 'object' || seen.has(value)) return;
    seen.add(value);
    if (Array.isArray(value)) {
      for (const item of value) walk(item, depth + 1);
      return;
    }
    for (const key of Object.keys(value)) {
      try { walk(value[key], depth + 1); } catch (_) {}
    }
  };

  for (const component of roots) {
    try { walk(component.$data, 0); } catch (_) {}
    try { walk(component.$props, 0); } catch (_) {}
  }
  return [...new Set(results)];
}
"""

_CLICK_CASE_POOL_NEXT_PAGE_SCRIPT = r"""
() => {
  const visible = (el) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 1 && rect.height > 1 &&
      style.display !== 'none' && style.visibility !== 'hidden';
  };
  for (const pager of document.querySelectorAll('.el-pagination')) {
    if (!visible(pager)) continue;
    const next = pager.querySelector('.btn-next');
    const active = pager.querySelector('.el-pager .active');
    const current = Number((active && active.textContent || '').trim()) || 1;
    if (!next || next.disabled || next.classList.contains('disabled')) {
      return {ok: false, current};
    }
    next.click();
    return {ok: true, current, target: current + 1};
  }
  return {ok: false, current: 1};
}
"""

_READ_CASE_POOL_PAGE_NUMBER_SCRIPT = r"""
() => {
  for (const pager of document.querySelectorAll('.el-pagination')) {
    const rect = pager.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) continue;
    const active = pager.querySelector('.el-pager .active');
    const current = Number((active && active.textContent || '').trim());
    if (current) return current;
  }
  return 1;
}
"""

_READ_CASE_POOL_AUTH_STATE_SCRIPT = r"""
() => ({
  // 只返回是否存在，不把 token 内容带出页面。
  hasToken: Boolean(localStorage.getItem('userID')),
  hasAuthCode: Boolean(new URL(window.location.href).searchParams.get('code'))
})
"""

_CLICK_LOGIN_BUTTON_SCRIPT = r"""
() => {
  const exact = new Set([
    '登录', '立即登录', '去登录', '重新登录', '统一认证登录',
    '单点登录', '授权登录', '登录并继续', '同意并登录'
  ]);
  const candidates = document.querySelectorAll(
    'button, a, input[type="button"], input[type="submit"], [role="button"]'
  );
  for (const el of candidates) {
    const text = String(el.innerText || el.textContent || el.value || '')
      .trim().replace(/\s+/g, ' ');
    if (!exact.has(text)) continue;
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    if (rect.width < 2 || rect.height < 2 ||
        style.display === 'none' || style.visibility === 'hidden') continue;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
    el.click();
    return {ok: true, text};
  }
  return {ok: false};
}
"""

# 案例库与 subject / 培训班一样，优先直接调用已登录页面的后端接口取链接。
# token 只在页面内从 localStorage 读取并作为请求头使用，不返回到 Python。
_FETCH_CASE_POOL_LINKS_SCRIPT = r"""
async () => {
  const token = localStorage.getItem('userID') || '';
  if (!token) throw new Error('casePool token is missing');
  const headers = {
    'Accept': 'application/json, text/plain, */*',
    'Content-Type': 'application/json;charset=UTF-8',
    'token': token
  };
  const post = async (url, body) => {
    const response = await fetch(url, {
      method: 'POST',
      headers,
      body: JSON.stringify(body || {})
    });
    if (!response.ok) throw new Error(`casePool API ${response.status}: ${url}`);
    const payload = await response.json();
    if (Number(payload && payload.code) !== 200) {
      throw new Error(`casePool API rejected: ${payload && payload.msg || url}`);
    }
    return payload.data;
  };

  const tagTree = await post(
    '/expert-assist-case/tag/listTree?source=home',
    {source: 'home'}
  );
  const roots = Array.isArray(tagTree) ? tagTree : [];
  const caseRoot = roots.find(item => item && item.name === '案例资源') ||
    roots.find(item => item && Array.isArray(item.childrenList));
  const firstTag = caseRoot && Array.isArray(caseRoot.childrenList) &&
    caseRoot.childrenList[0];
  if (!firstTag || firstTag.id == null) {
    throw new Error('casePool default classify tag is missing');
  }

  const values = [];
  const seenRecords = new Set();
  const pageSize = 100;
  let pageNum = 1;
  let requestPages = 0;
  let total = null;
  while (pageNum <= 1000) {
    const data = await post(
      '/expert-assist-case/case/getCaseHomeList',
      {
        classifyTagId: firstTag.id,
        sortFiled: '',
        sortType: '',
        pageSize,
        pageNum
      }
    );
    requestPages += 1;
    const records = data && Array.isArray(data.records) ? data.records : [];
    const parsedTotal = Number(data && data.total);
    if (Number.isFinite(parsedTotal) && parsedTotal >= 0) total = parsedTotal;

    let newRecords = 0;
    for (const record of records) {
      if (!record || typeof record !== 'object') continue;
      const key = String(record.id ?? record.caseId ?? record.url ?? '');
      if (!key || seenRecords.has(key)) continue;
      seenRecords.add(key);
      newRecords += 1;
      if (typeof record.url === 'string' && record.url.trim()) {
        values.push(record.url.trim());
      }
    }

    if (!records.length || !newRecords) break;
    if (total != null && seenRecords.size >= total) break;
    pageNum += 1;
  }
  return {
    values: [...new Set(values)],
    records: seenRecords.size,
    total,
    pages: requestPages
  };
}
"""


def _extract_query_params_from_app_href(href: str) -> dict[str, list[str]]:
    parsed_url = urlparse(href.strip())
    params = parse_qs(parsed_url.query)
    if params:
        return params

    fragment = parsed_url.fragment.lstrip("/")
    fragment_parts = fragment.split("?", 1)
    if len(fragment_parts) > 1:
        return parse_qs(fragment_parts[1])
    return {}


def _normalize_learning_zone_href(href: str) -> str | None:
    href = (href or "").strip()
    if not href or "kc.zhixueyun.com" not in href:
        return None

    if "/app/" in href or "businessType=" in href:
        params = _extract_query_params_from_app_href(href)
        business_id = params.get("businessId", [None])[0]
        business_type = params.get("businessType", [None])[0]
        if business_type == "1" and business_id:
            return f"{ZHIXUEYUN_COURSE_PREFIX}{business_id}"
        if business_type == "2" and business_id:
            return f"{ZHIXUEYUN_SUBJECT_PREFIX}{business_id}"

    normalized = normalize_url(href)
    if is_compliant_url_regex(normalized):
        return normalized
    return None


def extract_learning_links_from_learning_zone_html(html_content: str) -> list[str]:
    """
    从 topic / 学习专区 HTML 提取课程与主题链接。

    实勘: 不仅 <a href>，正文/脚本里也有 #/study/.../detail/UUID。
    """
    text = html_content or ""
    links: list[str] = []

    soup = BeautifulSoup(text, "html.parser")
    for link in soup.find_all("a"):
        normalized = _normalize_learning_zone_href(link.get("href"))
        if normalized:
            links.append(normalized)
        # 部分卡片把地址写在其它属性
        for attr in ("data-href", "data-url", "data-link", "onclick"):
            raw = link.get(attr)
            if raw:
                links.extend(_extract_detail_urls_from_text(str(raw)))

    links.extend(_extract_detail_urls_from_text(text))
    return unique_urls(links)


def extract_learning_links_from_runtime_values(values: object) -> list[str]:
    """从页面运行时暴露的字符串中提取课程/主题链接。"""

    if not isinstance(values, list):
        return []
    links: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        normalized = _normalize_learning_zone_href(value)
        if normalized:
            links.append(normalized)
        links.extend(_extract_detail_urls_from_text(value))
    return unique_urls(links)


async def _fetch_learning_zone_links_direct(context, url: str) -> list[str]:
    """用浏览器 Cookie 直接请求 topic HTML；失败返回空列表触发页面兜底。"""

    try:
        response = await context.request.get(url, timeout=30_000)
        if not response.ok:
            return []
        final = urlparse(response.url)
        expected = urlparse(url)
        # 防止把 SSO 登录页或错误页当成 topic 内容。
        if (final.netloc.lower(), final.path) != (
            expected.netloc.lower(),
            expected.path,
        ):
            return []
        return extract_learning_links_from_learning_zone_html(await response.text())
    except Exception:
        return []


def _extract_detail_urls_from_text(text: str) -> list[str]:
    results: list[str] = []
    for match in _DETAIL_IN_TEXT.finditer(text or ""):
        kind = match.group(1).lower()
        uuid = match.group(2)
        prefix = (
            ZHIXUEYUN_COURSE_PREFIX if kind == "course" else ZHIXUEYUN_SUBJECT_PREFIX
        )
        candidate = f"{prefix}{uuid}"
        if is_compliant_url_regex(candidate):
            results.append(candidate)

    for match in _APP_RESOURCE.finditer(text or ""):
        if match.group(1) and match.group(2):
            business_type, business_id = match.group(1), match.group(2)
        else:
            business_id, business_type = match.group(3), match.group(4)
        if business_type == "1":
            candidate = f"{ZHIXUEYUN_COURSE_PREFIX}{business_id}"
        elif business_type == "2":
            candidate = f"{ZHIXUEYUN_SUBJECT_PREFIX}{business_id}"
        else:
            continue
        if is_compliant_url_regex(candidate):
            results.append(candidate)
    return results


async def _click_load_more_until_stable(page, status_callback=None) -> int:
    """点击「更多」类按钮直到不再出现新按钮、链接不再增加，或达到上限。"""
    clicks = 0
    prev_count = len(
        extract_learning_links_from_learning_zone_html(await page.content())
    )
    for _ in range(_MORE_CLICK_MAX):
        await dismiss_topmost_overlays_async(page, max_count=2)
        try:
            result = await page.evaluate(_CLICK_MORE_SCRIPT)
        except Exception:
            break
        if not result or not result.get("ok"):
            break
        clicks += 1
        if status_callback and clicks == 1:
            status_callback("正在展开学习专区「更多」内容…")
        await page.wait_for_timeout(_MORE_CLICK_WAIT_MS)
        new_count = len(
            extract_learning_links_from_learning_zone_html(await page.content())
        )
        # 点了但链接数不涨，且连续无效则停（避免误点空转）
        if new_count <= prev_count:
            # 再给一页懒加载机会；仍不涨则结束
            await page.wait_for_timeout(_MORE_CLICK_WAIT_MS)
            new_count = len(
                extract_learning_links_from_learning_zone_html(await page.content())
            )
            if new_count <= prev_count:
                break
        prev_count = new_count
    return clicks


async def _read_runtime_learning_links(page) -> list[str]:
    try:
        values = await page.evaluate(_READ_RUNTIME_LEARNING_LINK_VALUES_SCRIPT)
    except Exception:
        return []
    return extract_learning_links_from_runtime_values(values)


async def _fetch_ctexpert_case_pool_links(page) -> tuple[list[str], dict[str, int]]:
    """通过案例库接口直接读取所有记录 URL，不操作页面分页控件。"""

    try:
        result = await page.evaluate(_FETCH_CASE_POOL_LINKS_SCRIPT)
    except Exception:
        return [], {"records": 0, "total": 0, "pages": 0}
    if not isinstance(result, dict):
        return [], {"records": 0, "total": 0, "pages": 0}
    links = extract_learning_links_from_runtime_values(result.get("values"))
    stats: dict[str, int] = {}
    for key in ("records", "total", "pages"):
        try:
            stats[key] = max(0, int(result.get(key) or 0))
        except (TypeError, ValueError):
            stats[key] = 0
    return links, stats


async def _click_ctexpert_login_button(page) -> str | None:
    """在当前页及 iframe 中点击明确的登录按钮，返回按钮文案。"""

    frames = getattr(page, "frames", None)
    if callable(frames):
        try:
            frames = frames()
        except Exception:
            frames = None
    for frame in frames or [page]:
        try:
            result = await frame.evaluate(_CLICK_LOGIN_BUTTON_SCRIPT)
        except Exception:
            continue
        if result and result.get("ok"):
            return str(result.get("text") or "登录")
    return None


async def _ensure_ctexpert_case_pool_authenticated(
    page,
    target_url: str,
    status_callback=None,
) -> None:
    """自动完成 A→登录页→SSO→A 回跳；仅检查 token 是否存在。"""

    elapsed = 0
    saw_auth_code = False
    clicked_login = False
    initial_url = (getattr(page, "url", "") or "").strip()
    saw_redirect = False
    while elapsed <= _CASE_POOL_AUTH_WAIT_MS:
        try:
            state = await page.evaluate(_READ_CASE_POOL_AUTH_STATE_SCRIPT)
        except Exception:
            state = None
        current_url = (getattr(page, "url", "") or "").strip()
        if current_url and current_url != initial_url:
            saw_redirect = True
        if state and state.get("hasToken"):
            if is_ctexpert_case_pool_url(current_url):
                if status_callback and (clicked_login or saw_redirect):
                    status_callback("专家助手认证完成，已回到案例库")
                return
            # token 已写入但页面尚未回到案例库时，继续等正常回跳。
        saw_auth_code = saw_auth_code or bool(state and state.get("hasAuthCode"))

        if not clicked_login:
            button_text = await _click_ctexpert_login_button(page)
            if button_text:
                clicked_login = True
                if status_callback:
                    status_callback(
                        f"案例库尚未登录，已点击“{button_text}”，正在等待认证回跳"
                    )

        if elapsed >= _CASE_POOL_AUTH_WAIT_MS:
            break
        await page.wait_for_timeout(_CASE_POOL_AUTH_POLL_MS)
        elapsed += _CASE_POOL_AUTH_POLL_MS

    if clicked_login or saw_redirect or saw_auth_code:
        raise RuntimeError(
            "专家助手自动登录回跳未完成：已加载登录 Cookie 并尝试登录，但页面未在 "
            "60 秒内回到案例库；请检查当前浏览器中的认证提示后重试"
        )
    raise RuntimeError(
        "专家助手案例库未登录，且页面上未找到可点击的登录入口；项目已加载登录 "
        f"Cookie，请确认该地址仍可访问后重试：{target_url}"
    )


async def _collect_ctexpert_case_pool_pages(
    page,
    initial_links: list[str],
    status_callback=None,
) -> tuple[list[str], int]:
    """累计案例库 Vue 卡片链接，并自动翻完 Element UI 分页。"""

    links = unique_urls(initial_links + await _read_runtime_learning_links(page))
    clicks = 0
    announced = False
    for _ in range(_CASE_POOL_PAGE_MAX):
        try:
            result = await page.evaluate(_CLICK_CASE_POOL_NEXT_PAGE_SCRIPT)
        except Exception:
            break
        if not result or not result.get("ok"):
            break

        clicks += 1
        if status_callback and not announced:
            announced = True
            status_callback("正在自动翻页读取案例库课程链接…")

        target = int(result.get("target") or 0)
        elapsed = 0
        changed = False
        while elapsed < _CASE_POOL_PAGE_CHANGE_TIMEOUT_MS:
            await page.wait_for_timeout(_CASE_POOL_PAGE_WAIT_MS)
            elapsed += _CASE_POOL_PAGE_WAIT_MS
            try:
                current = int(
                    await page.evaluate(_READ_CASE_POOL_PAGE_NUMBER_SCRIPT) or 0
                )
            except Exception:
                current = 0
            if target and current >= target:
                changed = True
                break
        if not changed:
            break

        await page.wait_for_timeout(_CASE_POOL_PAGE_SETTLE_MS)
        links = unique_urls(links + await _read_runtime_learning_links(page))
        # DOM 中偶尔也会同时出现标准 href，顺手累计，不能只保留最后一页。
        try:
            links = unique_urls(
                links
                + extract_learning_links_from_learning_zone_html(await page.content())
            )
        except Exception:
            pass
    return links, clicks


async def collect_learning_links_from_learning_zone_urls(
    learning_zone_urls: list[str],
    status_callback=None,
    before_close_callback=None,
    *,
    context=None,
) -> int:
    """
    解析学习专区链接并写入学习队列。

    可传入已有 context 复用浏览器；否则自建 context。
    """
    if not learning_zone_urls:
        return 0

    total_added = 0

    async def _run_with_context(active_context) -> None:
        nonlocal total_added
        for index, url in enumerate(learning_zone_urls, start=1):
            is_case_pool = is_ctexpert_case_pool_url(url)
            source_label = "案例库" if is_case_pool else "学习专区"
            if status_callback:
                status_callback(
                    f"正在解析{source_label}链接 {index}/{len(learning_zone_urls)}"
                )
            page = await active_context.new_page()
            try:
                elapsed = 0
                learning_links: list[str] = []
                case_pool_api_stats = {"records": 0, "total": 0, "pages": 0}
                case_pool_api_direct = False
                topic_html_direct = False
                if not is_case_pool:
                    learning_links = await _fetch_learning_zone_links_direct(
                        active_context, url
                    )
                    topic_html_direct = bool(learning_links)
                    if topic_html_direct and status_callback:
                        status_callback(
                            f"已通过 HTTP 直接读取学习专区 HTML：{len(learning_links)} 条链接"
                        )

                if not topic_html_direct:
                    await page.goto(url, wait_until="load")
                    await prepare_page_after_navigation_async(
                        page, status_callback=status_callback
                    )
                    if is_case_pool:
                        await _ensure_ctexpert_case_pool_authenticated(
                            page,
                            url,
                            status_callback=status_callback,
                        )

                if is_case_pool:
                    learning_links, case_pool_api_stats = (
                        await _fetch_ctexpert_case_pool_links(page)
                    )
                    case_pool_api_direct = bool(learning_links)
                    if learning_links and status_callback:
                        status_callback(
                            "已通过案例库接口直接读取 "
                            f"{case_pool_api_stats['records']} 条记录 / "
                            f"{case_pool_api_stats['pages']} 批请求"
                        )
                    elif status_callback:
                        status_callback(
                            "案例库接口直取未返回课程链接，改用页面数据兜底"
                        )
                # 先等首屏出现任意合规链接，再点更多收全
                while (
                    not learning_links
                    and elapsed <= LEARNING_ZONE_LINK_WAIT_MILLISECONDS
                ):
                    dismissed_count = await dismiss_topmost_overlays_async(page)
                    if dismissed_count and status_callback:
                        status_callback(
                            f"已关闭 {dismissed_count} 个页面弹窗，继续读取课程链接"
                        )
                    learning_links = extract_learning_links_from_learning_zone_html(
                        await page.content()
                    )
                    if is_case_pool:
                        learning_links = unique_urls(
                            learning_links + await _read_runtime_learning_links(page)
                        )
                    if learning_links:
                        break
                    if elapsed >= LEARNING_ZONE_LINK_WAIT_MILLISECONDS:
                        break
                    await page.wait_for_timeout(LEARNING_ZONE_LINK_POLL_MILLISECONDS)
                    elapsed += LEARNING_ZONE_LINK_POLL_MILLISECONDS

                if is_case_pool and not case_pool_api_direct:
                    learning_links, page_clicks = await _collect_ctexpert_case_pool_pages(
                        page,
                        learning_links,
                        status_callback=status_callback,
                    )
                    if page_clicks and status_callback:
                        status_callback(f"已读取案例库 {page_clicks + 1} 页")
                elif not is_case_pool and not topic_html_direct:
                    more_clicks = await _click_load_more_until_stable(
                        page, status_callback=status_callback
                    )
                    if more_clicks and status_callback:
                        status_callback(f"已展开「更多」{more_clicks} 次")

                    learning_links = extract_learning_links_from_learning_zone_html(
                        await page.content()
                    )
                # 再从渲染后的 DOM 文本兜底扫一轮（含非 a 标签）
                if not topic_html_direct:
                    try:
                        body_text = await page.evaluate(
                            "() => (document.body && document.body.innerText) || ''"
                        )
                        learning_links = unique_urls(
                            learning_links
                            + _extract_detail_urls_from_text(body_text or "")
                        )
                    except Exception:
                        pass

                enqueue = await enqueue_learning_links_with_subject_expand(
                    learning_links,
                    page=page,
                    status_callback=status_callback,
                    source_label=source_label,
                )
                total_added += enqueue["learning_added"]
                if status_callback:
                    if learning_links:
                        status_callback(
                            f"已从{source_label}识别 "
                            f"{enqueue['course_links']} 条课程、"
                            f"{enqueue['subject_links']} 个主题"
                            f"（展开后学习队列 +{enqueue['learning_added']}）"
                        )
                    else:
                        status_callback(
                            f"该{source_label}暂未识别到课程链接；已处理弹窗并等待页面加载，"
                            "可改用手动选择模式"
                        )
            finally:
                await page.close()

    if context is not None:
        await _run_with_context(context)
        if before_close_callback:
            before_close_callback(total_added)
    else:
        async with create_browser_context() as (_, new_context):
            await _run_with_context(new_context)
            if before_close_callback:
                before_close_callback(total_added)

    return total_added
