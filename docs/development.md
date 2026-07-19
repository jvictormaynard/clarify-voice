# Development

## Requirements

- Python 3.11 or newer
- Windows 10 or 11 for full product behavior
- A microphone for end-to-end recording checks
- An API key for live provider checks
- Git, if cloning instead of downloading a source archive

Node.js is not an application dependency. `package.json` only provides optional
short aliases for maintainers working from WSL.

## Windows setup

```powershell
git clone https://github.com/jvictormaynard/clarify-voice.git
cd clarify-voice
.\scripts\setup.ps1 -Dev
.\.venv\Scripts\python.exe app.py
```

The setup script creates `.venv` and installs runtime dependencies. `-Dev` also
installs PyInstaller for local packaging.

Environment variables are optional because provider settings can be entered in
the UI. For local automation, copy `.env.example` to `.env` and fill only the
values you need. Never commit `.env`.

## Checks

Run the same core checks used in CI:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q app.py desktop_state.py windows_hotkeys.py tests
```

From Linux or WSL with the dependencies installed:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q app.py desktop_state.py windows_hotkeys.py tests
```

## Build

```powershell
.\build.bat
```

or:

```powershell
.\scripts\build.ps1
```

The portable executable is written to `dist\ClarifyVoice.exe`. Local `.env`
files are deliberately excluded from every build.

## Windows acceptance checklist

For changes that affect UI, hotkeys, audio, clipboard behavior, system tray, or
packaging, unit tests are necessary but not sufficient. Verify the built
executable on Windows:

1. Launch `dist\ClarifyVoice.exe` and confirm only one logical app instance is
   active. A one-file PyInstaller build may show a launcher and child process.
2. Check the floating bar and menu at 100% and one scaled DPI setting if
   available.
3. Exercise every shortcut touched by the change.
4. Confirm the original application keeps focus through recording or selection.
5. Test the system tray, minimize, restore, and quit paths.
6. Attach before and after screenshots to visual pull requests.
7. Confirm that the executable contains no `.env` or personal key.

## Maintainer deploy from WSL

```bash
npm run deploy
```

This invokes `scripts/deploy.ps1` with Windows PowerShell, stages source files on
the native Windows temporary directory, builds, backs up the installed
executable, replaces it, and restarts ClarifyVoice. Override the discovered
target with `CLARIFYVOICE_INSTALL_PATH` when needed.

## Experimental Linux and macOS support

The source contains fallback clipboard and hotkey paths, but the polished and
packaged product is currently Windows-first. Linux requires SoX, `xclip`,
`xdotool`, Tk, PortAudio, and permissions suitable for global keyboard events.
Wayland global shortcuts and paste automation are not supported. macOS is not
packaged and may require Accessibility permission for simulated paste.

Contributions that improve these platforms are welcome when they preserve the
Windows path and include platform-specific validation.

## Release process

1. Update `CHANGELOG.md`.
2. Ensure CI is green on the intended commit.
3. Create and push a semantic tag, for example `v0.1.0`.
4. The release workflow builds on Windows, creates a ZIP and SHA-256 checksum,
   verifies the official SoX source archive, and publishes all of them to the
   same GitHub release.
5. Download the published asset and perform the Windows acceptance checklist.

The executable is currently unsigned. Code signing should be introduced before
representing the app as a warning-free consumer installer.
