@echo off
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul
title Course Automation
color 0B
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
REM cmd(传统控制台)默认窗口偏窄，这里把控制台拉宽，让 TUI 启动即铺满。
REM 注意：cmd 调整窗口大小时 TUI 不会实时重排(传统控制台限制)，想要可随意缩放
REM 请用 Windows Terminal 打开本 bat(右键 → 在终端中打开)。
mode con cols=140 lines=42 >nul 2>&1
cls

set "PAD=                "
echo.
echo %PAD%+------------------------------------------------------------+
echo %PAD%^|                    Course Automation                     ^|
echo %PAD%^|                    Unified Entry Point                    ^|
echo %PAD%^|                                                            ^|
echo %PAD%^|                 Starting launcher.py ...                   ^|
echo %PAD%+------------------------------------------------------------+
echo.

set "PYTHON_EXE=.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    where python >nul 2>nul
    if errorlevel 1 (
        color 0C
        echo %PAD%Python was not found.
        echo %PAD%Create .venv or install Python and add it to PATH.
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
    echo %PAD%Check log.txt for details.
    echo.
    pause
)
exit /b %EXIT_CODE%
