"""
统一配置与日志工具。

运行时配置主要从 .env 读取，本文件负责集中定义默认值、路径和日志行为。
"""

import asyncio
import ctypes
import logging
import os
import sys
import threading
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from urllib.parse import urlparse

# 加载 .env 文件（API密钥等敏感信息仍由 .env 管理）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# 运行时数据按用途分目录，避免凭证、队列和日志混在 data/ 根目录。
DATA_DIR = PROJECT_ROOT / "data"
CREDENTIALS_DIR = DATA_DIR / "credentials"
LINKS_DIR = DATA_DIR / "links"
LOGS_DIR = DATA_DIR / "logs"
REFERENCE_OUTPUT_DIR = DATA_DIR / "references"

COOKIES_FILE = CREDENTIALS_DIR / "cookies.json"
CREDENTIAL_META_FILE = CREDENTIALS_DIR / "credential_meta.json"
LEARNING_URLS_FILE = LINKS_DIR / "课程链接.json"
LEARNING_FAILURES_FILE = LINKS_DIR / "挂课失败链接.json"
EXAM_URLS_FILE = LINKS_DIR / "考试链接.json"
MANUAL_EXAM_FILE = LINKS_DIR / "人工考试链接.json"

INFO_LOG_FILE = LOGS_DIR / "app-info.log"
WARN_LOG_FILE = LOGS_DIR / "app-warn.log"
ERROR_LOG_FILE = LOGS_DIR / "app-error.log"

# ============================================================
# 日志配置
# ============================================================
LOG_LEVEL = logging.DEBUG
CONSOLE_LOG_LEVEL = logging.INFO
LOG_FORMAT = (
    "%(asctime)s [%(levelname)s] %(filename)s:%(lineno)d (%(funcName)s) %(message)s"
)
CONSOLE_LOG_FORMAT = "%(message)s"
_LOGGING_CONFIGURED = False
_NOISY_LOGGER_NAMES = (
    "asyncio",
    "openai",
    "httpx",
    "httpcore",
    "playwright",
    "urllib3",
    "websockets",
)


def _sanitize_console_message(message: str) -> str:
    if not message:
        return message

    normalized = message.replace("\r\n", "\n").replace("\r", "\n")
    if normalized.lstrip().startswith("Traceback (most recent call last):"):
        return ""

    sanitized_lines: list[str] = []
    skipping_call_log = False

    for line in normalized.split("\n"):
        stripped = line.strip()

        if stripped.startswith("Call log:"):
            skipping_call_log = True
            continue

        if skipping_call_log:
            if not stripped:
                continue
            if line.lstrip().startswith("- "):
                continue
            skipping_call_log = False

        sanitized_lines.append(line)

    collapsed_lines: list[str] = []
    previous_blank = False
    for line in sanitized_lines:
        is_blank = not line.strip()
        if is_blank and previous_blank:
            continue
        collapsed_lines.append(line)
        previous_blank = is_blank

    return "\n".join(collapsed_lines).strip("\n")


class _SanitizedConsoleFormatter(logging.Formatter):
    def format(self, record):
        return _sanitize_console_message(super().format(record))


class _SanitizedConsoleFilter(logging.Filter):
    def filter(self, record):
        return bool(_sanitize_console_message(record.getMessage()).strip())


def summarize_exception_message(exc: Exception, fallback: str) -> str:
    sanitized = _sanitize_console_message(str(exc)).strip()
    if not sanitized:
        return fallback

    lines = [line.strip() for line in sanitized.splitlines() if line.strip()]
    if not lines:
        return fallback

    first_line = lines[0]
    noisy_prefixes = (
        "Locator.",
        "Traceback ",
        "playwright.",
    )
    if first_line.startswith(noisy_prefixes):
        return fallback
    return f"{fallback}: {first_line}"


def _is_unretrieved_target_closed_context(context: dict) -> bool:
    message = str(context.get("message", ""))
    if "Future exception was never retrieved" not in message:
        return False

    exc = context.get("exception")
    if exc is None:
        return False

    exc_text = str(exc).lower()
    return exc.__class__.__name__ == "TargetClosedError" or any(
        marker in exc_text
        for marker in (
            "target page, context or browser has been closed",
            "browser has been closed",
        )
    )


def _make_asyncio_exception_handler(previous_handler=None):
    def _handle_asyncio_exception(loop, context):
        if _is_unretrieved_target_closed_context(context):
            return
        if previous_handler is not None:
            previous_handler(loop, context)
            return
        loop.default_exception_handler(context)

    return _handle_asyncio_exception


