class UserAbortRequested(Exception):
    """用户主动终止当前流程，调用方应按正常退出处理。"""

    def __init__(self, message: str = "", *, save_pending_urls: bool = True):
        super().__init__(message)
        self.save_pending_urls = save_pending_urls


class UserCancelRequested(Exception):
    """用户取消当前菜单操作，应返回主菜单。"""


class NoPermissionError(Exception):
    """当前账号无权限查看该学习资源（不应反复重试，需记录后跳过）。"""
