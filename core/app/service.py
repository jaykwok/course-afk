"""CLI and TUI share selection, sequential dispatch and business outcomes."""

from __future__ import annotations

import asyncio

from core.abort import UserCancelRequested
from core.workflow.contracts import Status


class ApplicationService:
    def __init__(self, host=None):
        if host is None:
            from core.workflow.host import get_host

            host = get_host()
        self.host = host

    async def batch(self, urls, kind, *, options=None, status_callback=None):
        if asyncio.get_running_loop() is not self.host.loop:
            return await asyncio.wrap_future(
                self.host.submit(
                    self.batch(
                        urls, kind, options=options, status_callback=status_callback
                    )
                )
            )
        from core.tasks.identity import canonical_url, parse_entry
        from core.workflow.host import VERSION

        selected = list(dict.fromkeys(canonical_url(u) for u in urls))
        account = self.host.identity_provider()
        if account == "unbound":
            raise ValueError("缺少可验证的账号标识，请先更新登录凭证")
        # Retain every selected task before the first browser can fail. Planned
        # runs without an engine row are dispatched with the same ID on retry.
        for url in selected:
            entry = parse_entry(
                url, account, kind="references" if kind == "references" else None
            )
            task_id = self.host.repository.upsert(entry)
            self.host.repository.plan(task_id, kind, options or {}, VERSION)
        results = []
        for url in selected:
            if status_callback:
                status_callback(f"{kind} {len(results) + 1}/{len(urls)}: {url}")
            try:
                result = await self.host.execute(
                    url, kind, options=options, status_callback=status_callback
                )
            except asyncio.CancelledError:
                raise UserCancelRequested(
                    "已取消本轮操作，当前和剩余任务保留"
                ) from None
            results.append(result)
            if status_callback and result["status"] != Status.COMPLETED:
                status_callback(
                    f"{result['status']}: {', '.join(result['reason_codes'])}"
                )
            if result["stop_scope"] == "batch":
                if "certificate_error" in result["reason_codes"]:
                    from core.abort import BrowserCertificateError

                    raise BrowserCertificateError("ERR_CERT_AUTHORITY_INVALID")
                raise UserCancelRequested(
                    result.get("details", {}).get("message")
                    or "本轮操作已停止，未完成任务保留"
                )
        return results

    async def learn(self, status_callback=None):
        from core.queues.learning import read_learning_urls, read_learning_failures

        urls = read_learning_urls()
        urls.extend(
            e.url for e in read_learning_failures() if e.reason == "url_type_pending"
        )
        return await self.batch(urls, "learn", status_callback=status_callback)

    async def exams(self, *, auto_submit=False, status_callback=None):
        from core.queues.exam import read_exam_urls
        from core.exam.routing import requeue_ai_exam_reviews

        requeue_ai_exam_reviews()
        return await self.batch(
            read_exam_urls(),
            "exam",
            options={"auto_submit": auto_submit},
            status_callback=status_callback,
        )

    async def references(self, urls, status_callback=None):
        return await self.batch(urls, "references", status_callback=status_callback)
