@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0"

echo [Miliastra-Tools] Checking Python runtime...

set "PYTHON="

for /d %%D in ("%~dp0runtime\cpython-3.11.*-windows-x86_64-none") do (
    if exist "%%D\python.exe" (
        set "PYTHON=%%D\python.exe"
    )
)

if not defined PYTHON (
    echo.
    echo [ERROR] Python 3.11 runtime not found.
    echo Please run update.bat first.
    echo.
    pause
    exit /b 1
)

echo Starting Miliastra-Tools...

"%PYTHON%" -m streamlit run "%~dp0streamlit_app.py"

if errorlevel 1 (
    echo.
    echo [ERROR] Miliastra-Tools exited with an error.
    echo.
    pause
    exit /b 1
)

pause