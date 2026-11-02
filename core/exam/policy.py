"""Shared last-boundary protection for course and standalone submissions."""

from contextvars import ContextVar
from dataclasses import dataclass

_permit = ContextVar("exam_submit_permit", default=None)


@dataclass(frozen=True)
class ExamPolicy:
    url: str
    account: str
    queue_file: object
    action_ids: tuple[str, ...]

    @classmethod
    def arm(cls, url, account, queue_file):
        from core.queues.history import pending_submission

        pending = pending_submission(url, queue_file, account=account)
        return _permit.set(
            cls(
                url,
                account,
                queue_file,
                tuple(i["action_id"] for i in (pending or {}).get("intents", [])),
            )
        )

    @staticmethod
    def disarm(token):
        _permit.reset(token)

    @staticmethod
    def verify():
        from core.workflow.host import activity_context, current_host
        from core.queues.history import pending_submission
        from core.abort import UserCancelRequested

        activity = activity_context.get()
        if activity is None:
            return  # Standalone DOM helpers/tests do not authorize a workflow.
        current_host().authorize(activity)
        permit = _permit.get()
        if permit is None or permit.account != activity["account"]:
            raise UserCancelRequested("未取得本次交卷授权，保留待办供核对")
        pending = pending_submission(
            permit.url, permit.queue_file, account=permit.account
        )
        ids = tuple(i["action_id"] for i in (pending or {}).get("intents", []))
        if len(ids) != 1 or ids != permit.action_ids or (pending or {}).get("legacy"):
            raise UserCancelRequested("交卷意图发生变化或存在未确认记录，禁止重复交卷")
