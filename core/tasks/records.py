"""Lossless merging of protective exam records, across URL aliases."""

from __future__ import annotations

import json


def unique(values):
    return list(
        {json.dumps(v, sort_keys=True, ensure_ascii=False): v for v in values}.values()
    )


def merge_history(previous: dict, incoming: dict) -> dict:
    result = {**previous, **incoming}
    result["failed_models"] = unique(
        previous.get("failed_models", []) + incoming.get("failed_models", [])
    )
    result["requires_manual_submit"] = bool(
        previous.get("requires_manual_submit") or incoming.get("requires_manual_submit")
    )
    submissions = dict(previous.get("submissions", {}))
    ambiguous = previous.get("additional_submissions", []) + incoming.get(
        "additional_submissions", []
    )
    for owner, submission in incoming.get("submissions", {}).items():
        if owner not in submissions or submissions[owner] == submission:
            submissions[owner] = submission
        else:
            ambiguous.append({"account": owner, **submission})
    result["submissions"] = submissions
    result["additional_submissions"] = unique(ambiguous)
    result["verified_submissions"] = unique(
        previous.get("verified_submissions", [])
        + incoming.get("verified_submissions", [])
    )
    return result


def merge_queue(previous: dict, incoming: dict) -> dict:
    result = {**previous, **incoming}
    if "ai_failed_model_configs" in previous or "ai_failed_model_configs" in incoming:
        result["ai_failed_model_configs"] = unique(
            previous.get("ai_failed_model_configs", [])
            + incoming.get("ai_failed_model_configs", [])
        )
    if previous.get("requires_manual_submit") or incoming.get("requires_manual_submit"):
        result["requires_manual_submit"] = True
    return result
