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
REM 检测控制台实际宽度，让方框在任何窗口尺寸下都居中
setlocal EnableDelayedExpansion
set COLS=120
for /f %%W in ('powershell -NoProfile -Command "$Host.UI.RawUI.WindowSize.Width" 2^>nul') do set COLS=%%W
set /a "PW=(!COLS! - 62) / 2"
if !PW! LSS 0 set PW=0
set BPAD=
for /L %%i in (1,1,!PW!) do call set "BPAD=%%BPAD%% "
echo !BPAD!+------------------------------------------------------------+
echo !BPAD!^|                     Course Automation                      ^|
echo !BPAD!^|                    Unified Entry Point                     ^|
echo !BPAD!^|                                                            ^|
echo !BPAD!^|                  Starting launcher.py ...                  ^|
echo !BPAD!+------------------------------------------------------------+
echo.
endlocal

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
