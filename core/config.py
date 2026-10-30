"""
统一配置与日志工具。

运行时配置主要从 .env 读取，本文件负责集中定义默认值、路径和日志行为。
"""

import ctypes
import logging
import math
import os
import random
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from dotenv import dotenv_values
from core.diagnostics import redact_text

# 不把 .env 注入 os.environ；AI 每轮读取最新文件，显式环境变量始终优先。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_FILE_SETTINGS = dotenv_values(PROJECT_ROOT / ".env", encoding="utf-8-sig")


def _env_raw(name: str):
    return os.environ.get(name, _FILE_SETTINGS.get(name))

# 运行时数据按用途分目录，避免凭证、队列和日志混在 data/ 根目录。
DATA_DIR = Path(_env_raw("COURSE_AFK_DATA_DIR") or PROJECT_ROOT / "data").expanduser().resolve()
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


class _RedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact_text(super().format(record))


class _SanitizedConsoleFormatter(_RedactingFormatter):
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
    from core.runtime import run_operation
    return run_operation(awaitable, exception_handler_factory=_make_asyncio_exception_handler)


def interrupt_running_async() -> bool:
    """请求停止当前操作；重复请求不打断清理。"""
    from core.runtime import interrupt_running_async as interrupt
    return interrupt()


def _env_flag(name: str, default: bool = False) -> bool:
    value = _env_raw(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_text(name: str, default: str | None = None) -> str | None:
    value = _env_raw(name)
    if value is None:
        return default
    stripped = value.strip()
    return stripped or default


def _env_int(name: str, default: int) -> int:
    raw = _env_text(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logging.warning(f"{name} 不是整数，回退默认值 {default}: {raw!r}")
        return default


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
    handler.setFormatter(_RedactingFormatter(LOG_FORMAT))
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
def is_ai_configured() -> bool:
    """是否已填写 AI 考试所需配置。

    .env 未填 AI 信息时挂课、资料收集等非 AI 功能仍可用；只有 AI 自动考试
    需要这三项（接口地址 / API Key / 模型名）。
    """
    from core.exam.settings import ai_is_configured
    return ai_is_configured()


# ============================================================
# 浏览器配置
# ============================================================
BROWSER_TYPE = (_env_text("BROWSER_TYPE", "chromium") or "chromium").lower()
_BROWSER_CHANNEL_RAW = _env_raw("BROWSER_CHANNEL")
BROWSER_CHANNEL = _default_browser_channel(BROWSER_TYPE) if _BROWSER_CHANNEL_RAW is None else str(_BROWSER_CHANNEL_RAW).strip() or None
# 关闭 Chromium「本地网络访问 / 私有网络访问(PNA)」拦截：知学云/天翼登录会探测本机服务
# (localhost), 触发 Edge「kc.zhixueyun.com 想要访问此设备上的其他应用和服务」授权弹窗,
# 该弹窗会阻塞页面加载、导致自动化超时卡死。Playwright 的 grant_permissions
# ("local-network-access") 目前压不住该提示(见 playwright#37861), 改用启动参数直接禁用。
#
# 注意 --disable-features 只能出现一次：Chromium 的 CommandLine 用 map 存开关，
# 重名后到者覆盖前者。Playwright 自己会先塞一份 --disable-features=...（含
# msForceBrowserSignIn / Translate / HttpsUpgrades / PaintHolding 等），用户 args
# 追加在其后，直接再写一个就会把它整份顶掉——Edge 强制登录、翻译弹窗都会回来。
# 所以这里把 Playwright 的默认项一起列出再追加自己的三项。
# 来源: playwright-core 1.64.1 chromiumSwitches.ts 的 disabledFeatures；升级时需核对。
# https://github.com/microsoft/playwright/blob/v1.64.1/packages/playwright-core/src/server/chromium/chromiumSwitches.ts
_PLAYWRIGHT_DISABLED_FEATURES = (
    "AvoidUnnecessaryBeforeUnloadCheckSync",
    "DestroyProfileOnBrowserClose",
    "DialMediaRouteProvider",
    "GlobalMediaControls",
    "HttpsUpgrades",
    "LensOverlay",
    "MediaRouter",
    "PaintHolding",
    "ThirdPartyStoragePartitioning",
    "BlockOriginHeaderModificationOnRedirect",
    "AvoidCorsURLLoaderRestartOnRedirect",
    "Translate",
    "AutoDeElevate",
    "OptimizationHints",
    "NetworkTimeServiceQuerying",
    "AimEnabled",
    "msForceBrowserSignIn",
    "msEdgeUpdateLaunchServicesPreferredVersion",
)

_PROJECT_DISABLED_FEATURES = (
    "LocalNetworkAccessChecks",
    "BlockInsecurePrivateNetworkRequests",
    "BlockInsecurePrivateNetworkRequestsForPermissions",
)

BROWSER_ARGS = [
    "--mute-audio",
    "--disable-blink-features=AutomationControlled",
    "--disable-features="
    + ",".join(_PLAYWRIGHT_DISABLED_FEATURES + _PROJECT_DISABLED_FEATURES),
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
ZHIXUEYUN_EXAM_DETAIL_PREFIX = "https://kc.zhixueyun.com/#/exam/exam/exam-detail/"
ZHIXUEYUN_EXAM_PREFIXES = (ZHIXUEYUN_EXAM_PREFIX, ZHIXUEYUN_EXAM_DETAIL_PREFIX)

# ============================================================
# 超时 / 等待时间（秒）
# ============================================================
# 视频观看结束后的同步确认上限；期间查询服务器，确认完成立即结束。
VIDEO_SYNC_EXTRA_WAIT = 5 * 60  # 5分钟

# 视频学完后「确认进度同步」的轮询 / 日志间隔
VIDEO_SYNC_POLL_INTERVAL = 30  # 秒

# 文档/网页：等待服务端同步的上限；到期保留未确认任务，继续其他章节。
DOCUMENT_WAIT = 60
# 文档挂机期间进度轮询间隔（秒）
DOCUMENT_POLL_INTERVAL = 10

# URL 学习类型等待时间
URL_TYPE_WAIT = 10  # 秒

# 视频挂机：把整段等待切成随机长度的小段（秒），每段结束核对播放位置与章节进度
VIDEO_WATCH_SLICE_MIN = _env_int("VIDEO_WATCH_SLICE_MIN", 20)
VIDEO_WATCH_SLICE_MAX = _env_int("VIDEO_WATCH_SLICE_MAX", 55)

# 计划观看时长之外的随机余量（秒）。剩余时长被向上取整到整分钟，不加余量
# 则每节都恰好在整分钟离开，是最显眼的固定节拍。
VIDEO_WATCH_OVERSHOOT_MIN = _env_int("VIDEO_WATCH_OVERSHOOT_MIN", 5)
VIDEO_WATCH_OVERSHOOT_MAX = _env_int("VIDEO_WATCH_OVERSHOOT_MAX", 45)

# 播放停滞（缓冲 / 被弹窗暂停）时累计最多顺延多少秒，避免无限等待
VIDEO_STALL_MAX_EXTRA_WAIT = _env_int("VIDEO_STALL_MAX_EXTRA_WAIT", 300)

# 挂课流程的 slow_mo 区间（毫秒）。
# Playwright 在每个动作前固定等待 slow_mo，写死 3000 会让整轮动作间隔完全
# 一致；这里改成每次启动浏览器在区间内取样。注意 slow_mo 只能做到「每次
# 运行不同」，同一轮内仍是定值——逐次动作/轮询的抖动由 core.humanize 在
# 各等待点补齐。
AFK_SLOW_MO_MIN = _env_int("AFK_SLOW_MO_MIN", 1500)
AFK_SLOW_MO_MAX = _env_int("AFK_SLOW_MO_MAX", 4500)


def sample_afk_slow_mo() -> int:
    """每次启动浏览器取一个 slow_mo（毫秒）。"""
    low, high = sorted((max(0, AFK_SLOW_MO_MIN), max(0, AFK_SLOW_MO_MAX)))
    return random.randint(low, high)

# 章节之间的随机停顿（秒）：一节结束立刻点开下一节是很显眼的机器节奏
SECTION_GAP_MIN = _env_int("SECTION_GAP_MIN", 2)
SECTION_GAP_MAX = _env_int("SECTION_GAP_MAX", 7)

# 课程之间的随机停顿（秒）：整批链接无缝连做同理
COURSE_GAP_MIN = _env_int("COURSE_GAP_MIN", 8)
COURSE_GAP_MAX = _env_int("COURSE_GAP_MAX", 40)

# ============================================================
# 考试配置
# ============================================================
# 每题之间不额外加停顿：get_ai_answers 的模型思考耗时本身就是随机的，
# 已经把答题时间线抖开了。

# 多选题逐个点选之间的随机停顿（秒）——同一题内的连点，模型耗时盖不到
def _env_nonnegative_float(name: str, default: float) -> float:
    try:
        value = float(_env_text(name, str(default)))
        if math.isfinite(value) and 0 <= value <= 3600:
            return value
    except (TypeError, ValueError):
        pass
    logging.warning("%s 配置无效，使用默认值 %s", name, default)
    return default


EXAM_OPTION_GAP_MIN = _env_nonnegative_float("EXAM_OPTION_GAP_MIN", 0.25)
EXAM_OPTION_GAP_MAX = _env_nonnegative_float("EXAM_OPTION_GAP_MAX", 0.9)

# 交卷两步确认之间的随机停顿（秒）
EXAM_SUBMIT_GAP_MIN = _env_nonnegative_float("EXAM_SUBMIT_GAP_MIN", 0.8)
EXAM_SUBMIT_GAP_MAX = _env_nonnegative_float("EXAM_SUBMIT_GAP_MAX", 2.5)

# 课程内考试: 剩余次数 <= 此值时禁止自动交卷（1 即“小于 2 次”）
COURSE_EXAM_ATTEMPT_THRESHOLD = 1
# 试卷链接考试: 剩余次数 <= 此值时禁止自动交卷（1 即“小于 2 次”）
PAPER_EXAM_ATTEMPT_THRESHOLD = 1

# ============================================================
# 自动登录配置
# ============================================================
# 自动登录天数选项的 data-time 值 ("3" 对应30天)
AUTO_LOGIN_DATA_TIME = "3"

# 登录凭证逻辑有效期（天）
CREDENTIAL_VALID_DAYS = 28
