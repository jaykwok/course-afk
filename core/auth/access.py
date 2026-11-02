"""One credential prerequisite for task-producing operations."""

class CredentialUnavailableError(ValueError):
    pass


def require_task_account():
    from core.auth.credential import load_credential_metadata
    from core.state import has_valid_credential
    from core.tasks.identity import account_id

    metadata = load_credential_metadata()
    if metadata and not metadata.account_name.strip():
        raise CredentialUnavailableError("登录信息缺少稳定账号标识，请在「账号与登录」重新登录；若仍失败，请检查账号资料接口。")
    available, expired = has_valid_credential()
    if not available or expired:
        raise CredentialUnavailableError("登录凭证缺失或已过期，请先在「账号与登录」更新登录凭证。")
    account = account_id()
    if account == "unbound":
        raise CredentialUnavailableError("未取得稳定账号标识，请重新登录后再添加或执行任务。")
    return account
