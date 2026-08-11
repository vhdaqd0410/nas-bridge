@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

set "PYTHON=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo [ERROR] Python venv not found.
    pause
    exit /b 1
)

echo ========================================
echo   NAS Bridge Desktop
echo ========================================
echo.
echo Starting desktop app...

"%PYTHON%" -B desktop.py
pause
