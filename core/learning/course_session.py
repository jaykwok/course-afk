"""The ordered course-page lifecycle and the single media completion rule."""

from __future__ import annotations

from core.browser.bootstrap import navigate_platform_page

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import time

from core.learning.progress import section_completed_on_server


class CompletionEvidence(StrEnum):
    SERVER_COMPLETE = "SERVER_COMPLETE"
    SERVER_INCOMPLETE = "SERVER_INCOMPLETE"
    UNKNOWN = "UNKNOWN"


class CompletionPolicy:
    @staticmethod
    def evidence(state: dict | None) -> CompletionEvidence:
        if section_completed_on_server(state):
            return CompletionEvidence.SERVER_COMPLETE
        if state is not None:
            return CompletionEvidence.SERVER_INCOMPLETE
        return CompletionEvidence.UNKNOWN

    @classmethod
    def complete(cls, state):
        return cls.evidence(state) == CompletionEvidence.SERVER_COMPLETE

    @classmethod
    def record(cls, states, *, course_url=None, source="section"):
        from core.workflow.host import activity_context, current_host
        from core.tasks.identity import TaskKey, Entry, parse_entry
        from core.tasks.repository import encode, now
        import json

        context = activity_context.get()
        if not context:
            return
        repo = current_host().repository
        parent = repo.task(context["task_id"])
        if course_url:
            entry = parse_entry(course_url, parent["account"])
            if entry.key.kind == "course" and entry.key.task_id != parent["task_id"]:
                child = repo.upsert(entry)
                repo.link(
                    parent["task_id"],
                    child,
                    source="course_page",
                    evidence={"url": entry.url},
                )
                parent = repo.task(child)
        identity = json.loads(parent["identity"])
        with repo.connect() as db:
            for section_id, state in (states or {}).items():
                key = TaskKey(
                    identity["tenant"],
                    identity["account"],
                    "section",
                    parent["task_id"] + ":" + section_id,
                )
                child = repo.upsert(Entry(key, parent["url"], ()), db=db)
                repo.link(
                    parent["task_id"],
                    child,
                    db=db,
                    source=source,
                    evidence={"section_id": section_id},
                )
                db.execute(
                    "UPDATE tasks SET status=?,outcome=?,updated_at=? WHERE task_id=?",
                    (
                        "COMPLETED" if cls.complete(state) else "WAITING_SYNC",
                        encode({"evidence": cls.evidence(state), "server": state}),
                        now(),
                        child,
                    ),
                )
            repo.refresh_ancestors(
                [
                    row[0]
                    for row in db.execute(
                        "SELECT child FROM task_edges WHERE parent=?",
                        (parent["task_id"],),
                    )
                ],
                db=db,
            )


class CourseSession:
    def __init__(self, page):
        self.page = page

    async def prepare(self, url=None):
        from core.learning.flows import (
            _ensure_course_learning_view,
            ensure_course_page_ready,
        )

        if url:
            await navigate_platform_page(self.page, url, wait_until="load")
        await self.page.wait_for_load_state("load")
        await self.maintain()
        await ensure_course_page_ready(self.page)
        await _ensure_course_learning_view(self.page)

    async def maintain(self):
        from core.learning.flows import (
            handle_rating_popup,
        )
        from core.browser.overlays import dismiss_topmost_overlays_async
        from core.learning.popups import check_and_handle_rating_popup, handle_archive_continue_popup

        await handle_rating_popup(self.page)
        await handle_archive_continue_popup(self.page)
        await check_and_handle_rating_popup(self.page)
        await dismiss_topmost_overlays_async(self.page, max_count=2)


@dataclass
class PhaseBudget:
    """Total confirmation time includes requests/actions; restart never resets it."""

    seconds: float
    phase: str
    offline_consumes: bool = True

    def __post_init__(self):
        from core.workflow.host import activity_context, current_host

        context = activity_context.get()
        self.repo = current_host().repository if context else None
        self.run_id = context["run_id"] if context else None
        self.consumed = 0.0
        if self.repo:
            with self.repo.connect() as db:
                row = db.execute(
                    "SELECT * FROM phases WHERE run_id=? AND phase=?",
                    (self.run_id, self.phase),
                ).fetchone()
                if row:
                    # A confirmation deadline continues while offline. Effective
                    # video watching has a separate plan and never credits it.
                    elapsed = (
                        datetime.now(timezone.utc)
                        - datetime.fromisoformat(row["started_at"])
                    ).total_seconds()
                    self.consumed = (
                        max(row["consumed"], elapsed)
                        if self.offline_consumes
                        else row["consumed"]
                    )
                    self.seconds = row["budget"]
                else:
                    db.execute(
                        "INSERT INTO phases VALUES(?,?,?,?,0)",
                        (
                            self.run_id,
                            self.phase,
                            datetime.now(timezone.utc).isoformat(),
                            self.seconds,
                        ),
                    )
        self.started = time.monotonic()

    @property
    def remaining(self):
        return max(0, self.seconds - self.consumed - (time.monotonic() - self.started))

    def save(self):
        if self.repo:
            with self.repo.connect() as db:
                db.execute(
                    "UPDATE phases SET consumed=? WHERE run_id=? AND phase=?",
                    (
                        self.consumed + time.monotonic() - self.started,
                        self.run_id,
                        self.phase,
                    ),
                )

    def extend(self, amount, maximum):
        """Reserve stall compensation once across restarts of this activity."""
        extra = amount
        if self.repo:
            with self.repo.connect() as db:
                key = self.phase + ":stall"
                db.execute(
                    "INSERT OR IGNORE INTO phases VALUES(?,?,?,?,0)",
                    (self.run_id, key, datetime.now(timezone.utc).isoformat(), maximum),
                )
                row = db.execute(
                    "SELECT budget,consumed FROM phases WHERE run_id=? AND phase=?",
                    (self.run_id, key),
                ).fetchone()
                extra = min(amount, max(0, row["budget"] - row["consumed"]))
                db.execute(
                    "UPDATE phases SET consumed=consumed+? WHERE run_id=? AND phase=?",
                    (extra, self.run_id, key),
                )
                db.execute(
                    "UPDATE phases SET budget=budget+? WHERE run_id=? AND phase=?",
                    (extra, self.run_id, self.phase),
                )
        self.seconds += extra
        return extra
