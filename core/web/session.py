"""Bounded projections and versioned prompts shared by HTTP and the controller."""
from __future__ import annotations

from collections import deque
from copy import deepcopy
import secrets
import threading

from core.abort import UserCancelRequested
from core.diagnostics import redact_text


class ProtocolError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def public(value):
    if isinstance(value, str):
        return redact_text(value)[:12000]
    if isinstance(value, (list, tuple)):
        return [public(v) for v in value[:200]]
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items()}
    return value


class WebSession:
    def __init__(self, control):
        self.control = control
        self.condition = threading.Condition(control.lock)
        self.instance_id = secrets.token_hex(16)
        self.seq = 0
        self.logs = deque(maxlen=400)
        self.dropped_logs = 0
        self.view = dict(title="中国电信网上大学", status="就绪", dashboard={},
                         summary=None, progress=None, prompt=None)
        self._answer = None
        self._answered_prompt = None

    def update(self, **fields):
        with self.condition:
            self.view.update(public(fields))
            self.seq += 1
            self.condition.notify_all()

    def log(self, message, level="info"):
        with self.condition:
            self.seq += 1
            if len(self.logs) == self.logs.maxlen:
                self.dropped_logs += 1
            self.logs.append(dict(seq=self.seq, operation_id=self.control.operation_id,
                                  level=level, message=public(str(message))))

    def snapshot(self):
        with self.condition:
            return dict(instance_id=self.instance_id, operation_id=self.control.operation_id,
                        seq=self.seq, type="snapshot", control=self.control.snapshot(),
                        **deepcopy(self.view), logs=list(self.logs), dropped_logs=self.dropped_logs)

    def prepare_prompt(self, kind, **fields):
        with self.condition:
            self.control.check_cancelled()
            if self.control.exit_requested:
                raise UserCancelRequested("管理页已关闭，人工确认已取消，待办保留")
            if self.view["prompt"] is not None:
                raise RuntimeError("已有未回答提示")
            prompt = dict(id=secrets.token_hex(16), operation_id=self.control.operation_id,
                          version=self.seq + 1, kind=kind, **public(fields))
            self._answer = self._answered_prompt = None
            self.update(prompt=prompt)
            return prompt

    def answer(self, prompt_id, operation_id, version, value):
        with self.condition:
            prompt = self.view["prompt"]
            if not prompt or (prompt["id"], prompt["operation_id"], prompt["version"]) != (prompt_id, operation_id, version):
                raise ProtocolError("提示已失效，请刷新状态")
            if self.control.cancelled or self.control.exit_requested or self._answered_prompt:
                raise ProtocolError("提示已回答或操作已取消")
            kind = prompt["kind"]
            valid = ((kind == "choice" and type(value) is int and 1 <= value <= len(prompt["options"]))
                     or (kind == "confirm" and type(value) is bool)
                     or (kind == "pause" and value is None)
                     or (kind == "multiline" and isinstance(value, str) and 0 < len(value.strip()) <= 50000))
            if not valid:
                raise ProtocolError("回答格式不正确", 400)
            self._answer, self._answered_prompt = value, prompt_id
            self.condition.notify_all()

    def wait_prompt(self, handle):
        with self.condition:
            try:
                while self._answered_prompt != handle["id"]:
                    self.control.check_cancelled()
                    if self.control.exit_requested:
                        raise UserCancelRequested("管理页已关闭，待办保留")
                    self.condition.wait(0.25)
                # Cancellation wins over an answer received just before cancellation.
                self.control.check_cancelled()
                return self._answer
            finally:
                if self.view["prompt"] and self.view["prompt"]["id"] == handle["id"]:
                    self.update(prompt=None)

    def release_waiters(self):
        with self.condition:
            self.condition.notify_all()
