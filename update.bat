@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0"

echo [Miliastra-Tools] Checking Python runtime...

REM ============================================================
REM 查找 Python 3.11 Runtime
REM ============================================================

set "PYTHON="

for /d %%D in ("%~dp0runtime\cpython-3.11*-windows-x86_64-none") do (
    if exist "%%D\python.exe" (
        set "PYTHON=%%D\python.exe"
    )
)

if not defined PYTHON (
    echo.
    echo [ERROR] Python 3.11 runtime not found.
    echo Expected under:
    echo %~dp0runtime
    echo.
    pause
    exit /b 1
)

echo.
echo [Miliastra-Tools] Python runtime:
echo %PYTHON%
echo.

REM ============================================================
REM 检查 pip
REM ============================================================

"%PYTHON%" -m pip --version >nul 2>&1

if errorlevel 1 (
    echo [Miliastra-Tools] Installing pip...

    "%PYTHON%" -m ensurepip

    if errorlevel 1 (
        echo.
        echo [ERROR] Failed to install pip.
        echo.
        pause
        exit /b 1
    )
)

REM ============================================================
REM 安装 / 更新依赖
REM
REM runtime 是 uv 管理的 Python，因此存在 EXTERNALLY-MANAGED
REM 这里是程序自己的私有 Runtime，所以明确允许 pip 修改
REM ============================================================

echo [Miliastra-Tools] Updating dependencies...

"%PYTHON%" -m pip install ^
    --break-system-packages ^
    -r "%~dp0requirements.txt" ^
    -i https://mirrors.aliyun.com/pypi/simple

if errorlevel 1 (
    echo.
    echo [ERROR] Failed to install dependencies.
    echo.
    pause
    exit /b 1
)

echo.
echo ================================================
echo   Miliastra-Tools update completed successfully.
echo ================================================
echo.

pause
exit /b 0