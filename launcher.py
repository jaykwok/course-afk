"""The local Web application is the only interactive entry point."""
from __future__ import annotations
import sys


def _check_python_version():
    if sys.version_info < (3, 14):
        raise RuntimeError("需要 Python 3.14 或更高版本")


def entrypoint():
    from core.startup import check_dependencies, configure_terminal, report_fatal_error
    try:
        configure_terminal()
        _check_python_version()
        if sys.argv[1:] not in ([], ["--check-startup"]):
            raise ValueError("仅支持直接启动 Web 管理页，或 --check-startup 检查依赖")
        check_dependencies()
        if "--check-startup" in sys.argv:
            print("启动依赖检查通过")
            return 0
        from core.app.runtime import launch_web
        return launch_web()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        report_fatal_error(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(entrypoint())
