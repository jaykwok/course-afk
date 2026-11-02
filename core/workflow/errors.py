from core.abort import (
    BrowserCertificateError,
    LearningFlowError,
    NoPermissionError,
    UserAbortRequested,
    UserCancelRequested,
    WafBlockError,
)
from core.workflow.contracts import Status, TaskOutcome


def error_outcome(task_id, exc):
    from core.browser.session import (
        is_target_closed_exception,
        raise_for_certificate_error,
    )

    try:
        raise_for_certificate_error(exc)
    except BrowserCertificateError as certificate:
        return TaskOutcome(
            task_id,
            Status.BLOCKED,
            ["certificate_error"],
            stop_scope="batch",
            details={"message": str(certificate)},
        )
    if isinstance(
        exc, (UserAbortRequested, UserCancelRequested, WafBlockError)
    ) or is_target_closed_exception(exc):
        return TaskOutcome(
            task_id,
            Status.INTERRUPTED,
            [getattr(exc, "reason", "cancelled")],
            stop_scope="batch",
            details={"message": str(exc)},
        )
    from core.exam.answers import ExamAiConfigurationError
    from core.exam.flow import ExamQuestionExtractionError

    if isinstance(exc, (ExamAiConfigurationError, ExamQuestionExtractionError)):
        return TaskOutcome(
            task_id,
            Status.BLOCKED,
            [
                "exam_configuration"
                if isinstance(exc, ExamAiConfigurationError)
                else "question_extraction"
            ],
            stop_scope="batch",
            details={"message": str(exc)},
        )
    if isinstance(exc, NoPermissionError):
        return TaskOutcome(
            task_id, Status.BLOCKED, [exc.reason], details={"message": str(exc)}
        )
    if isinstance(exc, LearningFlowError):
        return TaskOutcome(
            task_id,
            Status.WAITING_SYNC if exc.reason == "sync_timeout" else Status.FAILED,
            [exc.reason],
            retry_policy="next_operation",
            details=exc.detail,
        )
    return TaskOutcome(
        task_id,
        Status.FAILED,
        ["unexpected_error"],
        details={"error_type": type(exc).__name__},
    )
