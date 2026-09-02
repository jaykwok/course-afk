@echo off
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul
title Course Automation
color 0B
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cls

set "PAD=                "
echo.
REM 启动横幅固定左对齐：此前为居中 spawn PowerShell 取终端宽度，
REM 冷启动要多花 0.5~2 秒，纯装饰不值得。
echo   +------------------------------------------------------------+
echo   ^|                     Course Automation                      ^|
echo   ^|                    Unified Entry Point                     ^|
echo   ^|                                                            ^|
echo   ^|                  Starting launcher.py ...                  ^|
echo   +------------------------------------------------------------+
echo.

set "PYTHON_EXE=.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    REM 实测 PATH 里的 python 是否真正可执行（排除 Windows Store 重定向桩，它会静默退出 49）
    python -c "import sys; sys.exit(0)" >nul 2>nul
    if errorlevel 1 (
        color 0C
        echo %PAD%Python was not found or is the Windows Store stub.
        echo %PAD%Create .venv with: uv venv ^&^& uv pip install -r requirements.txt
        echo %PAD%Or install real Python from https://python.org and add it to PATH.
        echo.
        pause
        exit /b 1
    )
    set "PYTHON_EXE=python"
)

"%PYTHON_EXE%" "launcher.py"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    color 0C
    echo.
    echo %PAD%Launcher exited with code %EXIT_CODE%.
    echo %PAD%Check data\logs\app-error.log for details.
    echo.
    pause
)
exit /b %EXIT_CODE%