def run_async(awaitable):
    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(_make_asyncio_exception_handler(previous_handler))
        thread_id = threading.get_ident()

        async def _tracked():
            # 把当前任务登记起来，便于主线程通过 interrupt_running_async 跨线程取消
            # （TUI 里 Ctrl+C 需要打断工作线程上阻塞的 Playwright 循环并触发优雅保存）。
            with _RUNNING_ASYNC_LOCK:
                _RUNNING_ASYNC[thread_id] = (loop, asyncio.current_task())
            try:
                return await awaitable
            finally:
                with _RUNNING_ASYNC_LOCK:
                    _RUNNING_ASYNC.pop(thread_id, None)

        try:
            return runner.run(_tracked())
        finally:
            with _RUNNING_ASYNC_LOCK:
                _RUNNING_ASYNC.pop(thread_id, None)
            close_awaitable = getattr(awaitable, "close", None)
            if callable(close_awaitable):
                close_awaitable()


# 线程 id -> (事件循环, 正在运行的任务)；run_async 登记，interrupt_running_async 读取
_RUNNING_ASYNC: dict[int, tuple] = {}
_RUNNING_ASYNC_LOCK = threading.Lock()


def interrupt_running_async() -> bool:
    """从主线程取消工作线程上正在运行的 run_async 任务。

    TUI 的 Ctrl+C 绑定调用本函数：若挂课/考试流程正在工作线程上阻塞，
    取消该任务会抛 CancelledError，由对应 workflow 的异常处理触发优雅保存。
    没有正在运行的任务时返回 False（调用方可直接退出应用）。
    """
    current = threading.get_ident()
    with _RUNNING_ASYNC_LOCK:
        candidates = [
            (tid, pair)
            for tid, pair in _RUNNING_ASYNC.items()
            if tid != current
        ]
    if not candidates:
        return False
    _tid, (loop, task) = candidates[0]
    loop.call_soon_threadsafe(task.cancel)
    return True


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_text(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None:
        return default
    stripped = value.strip()
    return stripped or default


def _default_browser_channel(browser_type: str) -> str | None:
    if browser_type == "chromium" and sys.platform.startswith("win"):
        return "msedge"
    return None


class _LevelRangeFilter(logging.Filter):
    def __init__(self, *, minimum: int, maximum: int | None = None):
        super().__init__()
        self.minimum = minimum
        self.maximum = maximum

    def filter(self, record):
        return record.levelno >= self.minimum and (
            self.maximum is None or record.levelno <= self.maximum
        )


def _dated_log_namer(default_name: str) -> str:
    """将 ``app-info.log.2026-07-29`` 改成用户可读的归档名。"""
    path = Path(default_name)
    marker = ".log."
    if marker not in path.name:
        return default_name
    base_name, date_suffix = path.name.split(marker, 1)
    return str(path.with_name(f"{base_name}-{date_suffix}.log"))


def _build_file_handler(
    path: Path,
    *,
    minimum: int,
    maximum: int | None = None,
):
    handler = TimedRotatingFileHandler(
        path,
        when="midnight",
        interval=1,
        backupCount=0,
        encoding="utf-8",
    )
    handler.suffix = "%Y-%m-%d"
    handler.namer = _dated_log_namer
    handler.setLevel(minimum)
    handler.addFilter(_LevelRangeFilter(minimum=minimum, maximum=maximum))
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    return handler


def _build_file_handlers() -> list[logging.Handler]:
    # DEBUG 没有单独文件，和 INFO 一并进入 app-info；三个文件互不重复。
    return [
        _build_file_handler(
            INFO_LOG_FILE,
            minimum=logging.DEBUG,
            maximum=logging.INFO,
        ),
        _build_file_handler(
            WARN_LOG_FILE,
            minimum=logging.WARNING,
            maximum=logging.WARNING,
        ),
        _build_file_handler(
            ERROR_LOG_FILE,
            minimum=logging.ERROR,
        ),
    ]


def _get_console_log_level() -> int:
    return logging.DEBUG if _env_flag("DEBUG_MODE") else CONSOLE_LOG_LEVEL


def _is_utf8_console_encoding(encoding: str | None) -> bool:
    if not encoding:
        return False
    normalized = encoding.strip().lower().replace("_", "-")
    return normalized in {"utf-8", "utf8", "cp65001"}


def _prepare_console_streams() -> None:
    _disable_windows_console_input_modes()
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                try:
                    reconfigure(errors="replace")
                except Exception:
                    pass


def _can_use_rich_console() -> bool:
    return _is_utf8_console_encoding(getattr(sys.stdout, "encoding", None)) and (
        _is_utf8_console_encoding(getattr(sys.stderr, "encoding", None))
    )


def _disable_windows_console_input_modes() -> None:
    if not sys.platform.startswith("win"):
        return

    try:
        kernel32 = ctypes.windll.kernel32
        stdin_handle = kernel32.GetStdHandle(-10)
        if stdin_handle in (0, -1):
            return

        mode = ctypes.c_uint()
        if not kernel32.GetConsoleMode(stdin_handle, ctypes.byref(mode)):
            return

        extended_flags = 0x0080
        quick_edit_mode = 0x0040
        insert_mode = 0x0020
        updated_mode = (mode.value | extended_flags) & ~(quick_edit_mode | insert_mode)
        if updated_mode != mode.value:
            kernel32.SetConsoleMode(stdin_handle, updated_mode)
    except Exception:
        pass


def _build_console_handler():
    _prepare_console_streams()

    handler = None
    if _can_use_rich_console():
        try:
            from rich.logging import RichHandler

            handler = RichHandler(
                rich_tracebacks=True,
                show_path=False,
                show_time=False,
                show_level=False,
                markup=True,
            )
        except Exception:
            handler = None

    if handler is None:
        handler = logging.StreamHandler()
    handler.setLevel(_get_console_log_level())
    handler.addFilter(_SanitizedConsoleFilter())
    handler.setFormatter(_SanitizedConsoleFormatter(CONSOLE_LOG_FORMAT))
    return handler


def _silence_noisy_loggers():
    for logger_name in _NOISY_LOGGER_NAMES:
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def _should_show_startup_banner(show_startup_banner: bool | None) -> bool:
    if show_startup_banner is not None:
        return show_startup_banner
    return not _env_flag("SUPPRESS_STARTUP_BANNER")


def _log_startup_banner(root_logger):
    script_name = sys.argv[0] if sys.argv[0] else "unknown"
    separator = (
        f"\n{'='*60}\n"
        f"[启动] {script_name} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"{'='*60}"
    )
    root_logger.info(separator)


def ensure_data_layout() -> None:
    """只创建当前版本的分类目录；不读取或迁移旧版平铺路径。"""
    for directory in (
        DATA_DIR,
        CREDENTIALS_DIR,
        LINKS_DIR,
        LOGS_DIR,
        REFERENCE_OUTPUT_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def setup_logging(show_startup_banner: bool | None = None):
    """统一日志配置，所有脚本共用，追加模式保留历史日志"""
    global _LOGGING_CONFIGURED

    if _LOGGING_CONFIGURED:
        return logging.getLogger()

    ensure_data_layout()

    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    root_logger.setLevel(LOG_LEVEL)
    for handler in _build_file_handlers():
        root_logger.addHandler(handler)
    root_logger.addHandler(_build_console_handler())
    _silence_noisy_loggers()
    _LOGGING_CONFIGURED = True

    if _should_show_startup_banner(show_startup_banner):
        _log_startup_banner(root_logger)
    return root_logger


# ============================================================
# OpenAI 兼容 AI 模型配置（从 .env 读取）
# ============================================================
OPENAI_COMPLETION_BASE_URL = os.getenv("OPENAI_COMPLETION_BASE_URL")
OPENAI_COMPLETION_API_KEY = os.getenv("OPENAI_COMPLETION_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME")
AI_REQUEST_TYPE = (_env_text("AI_REQUEST_TYPE", "responses") or "responses").lower()
AI_ENABLE_WEB_SEARCH = _env_flag("AI_ENABLE_WEB_SEARCH", False)
AI_ENABLE_THINKING = _env_flag("AI_ENABLE_THINKING", False)
AI_REASONING_EFFORT = _env_text("AI_REASONING_EFFORT")
if AI_REASONING_EFFORT:
    AI_REASONING_EFFORT = AI_REASONING_EFFORT.lower()
AI_RESPONSE_TOOLS = [{"type": "web_search"}] if AI_ENABLE_WEB_SEARCH else None

# AI 请求超时与重试（应对接口不稳/网络抖动；单位：秒 / 次）
AI_REQUEST_TIMEOUT = float(_env_text("AI_REQUEST_TIMEOUT", "60") or "60")
AI_MAX_RETRIES = int(_env_text("AI_MAX_RETRIES", "2") or "2")

# AI 考试参数
AI_TEMPERATURE = 0
AI_SYSTEM_PROMPT = (
    "你是一个专业的考试助手, 请根据题目选择最合适的答案。"
    "如果关键信息不足且已提供联网搜索工具, 可以先搜索再作答。"
    "最终只输出答案内容, 不要解释。"
)


def is_ai_configured() -> bool:
    """是否已填写 AI 考试所需配置。

    .env 未填 AI 信息时挂课、人工考试等非 AI 功能仍可用；只有 AI 自动考试
    需要这三项（接口地址 / API Key / 模型名）。
    """
    return bool(OPENAI_COMPLETION_BASE_URL and OPENAI_COMPLETION_API_KEY and MODEL_NAME)


def validate_ai_base_url(url: str | None) -> str | None:
    """校验 AI 接口地址，防止误填内网/非法地址导致 API Key 外泄。

    仅 http/https 且带 hostname 的地址才放行；返回去掉末尾斜杠的地址。
    """
    if not url:
        return url
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(
            f"OPENAI_COMPLETION_BASE_URL 非法（需以 http:// 或 https:// 开头）: {url!r}"
        )
    return url.strip().rstrip("/")

# ============================================================
# 浏览器配置
# ============================================================
BROWSER_TYPE = (_env_text("BROWSER_TYPE", "chromium") or "chromium").lower()
BROWSER_CHANNEL = _env_text("BROWSER_CHANNEL", _default_browser_channel(BROWSER_TYPE))
# 关闭 Chromium「本地网络访问 / 私有网络访问(PNA)」拦截：知学云/天翼登录会探测本机服务
# (localhost), 触发 Edge「kc.zhixueyun.com 想要访问此设备上的其他应用和服务」授权弹窗,
# 该弹窗会阻塞页面加载、导致自动化超时卡死。Playwright 的 grant_permissions
# ("local-network-access") 目前压不住该提示(见 playwright#37861), 改用启动参数直接禁用。
BROWSER_ARGS = [
    "--mute-audio",
    "--disable-blink-features=AutomationControlled",
    "--disable-features="
    "LocalNetworkAccessChecks,"
    "BlockInsecurePrivateNetworkRequests,"
    "BlockInsecurePrivateNetworkRequestsForPermissions",
]

# ============================================================
# 平台 URL
# ============================================================
MYLEARNING_HOME = "https://www.mylearning.cn/p5/index.html"
MYLEARNING_SSO_PATTERN = "**/sso/login**"
MYLEARNING_CENTER_HOME = "https://center.mylearning.cn/PC/home"
MYLEARNING_CENTER_HOME_PATTERN = r"https://center\.mylearning\.cn/PC/home(?:\?.*)?$"
ZHIXUEYUN_COURSE_PREFIX = "https://kc.zhixueyun.com/#/study/course/detail/"
ZHIXUEYUN_SUBJECT_PREFIX = "https://kc.zhixueyun.com/#/study/subject/detail/"
ZHIXUEYUN_TRAIN_CLASS_PREFIX = "https://kc.zhixueyun.com/#/train-new/class-detail/"
ZHIXUEYUN_EXAM_PREFIX = "https://kc.zhixueyun.com/#/exam/exam/answer-paper/"

# ============================================================
# 超时 / 等待时间（秒）
# ============================================================
# 视频课程服务端记录学习点的周期，也是播放结束后的额外等待上限
VIDEO_SYNC_EXTRA_WAIT = 5 * 60  # 5分钟

# 学完后同步确认最短等待窗（确认已同步会提前返回，故下限可接受）
VIDEO_SYNC_MIN_WAIT = 30  # 秒

# 视频学完后「确认进度同步」的轮询 / 日志间隔
VIDEO_SYNC_POLL_INTERVAL = 30  # 秒

# 文档/网页：统一挂机上限（秒）。提前同步则提前离开；到点直接走人，不另开同步确认窗、不因未同步判失败。
DOCUMENT_WAIT = 60
# 文档挂机期间进度轮询间隔（秒）
DOCUMENT_POLL_INTERVAL = 10

# URL 学习类型等待时间
URL_TYPE_WAIT = 10  # 秒

# 挂课流程的 slow_mo 参数
AFK_SLOW_MO = 3000  # 毫秒

# ============================================================
# 考试配置
# ============================================================
# 课程内考试: 剩余次数 <= 此值时转为人工考试（1 即“小于 2 次”）
COURSE_EXAM_ATTEMPT_THRESHOLD = 1
# 试卷链接考试: 剩余次数 <= 此值时转为人工考试（1 即“小于 2 次”）
PAPER_EXAM_ATTEMPT_THRESHOLD = 1

# ============================================================
# 自动登录配置
# ============================================================
# 自动登录天数选项的 data-time 值 ("3" 对应30天)
AUTO_LOGIN_DATA_TIME = "3"

# 登录凭证逻辑有效期（天）
CREDENTIAL_VALID_DAYS = 28
