class UserAbortRequested(Exception):
    """用户主动终止当前流程，调用方应按正常退出处理。"""

    def __init__(self, message: str = "", *, save_pending_urls: bool = True):
        super().__init__(message)
        self.save_pending_urls = save_pending_urls


class UserCancelRequested(Exception):
    """用户取消当前菜单操作，应返回主菜单。"""


class NoPermissionError(Exception):
    """资源不可访问（无权限/已删除/已下架等）。

    不应反复重试：从课程链接清理，并写入挂课失败链接注明原因。
    """

    def __init__(
        self,
        message: str = "无权限查看该资源",
        *,
        reason: str = "no_permission",
        reason_text: str | None = None,
    ):
        super().__init__(message)
        self.reason = reason or "no_permission"
        self.reason_text = reason_text or message


class LearningFlowError(Exception):
    """挂课流程可结构化记录的失败（写入失败链接时用 reason / reason_text）。"""

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        reason_text: str | None = None,
        keep_pending: bool = True,
        detail: dict | None = None,
    ):
        super().__init__(message)
        self.reason = reason
        self.reason_text = reason_text or message
        self.keep_pending = keep_pending
        self.detail = detail or {}


class SyncTimeoutError(LearningFlowError):
    """学完后进度长时间未同步到服务端。"""

    def __init__(
        self,
        message: str = "课程进度未能在等待时间内同步完成",
        *,
        reason_text: str | None = None,
    ):
        super().__init__(
            message,
            reason="sync_timeout",
            reason_text=reason_text
            or "课程进度同步超时，后续可重新加入课程链接",
            keep_pending=True,
        )


class PartialCourseFailure(LearningFlowError):
    """课程或部分主题小节学习失败。"""

    def __init__(
        self,
        message: str = "部分章节学习失败",
        *,
        reason: str = "partial_course_failure",
        reason_text: str | None = None,
        detail: dict | None = None,
    ):
        super().__init__(
            message,
            reason=reason,
            reason_text=reason_text
            or "部分章节/主题课程学习失败，后续可重新加入课程链接",
            keep_pending=True,
            detail=detail,
        )


class SectionActivationError(LearningFlowError):
    """点击章节后，目标章节没有进入当前 DOM 的 focus 状态。"""

    def __init__(
        self,
        message: str = "目标章节切换未生效",
        *,
        reason_text: str | None = None,
        detail: dict | None = None,
    ):
        super().__init__(
            message,
            reason="section_activation_failed",
            reason_text=reason_text
            or "目标章节点击后未激活，已保留链接供后续重试",
            keep_pending=True,
            detail=detail,
        )


class VideoPlayerNotReadyError(LearningFlowError):
    """目标视频章节已选中，但播放器未在等待窗内就绪。"""

    def __init__(
        self,
        message: str = "视频播放器未就绪",
        *,
        reason_text: str | None = None,
        detail: dict | None = None,
    ):
        super().__init__(
            message,
            reason="video_player_not_ready",
            reason_text=reason_text
            or "目标视频播放器未就绪，已保存播放器状态供后续诊断",
            keep_pending=True,
            detail=detail,
        )


class ConcurrentStudyLimitError(LearningFlowError):
    """平台限制同时打开过多课程详情页。"""

    def __init__(
        self,
        message: str = "平台并发学习限流（整页提示，非弹窗）: 已打开新的课程",
        *,
        reason_text: str | None = None,
    ):
        super().__init__(
            message,
            reason="concurrent_study_limit",
            reason_text=reason_text
            or "平台并发学习限流，已保留链接待下轮再试",
            keep_pending=True,
        )


class WafBlockError(LearningFlowError):
    """平台网站安全防护临时拦截；应停止本批次，避免连续撞风控。"""

    def __init__(
        self,
        message: str = "平台网站安全防护临时拦截",
        *,
        reason_text: str | None = None,
    ):
        super().__init__(
            message,
            reason="waf_blocked",
            reason_text=reason_text
            or "平台网站安全防护临时拦截，已保留剩余链接；请约 30 分钟后重试",
            keep_pending=True,
        )
