"""Exam history facade; stable identity and all unresolved writes survive queues."""
from __future__ import annotations

from pathlib import Path

from core.storage import serialized
from core.tasks.identity import canonical_url, parse_entry
from core.tasks.repository import now
from core.tasks.store import read_history, write_history, repository_for


def _read(queue_file):
    return read_history(queue_file)


def failed_configs(url: str, queue_file: Path) -> list[dict]:
    return list(_read(queue_file)["entries"].get(canonical_url(url), {}).get("failed_models", []))


def manual_submission_required(url: str, queue_file: Path) -> bool:
    return bool(_read(queue_file)["entries"].get(canonical_url(url),{}).get("requires_manual_submit"))


@serialized
def protect_manual_submission(url: str, queue_file: Path):
    data=_read(queue_file)
    data['entries'].setdefault(canonical_url(url),{})['requires_manual_submit']=True
    write_history(queue_file,data)


@serialized
def record_failures(url: str, configs: list[dict], queue_file: Path) -> None:
    if not configs:
        return
    data = _read(queue_file)
    entry = data["entries"].setdefault(canonical_url(url), {})
    previous = entry.setdefault("failed_models", [])
    for config in configs:
        if config not in previous:
            previous.append(config)
    write_history(queue_file, data)


@serialized
def record_submission_intent(url: str, model_config: dict, queue_file: Path, *, baseline=None) -> None:
    account = model_config.get("account") or "unbound"
    repo = repository_for(queue_file)
    task_id = repo.upsert(parse_entry(url, account))
    repo.intent(task_id, "submit", {"model_config": model_config, "server": baseline or {}})
    data = _read(queue_file)
    entry = data["entries"].setdefault(canonical_url(url), {})
    submissions = entry.setdefault("submissions", {})
    previous = submissions.get(account)
    if previous and previous.get("state") == "unverified":
        return
    if previous:
        entry.setdefault("verified_submissions", []).append({"account": account, **previous})
    submissions[account] = {"state": "unverified", "model_config": model_config, "baseline": baseline or {}, "at": now()}
    write_history(queue_file, data)


def pending_submission(url: str, queue_file: Path, *, account: str = "unbound") -> dict | None:
    data = _read(queue_file)
    entry = data["entries"].get(canonical_url(url), {})
    submission = entry.get("submissions", {}).get(account)
    repo = repository_for(queue_file)
    pending = repo.pending_intents(parse_entry(url, account).key.task_id, "submit")
    legacy = [s for s in entry.get("legacy_submissions", []) if s.get("account")==account and s.get("state")=="unverified"]
    if pending or legacy or isinstance(submission, dict) and submission.get("state") == "unverified":
        return {**(submission or {}), "state": "unverified", "intents": pending, "legacy": legacy}
    return None


@serialized
def finish_submission(url: str, state: str, queue_file: Path, *, account: str = "unbound") -> None:
    repo = repository_for(queue_file)
    task_id = parse_entry(url, account).key.task_id
    pending = repo.pending_intents(task_id, "submit")
    if len(pending) == 1:
        repo.confirm_intent(pending[0]["action_id"], state, {"result": state})
    data = _read(queue_file)
    submission = data["entries"].get(canonical_url(url), {}).get("submissions", {}).get(account)
    if submission is not None:
        submission["state"] = state
        write_history(queue_file, data)
