"""Keep these v1 definitions available while their histories exist."""

from dbos import DBOS, WorkflowSerializationFormat


@DBOS.step(name="course_afk.validate_task.v1", retries_allowed=False)
async def validate_task(payload: dict) -> dict:
    from core.workflow.host import current_host

    if payload.get("schema_version") != 1:
        raise ValueError("不兼容的任务输入版本")
    task = current_host().repository.task(payload["task_id"])
    if task["account"] != payload["account"]:
        raise PermissionError("持久任务与执行账号不一致")
    return payload


@DBOS.step(name="course_afk.activity.v1", preemptible=True, retries_allowed=False)
async def activity(payload: dict) -> dict:
    from core.workflow.host import current_host

    return await current_host().perform(payload)


@DBOS.workflow(
    name="collect_entry_v1", serialization_type=WorkflowSerializationFormat.PORTABLE
)
async def collect_entry_v1(payload: dict) -> dict:
    return await activity(await validate_task(payload))


@DBOS.workflow(
    name="learn_course_v1", serialization_type=WorkflowSerializationFormat.PORTABLE
)
async def learn_course_v1(payload: dict) -> dict:
    return await activity(await validate_task(payload))


@DBOS.workflow(
    name="run_exam_v1", serialization_type=WorkflowSerializationFormat.PORTABLE
)
async def run_exam_v1(payload: dict) -> dict:
    return await activity(await validate_task(payload))


@DBOS.workflow(
    name="collect_references_v1",
    serialization_type=WorkflowSerializationFormat.PORTABLE,
)
async def collect_references_v1(payload: dict) -> dict:
    return await activity(await validate_task(payload))


WORKFLOWS = {
    "discover": collect_entry_v1,
    "learn": learn_course_v1,
    "exam": run_exam_v1,
    "references": collect_references_v1,
}
