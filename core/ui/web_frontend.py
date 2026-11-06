"""Structured synchronous frontend; only the serial worker waits for humans."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import logging
import secrets


class WebLogHandler(logging.Handler):
    def __init__(self, session):
        super().__init__()
        self.session = session

    def emit(self, record):
        try:
            self.session.log(self.format(record), record.levelname.lower())
        except Exception:
            pass


class WebFrontend:
    def __init__(self, session):
        self.session = session
        self.control = session.control
        self.options = {}
        from core.state import StateRevision
        self._revision = StateRevision()
        self._signature = None

    def close(self):
        self._revision.close()

    def refresh_dashboard(self, *, force=False):
        from core.state import collect_project_state
        signature = self._revision.signature()
        if force or signature != self._signature:
            self._signature = signature
            self.render_dashboard(collect_project_state())

    def render_dashboard(self, state):
        from core.auth.credential import load_credential_metadata, load_pending_owner_metadata
        from core.config import is_ai_configured, LEARNING_FAILURES_FILE
        from core.queues.learning import RETRIABLE_LEARNING_FAILURE_REASONS, read_learning_failures
        from core.state import credential_view, recommend_next_step
        metadata = load_credential_metadata()
        owner = load_pending_owner_metadata()
        try:
            failures = read_learning_failures(file_path=LEARNING_FAILURES_FILE)
            retriable_count = sum(1 for e in failures if e.reason in RETRIABLE_LEARNING_FAILURE_REASONS)
        except Exception:
            retriable_count = 0
        self.session.update(dashboard=dict(
            **asdict(state),
            account=metadata.account_label if metadata and state.has_credential else "未登录",
            credential=asdict(credential_view(state, metadata)),
            pending_owner=(owner.account_label or owner.account_name or owner.account_display_name) if owner else None,
            retriable_failure_count=retriable_count,
            ai_configured=bool(is_ai_configured()),
            recommended=recommend_next_step(
                has_credential=state.has_credential and not state.credential_expired,
                learning_count=state.learning_count,
                exam_count=state.exam_count,
                manual_exam_count=state.manual_exam_count,
            ),
        ))

    def show_title(self, title, subtitle=None):
        self.session.update(title=title, subtitle=subtitle)

    def _message(self, message, level):
        self.refresh_dashboard()
        self.session.update(status=message)
        self.session.log(message, level)

    def show_info(self, message): self._message(message, "info")
    def show_success(self, message): self._message(message, "success")
    def show_warning(self, message): self._message(message, "warning")
    def show_error(self, message): self._message(message, "error")

    def begin_operation(self, title, message):
        self.control.check_cancelled()
        self.session.update(title=title, status=message)

    def show_summary(self, title, rows):
        # Equal results from separate queries must remain distinguishable even
        # when the WebSocket skips the intermediate summary=None projection.
        self.session.update(summary=dict(id=secrets.token_hex(16), title=title, rows=rows))

    def _prompt(self, kind, **fields):
        return self.session.wait_prompt(self.session.prepare_prompt(kind, **fields))

    def prompt_choice(self, title, options, prompt="请选择"):
        return self._prompt("choice", title=title, options=options, message=prompt)

    def prompt_yes_no(self, message, default="N"):
        if "自动交卷" in message and isinstance(self.options.get("auto_submit"), bool):
            return self.options.pop("auto_submit")
        return self._prompt("confirm", title="请确认", message=message, default=default.upper() == "Y")

    def prompt_summary_confirmation(self, title, rows, message="确认继续处理？", default="Y"):
        return self._prompt("confirm", title=title, rows=rows, message=message, default=default.upper() == "Y")

    def prompt_multiline_input(self, messages, *, title="添加链接", cancel_message="已取消添加链接"):
        preset = self.options.pop("input_text", None)
        if isinstance(preset, str) and preset.strip():
            return preset
        return self._prompt("multiline", title=title, message="\n".join(messages))

    def pause(self, message="查看完成后返回总览"):
        return None

    def prepare_pause_with_summary(self, title, rows, message="查看完成后返回总览"):
        self.show_summary(title, rows)
        return None

    def wait_prepared_prompt(self, handle):
        if handle is not None:
            return self.session.wait_prompt(handle)
        return None

    async def wait_with_progress(self, duration, description="处理中"):
        duration = max(0, float(duration))
        started = asyncio.get_running_loop().time()
        try:
            while True:
                self.control.check_cancelled()
                elapsed = min(duration, asyncio.get_running_loop().time() - started)
                self.session.update(progress=dict(description=description, completed=elapsed, total=duration))
                if elapsed >= duration:
                    break
                await asyncio.sleep(min(0.25, duration - elapsed))
        finally:
            self.session.update(progress=None)
