# Architecture

ClarifyVoice is a local-first desktop application. It has no ClarifyVoice-owned
backend: the desktop process calls the AI provider selected by the user.

## Runtime flow

```text
Global hotkey
    |
    v
Desktop workflow ----> temporary audio or selected text
    |                              |
    v                              v
Provider client ------------> Gemini / OpenAI / Groq / custom endpoint
    |
    v
Focus and selection safety check
    |
    +---- unchanged ----> paste result into the original application
    |
    +---- changed ------> keep result in clipboard and show result panel
```

All UI updates return to the Tk event loop. Provider calls and audio processing
run outside that loop so the floating window remains responsive.

## Current modules

### `app.py`

The application entry point currently owns:

- configuration and local statistics;
- provider discovery, validation, transcription, rewriting, and translation;
- audio capture and conversion;
- clipboard and focus safety;
- the CustomTkinter interface, tray, pill, and result surfaces;
- the headless transcription commands.

This is intentionally documented as a known concentration of responsibilities.
New contributions should extract cohesive, testable units when doing so makes a
change safer, but broad rewrites of stable UI code should be proposed in an
issue before implementation.

### `desktop_state.py`

Contains the small `WorkflowController` that prevents rewrite and translation
flows from overlapping.

### `windows_hotkeys.py`

Owns native Windows `RegisterHotKey` registration and Ctrl-key synthesis.
Packaged Windows builds exclude the optional cross-platform `keyboard` module.

### `tests/`

The unit suite tests provider routing, URL construction, configuration,
clipboard safety, workflow state, usage statistics, and geometry calculations.
Windows UI acceptance still requires a real installed build because mocked
tests cannot reveal transparency, focus, DPI, or hotkey integration defects.

## Data ownership

| Data | Location | Content |
| --- | --- | --- |
| Settings | `%APPDATA%\ClarifyVoice\config.json` | Provider keys, endpoints, models, and UI preferences |
| Usage stats | `%APPDATA%\ClarifyVoice\usage_stats.json` | Counts, durations, model identifiers, and estimates; no transcript text |
| Working audio | `%APPDATA%\ClarifyVoice\temp_recording.wav` | Temporary audio used during processing |

On non-Windows source runs, the equivalent data directory is
`~/.clarifyvoice`.

## Packaging

PyInstaller creates a one-file Windows executable containing the Python runtime,
application modules, visual assets, and the vendored SoX runtime. `.env` is
never bundled. End-user provider settings are read from the user data directory.

The GitHub release workflow builds on a Windows runner and publishes the
executable plus a SHA-256 checksum. `scripts/deploy.ps1` is a maintainer tool for
updating an existing local installation from WSL; contributors normally use
`scripts/build.ps1`.

## Legacy prototype

`legacy/electron-prototype/` contains the incomplete Electron implementation
that preceded the Python rewrite. It is not installed, tested, packaged, or used
at runtime. Keep changes to it separate from current application changes.
