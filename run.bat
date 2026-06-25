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
echo %PAD%Course Automation
echo %PAD%Unified Entry Point
echo %PAD%Starting launcher.py ...
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
