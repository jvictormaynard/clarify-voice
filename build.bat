@echo off
cd /d "%~dp0"
echo Installing dependencies...
pip install -q keyboard requests sounddevice numpy customtkinter Pillow pyinstaller

echo.
echo Building ClarifyVoice.exe...
pyinstaller --noconfirm --onefile --windowed --name ClarifyVoice --icon=NONE --add-data "extra;extra" --add-data ".env;." --hidden-import=sounddevice --hidden-import=_sounddevice_data app.py

echo.
if exist "dist\ClarifyVoice.exe" (
    echo Build successful!
    echo.
    echo Your exe is at: dist\ClarifyVoice.exe
    echo Copy it anywhere along with the "extra" folder and ".env" file.
) else (
    echo Build failed. See errors above.
)
pause
