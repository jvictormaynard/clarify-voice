@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Preparing ClarifyVoice for the first run...
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1"
    if errorlevel 1 goto :error
)

".venv\Scripts\python.exe" spikes\pyside6\qml_app.py
if errorlevel 1 goto :error
exit /b 0

:error
echo.
echo ClarifyVoice could not start. Review the message above or open a bug report.
pause
exit /b 1
