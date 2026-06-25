@echo off
REM ====================================================================
REM  打包为 Windows exe（onedir）。需要先安装 PyInstaller：
REM    uv pip install pyinstaller
REM  产物：dist\course-afk\course-afk.exe（连同 _internal\ 依赖目录）
REM  使用：把 .env / cookies.json 放到 dist\course-afk\ 下（与 exe 同级），
REM        双击 course-afk.exe 即可。浏览器默认用系统 Edge。
REM ====================================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "PYTHON_EXE=.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Python was not found. Create .venv or install Python.
        exit /b 1
    )
    set "PYTHON_EXE=python"
)

"%PYTHON_EXE%" -m PyInstaller --noconfirm --clean --onedir --name course-afk ^
    --collect-all textual ^
    --collect-all playwright ^
    --collect-submodules core ^
    launcher.py

if errorlevel 1 (
    echo.
    echo Build failed.
    exit /b 1
)

echo.
echo Build complete: dist\course-afk\course-afk.exe

REM 附上 .env 模板和使用说明，方便用户上手
copy /Y ".env.example" "dist\course-afk\.env.example" >nul
copy /Y "使用说明.txt" "dist\course-afk\使用说明.txt" >nul
exit /b 0
