#!/usr/bin/env python3
"""知学云课程页探针（在正式单链接挂课流程上增加只读数据捕获）。

用法：
    python tools/probe_course_page.py <course-or-subject-url>
    python tools/probe_course_page.py <course-url> --section-index 8
    python tools/probe_course_page.py <course-or-subject-url> --run-flow

结果保存到 ``tools/capture/course_page/``。Cookie 只用于正式浏览器上下文，
探针结果不会保存 Cookie 值；浏览器始终为可见模式。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import traceback
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.browser.session import create_browser_context
from core.config import (
    AFK_SLOW_MO,
    COOKIES_FILE,
    PROJECT_ROOT,
    ensure_data_layout,
)
from core.discovery.subject_parse import expand_subject_from_page
from core.file_ops import is_subject_detail_url, load_cookies
from core.learning.afk_runner import _process_url
from core.learning.common import ensure_course_page_ready
from core.learning.flows import (
    _activate_course_section,
    _collect_chapter_boxes,
    course_learning,
    subject_learning,
)
from core.learning.handlers import _ensure_video_player_ready


CAPTURE_DIR = PROJECT_ROOT / "tools" / "capture" / "course_page"
RESULT_FILE = CAPTURE_DIR / "latest.json"
SELECTORS = (
    "dl.chapter-list-box",
    ".chapter-list-box",
    ".chapter-list-box.required",
    ".chapter-list-box.elective",
    "[data-sectiontype]",
    ".section-item-wrapper",
    "div.course-progress div.progress",
    ".vjs-progress-control",
    ".vjs-duration-display",
    "video",
    ".video-js",
    ".prism-player",
    "[class*='video-player']",
    "[class*='videoPlayer']",
    "[class*='fullScreen-content']",
    ".study-transition-page",
    ".ant-modal-content",
    ".item.current-hover",
    "[data-resource-id]",
    ".section-type",
    ".inline-block.operation",
    "dl.chapter-list-box.focus",
    "video[controls]",
    "video source",
    "iframe",
    "object",
    "embed",
    "[class*='player']",
    "[data-region='content']",
    ".dialog-overlay",
    ".topLoading",
    ".loading",
    "[class*='empty']",
)

_DOM_PROBE_SCRIPT = r"""
(selectors) => {
  const compact = value => String(value || "").replace(/\s+/g, " ").trim();
  const sourceInfo = value => {
    if (!value) return null;
    try {
      const parsed = new URL(String(value), location.href);
      return {protocol: parsed.protocol, host: parsed.host, path: parsed.pathname};
    } catch (_error) {
      return {protocol: "", host: "", path: String(value).slice(0, 300)};
    }
  };
  const describe = element => ({
    tag: element.tagName.toLowerCase(),
    id: element.id || "",
    className: String(element.className?.baseVal || element.className || ""),
    dataSectiontype: element.getAttribute("data-sectiontype"),
    dataResourceId: element.getAttribute("data-resource-id"),
    visible: Boolean(
      element.getClientRects().length &&
      getComputedStyle(element).visibility !== "hidden" &&
      getComputedStyle(element).display !== "none"
    ),
    source: sourceInfo(
      element.currentSrc || element.src || element.data || element.getAttribute("src")
    ),
    media: element instanceof HTMLMediaElement ? {
      readyState: element.readyState,
      networkState: element.networkState,
      paused: element.paused,
      duration: Number.isFinite(element.duration) ? element.duration : null,
      currentTime: element.currentTime,
      errorCode: element.error?.code || null,
      errorMessage: String(element.error?.message || "").slice(0, 500),
    } : null,
    text: compact(element.innerText || element.textContent).slice(0, 500),
  });

  const selectorResults = selectors.map(selector => {
    try {
      const elements = Array.from(document.querySelectorAll(selector));
      return {
        selector,
        count: elements.length,
        samples: elements.slice(0, 12).map(describe),
      };
    } catch (error) {
      return {selector, count: 0, error: String(error)};
    }
  });

  const interestingPattern =
    /chapter|section|video|player|course|progress|error|empty|no-data|loading|auth/i;
  const interesting = Array.from(document.querySelectorAll("[class]"))
    .filter(element => interestingPattern.test(String(element.className || "")))
    .slice(0, 160)
    .map(describe);

  const subjectItems = Array.from(
    document.querySelectorAll(".item.current-hover, [data-resource-id]")
  ).slice(0, 400).map(describe);

  const contentRegion = document.querySelector("[data-region='content']");
  const cookieText = String(document.cookie || "");
  const hasCookie = name => new RegExp(`(?:^|;\\s*)${name}=`).test(cookieText);
  const hasStorageKey = name => {
    try {
      return [window.localStorage, window.sessionStorage].some(storage =>
        Array.from({length: storage.length}, (_, index) => storage.key(index))
          .some(key => String(key || "").toLowerCase() === name.toLowerCase())
      );
    } catch (_error) {
      return false;
    }
  };

  return {
    selectorResults,
    interesting,
    subjectItems,
    documentReadyState: document.readyState,
    contentRegionHtml: String(contentRegion?.innerHTML || "").slice(0, 5000),
    authPresence: {
      authorizationCookie: hasCookie("authorization"),
      saveCookie: hasCookie("save_cookie"),
      tokenStorageKey: hasStorageKey("token"),
      saasTokenStorageKey: hasStorageKey("saas-token"),
    },
    bodyText: compact(document.body?.innerText || "").slice(0, 10000),
  };
}
"""


def _sanitize_url(url: str) -> str:
    sanitized = re.sub(
        r"(kc\.zhixueyun\.com/oauth/#login/)[^?#\s]+",
        r"\1<redacted>",
        str(url or ""),
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"([?&#/](?:access_token|refresh_token|authorization|token|code|auth_key|signature|sign|policy|expires|key-pair-id)=)[^&\s]+",
        r"\1<redacted>",
        sanitized,
        flags=re.IGNORECASE,
    )


_SENSITIVE_KEY = re.compile(
    r"authorization|cookie|credential|password|secret|session|token",
    re.IGNORECASE,
)


def _redact_json_value(value):
    if isinstance(value, dict):
        return {
            key: "<redacted>" if _SENSITIVE_KEY.search(str(key)) else _redact_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]
    return value


def _redact_text(text: str) -> str:
    text = re.sub(
        r"(?i)(bearer\s+)[a-z0-9._~+/=-]+",
        r"\1<redacted>",
        str(text or ""),
    )
    return re.sub(
        r'''(?ix)
        (["']?(?:authorization|cookie|credential|password|secret|session|token)["']?
        \s*[:=]\s*["']?)
        ([^"'&,;\s}\]]+)
        ''',
        r"\1<redacted>",
        text,
    )


def _redact_response_body(
    text: str,
    content_type: str,
    *,
    limit: int = 20000,
) -> str:
    if "json" in content_type.lower():
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            pass
        else:
            return json.dumps(
                _redact_json_value(parsed),
                ensure_ascii=False,
                separators=(",", ":"),
            )[:limit]
    return _redact_text(text)[:limit]


def _event_value(value, name: str, default=None):
    result = getattr(value, name, default)
    return result() if callable(result) else result


def _is_relevant_response(response) -> bool:
    url = str(_event_value(response, "url", "") or "")
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if not (host == "zhixueyun.com" or host.endswith(".zhixueyun.com")):
        return False
    request = _event_value(response, "request")
    resource_type = str(_event_value(request, "resource_type", "") or "").lower()
    status = int(_event_value(response, "status", 0) or 0)
    path = parsed.path.lower()
    return bool(
        status >= 400
        or resource_type in {"document", "xhr", "fetch", "media"}
        or "/api/" in path
        or "/oauth/" in path
        or "/bundle/" in path
    )


async def _capture_page(page) -> dict[str, object]:
    return {
        "final_url": _sanitize_url(page.url),
        "title": await page.title(),
        "dom": await page.evaluate(_DOM_PROBE_SCRIPT, list(SELECTORS)),
    }


async def _save_stage_capture(
    page,
    stage: str,
    *,
    run_dir: Path,
    captures: list[dict[str, object]],
) -> None:
    captured_at = datetime.now()
    payload = {
        "stage": stage,
        "captured_at": captured_at.astimezone().isoformat(),
        **(await _capture_page(page)),
    }
    index = len(captures) + 1
    stem = f"{index:02d}-{stage}"
    json_path = run_dir / f"{stem}.json"
    html_path = run_dir / f"{stem}.html"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    html_path.write_text(await page.content(), encoding="utf-8")
    payload["json_file"] = str(json_path)
    payload["html_file"] = str(html_path)
    captures.append(payload)


def _save_result(result: dict[str, object], *, run_dir: Path) -> Path:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    result_path = run_dir / "result.json"
    result["result_file"] = str(result_path)
    result_text = json.dumps(result, ensure_ascii=False, indent=2)
    result_path.write_text(result_text, encoding="utf-8")
    RESULT_FILE.write_text(result_text, encoding="utf-8")
    return result_path


def _append_bounded(items: list[dict[str, object]], item: dict[str, object], limit: int) -> None:
    if len(items) < limit:
        items.append(item)


async def _capture_response_event(
    response,
    *,
    network_events: list[dict[str, object]],
) -> None:
    if not _is_relevant_response(response):
        return

    request = _event_value(response, "request")
    resource_type = str(_event_value(request, "resource_type", "") or "")
    status = int(_event_value(response, "status", 0) or 0)
    event: dict[str, object] = {
        "event": "response",
        "captured_at": datetime.now().astimezone().isoformat(),
        "url": _sanitize_url(str(_event_value(response, "url", "") or "")),
        "method": str(_event_value(request, "method", "") or ""),
        "resource_type": resource_type,
        "status": status,
        "status_text": str(_event_value(response, "status_text", "") or ""),
    }
    _append_bounded(network_events, event, 500)

    if resource_type.lower() not in {"xhr", "fetch"} and status < 400:
        return
    try:
        headers = await response.all_headers()
        content_type = str(headers.get("content-type") or "")
        event["content_type"] = content_type
        if status < 400 and not any(
            kind in content_type.lower() for kind in ("json", "text", "html")
        ):
            return
        body = await response.text()
        if body:
            body_limit = (
                250000
                if "/subject/chapter-progress" in str(event["url"])
                else 20000
            )
            event["body_preview"] = _redact_response_body(
                body,
                content_type,
                limit=body_limit,
            )
            event["body_truncated"] = len(body) > body_limit
    except Exception as exc:
        event["body_capture_error"] = type(exc).__name__


def _attach_page_listeners(
    page,
    *,
    network_events: list[dict[str, object]],
    console_events: list[dict[str, object]],
    response_tasks: set[asyncio.Task],
    observed_page_ids: set[int],
) -> None:
    page_id = id(page)
    if page_id in observed_page_ids:
        return
    observed_page_ids.add(page_id)

    on = getattr(page, "on", None)
    if not callable(on):
        return

    def track_response(response) -> None:
        task = asyncio.create_task(
            _capture_response_event(response, network_events=network_events)
        )
        response_tasks.add(task)
        task.add_done_callback(response_tasks.discard)

    def capture_failed_request(request) -> None:
        failure = _event_value(request, "failure", "") or ""
        _append_bounded(
            network_events,
            {
                "event": "requestfailed",
                "captured_at": datetime.now().astimezone().isoformat(),
                "url": _sanitize_url(str(_event_value(request, "url", "") or "")),
                "method": str(_event_value(request, "method", "") or ""),
                "resource_type": str(
                    _event_value(request, "resource_type", "") or ""
                ),
                "failure": _redact_text(str(failure)),
            },
            500,
        )

    def capture_console(message) -> None:
        location = _event_value(message, "location", {}) or {}
        _append_bounded(
            console_events,
            {
                "event": "console",
                "captured_at": datetime.now().astimezone().isoformat(),
                "type": str(_event_value(message, "type", "") or ""),
                "text": _redact_text(
                    str(_event_value(message, "text", "") or "")
                )[:5000],
                "location": {
                    "url": _sanitize_url(str(location.get("url") or "")),
                    "line": location.get("lineNumber"),
                    "column": location.get("columnNumber"),
                },
            },
            300,
        )

    def capture_page_error(error) -> None:
        _append_bounded(
            console_events,
            {
                "event": "pageerror",
                "captured_at": datetime.now().astimezone().isoformat(),
                "text": _redact_text(str(error))[:5000],
            },
            300,
        )

    on("response", track_response)
    on("requestfailed", capture_failed_request)
    on("console", capture_console)
    on("pageerror", capture_page_error)


async def main(
    target_url: str,
    *,
    run_flow: bool = False,
    flow_timeout: int = 90,
    section_index: int | None = None,
) -> dict[str, object]:
    target_url = (target_url or "").strip()
    if not target_url:
        raise ValueError("未提供课程页面 URL")
    if section_index is not None and section_index < 1:
        raise ValueError("章节序号必须从 1 开始")

    ensure_data_layout()
    cookies = load_cookies(COOKIES_FILE)
    started_at = datetime.now()
    run_dir = CAPTURE_DIR / f"probe_{started_at:%Y%m%d_%H%M%S_%f}"
    run_dir.mkdir(parents=True, exist_ok=True)
    failure_file = run_dir / "挂课失败链接.json"
    captures: list[dict[str, object]] = []
    network_events: list[dict[str, object]] = []
    console_events: list[dict[str, object]] = []
    response_tasks: set[asyncio.Task] = set()
    observed_page_ids: set[int] = set()
    result: dict[str, object] = {
        "requested_url": _sanitize_url(target_url),
        "probed_at": started_at.astimezone().isoformat(),
        "cookies_file": str(COOKIES_FILE),
        "cookies_loaded": bool(cookies),
        "cookie_count": len(cookies),
        "cookie_domains": sorted(
            {str(item.get("domain") or "") for item in cookies if item.get("domain")}
        ),
        "run_flow": run_flow,
        "flow_timeout_seconds": flow_timeout if run_flow or section_index else None,
        "target_section_index": section_index,
        "browser_slow_mo": AFK_SLOW_MO,
        "headless": False,
        "capture_dir": str(run_dir),
        "captures": captures,
    }

    async def capture_callback(page, stage: str) -> None:
        _attach_page_listeners(
            page,
            network_events=network_events,
            console_events=console_events,
            response_tasks=response_tasks,
            observed_page_ids=observed_page_ids,
        )
        if stage == "page_created":
            return
        await _save_stage_capture(
            page,
            stage,
            run_dir=run_dir,
            captures=captures,
        )

    async def inspect_only(_page) -> None:
        return None

    async def inspect_subject(page) -> None:
        expanded = await expand_subject_from_page(page, target_url)
        result["subject_expand"] = asdict(expanded)

    async def inspect_course_section(page) -> None:
        await ensure_course_page_ready(page)
        chapters = await _collect_chapter_boxes(page)
        assert section_index is not None
        if section_index > len(chapters):
            raise ValueError(
                f"课程仅识别到 {len(chapters)} 节，无法探测第 {section_index} 节"
            )
        track, box = chapters[section_index - 1]
        result["target_section"] = {
            "index": section_index,
            "track": track,
            "section_type": await box.get_attribute("data-sectiontype"),
            "text": (await box.locator(".section-item-wrapper").inner_text())[:1000],
        }
        await _activate_course_section(page, box)
        if result["target_section"]["section_type"] in {"5", "6"}:
            await _ensure_video_player_ready(page, box=box)
        await page.wait_for_timeout(2000)

    if section_index is not None:
        if is_subject_detail_url(target_url):
            raise ValueError("--section-index 只适用于课程详情链接")
        handler = inspect_course_section
    elif is_subject_detail_url(target_url):
        handler = subject_learning if run_flow else inspect_subject
    else:
        handler = course_learning if run_flow else inspect_only
    async with create_browser_context(
        cookies_path=COOKIES_FILE,
        headless=False,
        slow_mo=AFK_SLOW_MO,
    ) as (_, context):
        try:
            keep_pending = await asyncio.wait_for(
                _process_url(
                    context,
                    target_url,
                    handler,
                    capture_callback=capture_callback,
                    failure_file=failure_file,
                ),
                timeout=max(1, int(flow_timeout)),
            )
            result["flow"] = {
                "status": "kept_pending" if keep_pending else "completed",
                "keep_pending": keep_pending,
            }
        except asyncio.TimeoutError:
            result["flow"] = {
                "status": "timeout",
                "message": f"正式单链接挂课流程超过 {flow_timeout} 秒，探针已停止",
            }
        except Exception as exc:
            result["flow"] = {
                "status": "error",
                "error_type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }
        finally:
            await asyncio.sleep(0)
            if response_tasks:
                await asyncio.gather(*list(response_tasks), return_exceptions=True)

    network_file = run_dir / "network.json"
    console_file = run_dir / "console.json"
    network_file.write_text(
        json.dumps(network_events, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    console_file.write_text(
        json.dumps(console_events, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    result["network_capture"] = {
        "file": str(network_file),
        "event_count": len(network_events),
    }
    result["console_capture"] = {
        "file": str(console_file),
        "event_count": len(console_events),
    }

    if failure_file.exists():
        try:
            result["failure_entries"] = json.loads(
                failure_file.read_text(encoding="utf-8")
            )
        except Exception as exc:
            result["failure_file_error"] = str(exc)
    _save_result(result, run_dir=run_dir)

    return result


if __name__ == "__main__":
    reconfigure_stdout = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure_stdout):
        reconfigure_stdout(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="探测知学云课程页 DOM 与正式挂课流程")
    parser.add_argument("url", help="课程详情或主题详情 URL")
    parser.add_argument(
        "--run-flow",
        action="store_true",
        help="调用正式 course_learning 流程并捕获结果",
    )
    parser.add_argument(
        "--flow-timeout",
        type=int,
        default=90,
        help="正式挂课流程的探针超时秒数（默认 90）",
    )
    parser.add_argument(
        "--section-index",
        type=int,
        help="仅激活并采集指定课程章节（从 1 开始，不执行整段计时）",
    )
    args = parser.parse_args()

    probe_result = asyncio.run(
        main(
            args.url,
            run_flow=args.run_flow,
            flow_timeout=args.flow_timeout,
            section_index=args.section_index,
        )
    )
    print(
        json.dumps(
            {
                "flow": probe_result.get("flow"),
                "capture_count": len(probe_result.get("captures", [])),
                "capture_dir": probe_result.get("capture_dir"),
                "result_file": probe_result.get("result_file"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
