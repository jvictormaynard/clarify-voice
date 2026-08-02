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
Provider registry ----------> Gemini / OpenAI-compatible adapter
    |                              |
    |                              +----> OpenAI / Groq / custom endpoint
    v
Typed capability result
    |
    v
Focus and selection safety check
    |
    +---- unchanged ----> snapshot, atomically paste, and conditionally restore
    |
    +---- changed ------> keep result in clipboard and show result panel
```

All UI updates return to the Tk event loop. Provider calls and audio processing
run outside that loop so the floating window remains responsive.

## Current modules

### `app.py`

The application entry point currently owns:

- desktop workflows and UI integration. Configuration and local statistics are
  accessed through the repository boundary in `repositories.py` (the legacy
  `APP_CONFIG` mapping remains as a compatibility adapter);
- audio capture and conversion;
- clipboard and focus safety, including serialized rich-format snapshots;
- the CustomTkinter interface, tray, pill, and result surfaces;
- the headless transcription commands.

This is intentionally documented as a known concentration of responsibilities.
New contributions should extract cohesive, testable units when doing so makes a
change safer, but broad rewrites of stable UI code should be proposed in an
issue before implementation.

### Provider layer

Provider code is split into three UI-independent modules:

- `provider_types.py` defines capabilities, metadata, connections, typed
  transcription/rewrite/translation requests and results, model catalogs, and
  actionable provider errors;
- `provider_adapters.py` implements Gemini and the shared OpenAI-compatible
  protocol used by OpenAI and Groq. Endpoint normalization and model filtering
  live here so display labels never become API IDs;
- `provider_registry.py` is authoritative for provider IDs, display metadata,
  capabilities, default endpoints/models, adapters, and request routing.

The desktop asks the registry whether a provider supports multimodal audio,
text generation, model discovery, or custom base URLs. It does not select a
workflow by comparing provider names. Compatibility functions in `app.py`
retain the existing public call surface while delegating to typed requests.

The HTTP dependency is intentionally the narrow `HttpClient` protocol with
`get` and `post`. Adapters preserve the existing request timeouts and response
shapes; retry, session, logging, and shared error policy belong to issue #17.

#### Adding an OpenAI-compatible provider

1. Add `ProviderMetadata` with a canonical lowercase API ID, a separate display
   name, config keys/defaults, and the capabilities the endpoint really offers.
2. Call `ProviderRegistry.register_openai_compatible`, supplying official model
   IDs or legacy label aliases only when the provider needs them.
3. Extend persisted configuration fields and provider artwork for the new ID.
4. Add fake-HTTP contract tests for discovery, authentication, transcription,
   text generation, custom URL normalization, and unsupported capabilities.

Dictation, rewrite, translation, and model-picker workflows require no new
provider-specific branch. A protocol that is not OpenAI-compatible should use a
new adapter implementing the same typed operations, then be registered once.

### `desktop_state.py`

Contains the small `WorkflowController` that prevents rewrite and translation
flows from overlapping.

### `windows_hotkeys.py`

Owns native Windows `RegisterHotKey` registration and Ctrl-key synthesis.
Packaged Windows builds exclude the optional cross-platform `keyboard` module.

### `windows_clipboard.py`

Provides the Windows-only clipboard adapter. It snapshots and restores the
supported global-memory formats (text, HTML, RTF, and DIB images) without
attempting to read arbitrary clipboard formats.

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
is running, so a downgrade cannot delete newer fields. The same on-disk check
guards statistics appends, including when a fresh repository instance writes
without a preceding load. The legacy mapping adapter retains recognized
startup settings such as `autostart` during round-trips. Supported provider
choices and model canonicalization come from the provider registry while the
on-disk keys remain unchanged.

## Data ownership

| Data | Location | Content |
| --- | --- | --- |
| Settings | `%APPDATA%\ClarifyVoice\config.json` | Provider keys, endpoints, models, and UI preferences |
| Usage stats | `%APPDATA%\ClarifyVoice\usage_stats.json` | Counts, durations, model identifiers, and estimates; no transcript text |
| Working audio | `%APPDATA%\ClarifyVoice\clarifyvoice-recording-*.wav` | One unique session-owned file, deleted after the provider no longer needs it |

On non-Windows source runs, the equivalent data directory is
`~/.clarifyvoice`.

For startup configuration, precedence is persisted settings first, then the
documented environment variables (`*_API_KEY`, `*_BASE_URL`, `*_MODEL`, and
refinement variables), then built-in defaults. Environment variables therefore
provide reliable first-run/headless defaults without overwriting a user's saved
UI choices on later launches.

Each recording reserves a unique temporary WAV owned by its
`RecordingSession`. The provider reads it only during that session; cleanup
then removes it on success, provider or encoding failure, cancellation, or
application exit. SoX is stopped before cleanup, and Windows processes are
attached to a Job Object so force-closing the app cannot orphan the recorder.
Each session publishes exactly one immutable terminal state (`completed`,
`failed`, or `cancelled`); cleanup errors and retry exhaustion are tracked
separately and never rewrite that published outcome.
Stale SoX discovery runs during recorder initialization, before a hotkey can
start fresh capture. On Windows, where `SingleInstanceGuard` owns the data
directory exclusively, the same recovery removes only the legacy
`temp_recording.wav` and session-pattern WAVs there. Unix source runs skip
orphan deletion because they do not have equivalent inter-process ownership;
cleanup failures never block startup. Recorder cancellation is serialized
through process and microphone-stream setup. If shutdown happens during an
upload, the non-daemon
shutdown watcher keeps the process alive until the provider worker releases
the file, then performs bounded cleanup retries; provider requests are bounded
to 60 seconds. Shutdown is not marked complete until deletion succeeds. A
persistent cleanup failure remains observable and retains session ownership so
the path cannot be overwritten by a later recording. UI ownership observers
wait for the watcher's explicit terminal signal rather than the shorter
two-second UI timeout; a late successful retry is released on Tk's event loop,
while exhausted cleanup remains owned and visible. Escape cancellation
likewise retains ownership until recorder shutdown completes, preventing
immediate restart from reusing the recorder concurrently.

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
