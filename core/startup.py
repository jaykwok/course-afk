"""Dependency checks and terminal error output before third-party imports."""

from importlib.util import find_spec
import os
from pathlib import Path
import re
import sys
import tomllib


def configure_terminal():
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def check_dependencies():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    modules = {"beautifulsoup4": "bs4", "python-dotenv": "dotenv"}
    missing = []
    for requirement in project["project"]["dependencies"]:
        name = re.split(r"[<>=!~;\[]", requirement, maxsplit=1)[0].strip()
        if find_spec(modules.get(name, name.replace("-", "_"))) is None:
            missing.append(name)
    if missing:
        raise RuntimeError("缺少启动依赖：" + ", ".join(missing) + "。请在项目目录运行 uv sync，再运行 uv run launcher.py。")
    for name in ("index.html", "style.css", "app.js"):
        if not (root / "core" / "web" / "static" / name).is_file():
            raise RuntimeError("本地 Web 静态资源不完整，请重新安装完整分发包：" + name)


def report_fatal_error(exc):
    from core.diagnostics import redact_text

    config = sys.modules.get("core.config")
    path = getattr(config, "ERROR_LOG_FILE", None)
    if path is None:
        root = Path(os.environ.get("COURSE_AFK_LOGS_DIR") or Path(__file__).resolve().parents[1] / "logs")
        path = root / "app-error.log"
    path = Path(path).resolve()
    summary = redact_text(f"{type(exc).__name__}: {exc}").replace("\r", " ").split("\n")[0]
    message = f"程序未能继续：{summary}\n错误日志：{path}\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(message)
    except OSError:
        message += "错误日志无法写入，请检查目录权限。\n"
    sys.stderr.write(message)


def shutdown_workflows():
    host = sys.modules.get("core.workflow.host")
    if host is not None:
        host.shutdown_host()
