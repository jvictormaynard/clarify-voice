@echo off
setlocal
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build.ps1"
if errorlevel 1 (
    echo.
    echo ClarifyVoice build failed.
    pause
    exit /b 1
)

echo.
echo Build complete: %~dp0dist\ClarifyVoice.exe
pause
