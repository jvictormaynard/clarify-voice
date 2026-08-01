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

- provider workflows and UI integration. Configuration and local statistics are
  accessed through the repository boundary in `repositories.py` (the legacy
  `APP_CONFIG` mapping remains as a compatibility adapter while extraction is
  staged);
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

Repository behavior is covered separately by configuration, migration, and
statistics tests. Every repository write is JSON-encoded to a same-directory
temporary file, flushed, and atomically replaced. Unknown fields and malformed
values are ignored so a damaged or newer settings file cannot prevent startup.

### `repositories.py`

`ConfigRepository` and `UsageStatsRepository` are the application-facing
interfaces. `LocalConfigRepository` loads the existing flat settings format,
adds `schema_version`, and applies ordered idempotent migrations. Its typed
`AppConfig` model groups provider endpoints/model IDs, provider selection, UI
preferences, and startup settings. `LocalUsageStatsRepository` preserves the
anonymous event list and its existing `version` marker while also writing an
explicit `schema_version`. A file from a newer schema is loaded read-only for
compatibility; saves are refused until a version that understands that schema
is running, so a downgrade cannot delete newer fields.

## Data ownership

| Data | Location | Content |
| --- | --- | --- |
| Settings | `%APPDATA%\ClarifyVoice\config.json` | Provider keys, endpoints, models, and UI preferences |
| Usage stats | `%APPDATA%\ClarifyVoice\usage_stats.json` | Counts, durations, model identifiers, and estimates; no transcript text |
| Working audio | `%APPDATA%\ClarifyVoice\temp_recording.wav` | Temporary audio used during processing |

On non-Windows source runs, the equivalent data directory is
`~/.clarifyvoice`.

For startup configuration, precedence is persisted settings first, then the
documented environment variables (`*_API_KEY`, `*_BASE_URL`, `*_MODEL`, and
refinement variables), then built-in defaults. Environment variables therefore
provide reliable first-run/headless defaults without overwriting a user's saved
UI choices on later launches.

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
