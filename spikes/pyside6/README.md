# PySide6 decision spike

This is an isolated, fake-data prototype for [issue #24](https://github.com/jvictormaynard/clarify-voice/issues/24). It is not imported by `app.py`, is not included in the production requirements, and is not called by the normal startup or build scripts.

## Run the prototype on Windows

From the repository root, install only the optional spike dependency into a disposable environment:

```powershell
py -m venv spikes\pyside6\.venv
spikes\pyside6\.venv\Scripts\python.exe -m pip install -r spikes\pyside6\requirements.txt
spikes\pyside6\.venv\Scripts\python.exe -m spikes.pyside6.app
```

The window exercises idle, recording, processing, success, result, and settings surfaces. All text and transitions are fake. The prototype does not capture audio, call a provider, touch the clipboard, register a global hotkey, or read user configuration.

## Comparable builds and measurements

Build both one-file, windowed artifacts into the spike directory only:

```powershell
powershell -ExecutionPolicy Bypass -File spikes\pyside6\package.ps1
powershell -ExecutionPolicy Bypass -File spikes\pyside6\benchmark.ps1 `
  -CustomTkinterExecutable spikes\pyside6\artifacts\customtkinter\ClarifyVoice-customtkinter.exe `
  -PySide6Executable spikes\pyside6\artifacts\pyside6\ClarifyVoice-pyside6.exe
```

`benchmark.ps1` reports cold start, working set/private memory, process and thread counts, and package size. Run both targets on the same Windows machine, after the same reboot state, and attach the resulting CSV plus screenshots/video to the decision record. Use 100%, 125%, and 150% display scaling where available.

## Manual behavior matrix

Record pass/fail notes for always-on-top, no-activate behavior, transparency, rounded corners, dragging, tray show/quit, DPI scaling, keyboard navigation/accessibility, animation smoothness, and coexistence with ClarifyVoice's production global hotkeys. The spike intentionally does not claim hotkey compatibility until that manual check is performed.
