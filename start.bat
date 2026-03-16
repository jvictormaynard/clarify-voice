@echo off
cd /d "%~dp0"
echo Installing dependencies...
pip install -q keyboard requests sounddevice numpy customtkinter Pillow
echo Starting ClarifyVoice...
python app.py
if errorlevel 1 (
    echo.
    echo Something went wrong. See error above.
    pause
)
