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

The HTTP dependency is intentionally the narrow `HttpClient` protocol with a
single `request` method and guarded JSON decoding. Adapters preserve the
existing request payloads and response shapes; session ownership, timeouts,
retry, cancellation, logging, and shared error policy live in `provider_http.py`.
Connections arrive from the repository-loaded configuration (with provider
secrets resolved by `SecretStore`); the transport never persists credentials or
reads environment variables itself.

### Workflow-scoped routing

`workflow_config.py` is the UI-free boundary for routing decisions. It models
independent `transcription`, `refinement`, `rewrite`, `translation`, and
`local_asr_refinement` scopes. Each route carries a canonical provider ID,
model ID, prompt policy, optional custom endpoint, and an explicit enabled
flag; API keys are never part of a route. `validate_workflow_config` checks the
registry capability required by every scope and rejects unsupported
combinations before an adapter or local sidecar can start. Custom endpoints are
HTTP(S)-only and cannot contain URL userinfo, query parameters, or fragments;
this keeps adapter path composition deterministic and avoids credential-bearing
URLs at rest. Its
`test_workflow_configuration` result is local and diagnostic-safe: it makes no
provider request and does not persist prompt text.

### Voice translation foundation (`voice_translation.py`)

Issue #49 adds a separate, UI-free contract for voice translation rather than
coupling a new hotkey to the selected-text `WorkflowService`. The versioned
`VoiceTranslationConfig` stores an automatic or explicit source language, one
explicit target language, and a dedicated provider/model/prompt route. Route
validation delegates to the provider capability registry and therefore rejects
providers without `TEXT_GENERATION` before a request can start; it performs no
network call and keeps prompts out of diagnostics.

`VoiceTranslationWorkflow` is a deterministic transaction seam for the future
recorder and Windows adapters. Its immutable state machine retains the raw
transcript across translation, focus, clipboard, and publication failures. A
translation failure or empty result produces an explicit `COPY_ONLY` decision
containing that raw transcript. A successful translation is only `PASTED` when
both the captured target is still current and the clipboard adapter says it
owns the publication transaction; otherwise it is explicitly `COPY_ONLY`.
An atomic `claim_publication` state transition is a non-cancellable barrier:
once the clipboard effect starts, cancellation is ignored and the eventual
completion remains consistent with the published outcome. The application-wide
default `VoiceTranslationPublicationCoordinator` serializes external
publication effects across separately constructed workflow objects, so two
global actions cannot paste over one another. The policy is side-effect free
and can be reviewed independently of any desktop API. Every worker carries the
operation ID captured when it starts; all transitions and publication claims
verify that ID, so a late completion from a cancelled run fails closed instead
of mutating or publishing into a newer operation. Its caller receives the
originating operation's terminal snapshot, never the newer operation's state.

This is intentionally scaffolding, not the packaged feature. A follow-up must
connect the state machine to recording, global hotkey dispatch, the focus-safe
Windows clipboard transaction, visible local/cloud routing and local-ASR
cloud-refinement opt-in, settings persistence, shutdown/cancellation, and
manual acceptance in three real applications. No `app.py` route or packaged
Windows claim is made by this foundation.

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

### `workflows.py`

Defines the staged, UI-independent application-service boundary for dictation,
rewrite, and translation. The service accepts explicit command dataclasses and
publishes immutable workflow states. It owns overlap prevention and assigns a
monotonic operation ID to every session; workers check that ID before provider
results can change the clipboard, statistics, or current state.

Gateway calls never run while the service lock is held. Dictation delivers its
terminal `COMPLETED` state before claiming clipboard/statistics publication;
rewrite and translation deliver a non-cancellable `PUBLISHING` barrier first.
Cancellation or shutdown that wins before a claim performs neither output nor
usage accounting. An accepted publication records usage once, and terminal
release waits for a blocked external gateway so the UI cannot announce `READY`
before its effect has returned.

```text
Tk command dispatcher                 Tk state renderer
          |                                  ^
          v                                  |
   WorkflowService ---- immutable WorkflowState
          |
          +---- ProviderGateway (typed registry facade)
          +---- AudioGateway
          +---- ClipboardGateway
          +---- WorkflowConfig
          +---- StatisticsGateway
          +---- Scheduler / Clock
```

The gateway protocols describe ownership boundaries; they do not reimplement
provider routing, HTTP policy, recording lifecycle, clipboard transactions, or
configuration persistence. `ProviderGateway` is the workflow-facing facade for
the typed requests and results routed by `ProviderRegistry`; workflows never
branch on provider IDs or call adapter HTTP directly.

The recording boundary is connected to the real `RecordingSession` lifecycle
from issue #18 through `RecordingAudioGateway`. A stop waits for startup to
become terminal and returns an immutable `RecordingSnapshot` containing both
the owned path and in-memory bytes. The session can therefore complete cleanup
as soon as the snapshot exists without tying its temporary WAV to a slow
provider request. The active Tk path uses the same stop/snapshot/terminal
methods, so the scaffolding does not duplicate start-stop or cleanup policy.

The runtime adapters in `app.py` connect the service to the typed provider
registry, the real `RecordingSession`, and the focus-safe Windows clipboard.
The hotkeys capture the original `SelectionTarget` before Tk can take focus and
dispatch explicit workflow commands; legacy helpers remain only for narrow
compatibility tests until their callers are retired.

### `audio_file_batch.py`

Defines the UI-independent local-file import boundary staged for issue #54.
`AudioFileBatchService` validates a finite local path list, exposes explicit
provider/model/language selection, snapshots WAV inputs, and normalizes other
allowlisted SoX formats into a private temporary WAV. `AudioBatchJob` submits
at most a configured number of provider operations, publishes per-file states,
propagates cooperative cancellation, and preserves successful results when
another file fails. `RegistryAudioTranscriptionGateway` reuses the typed
`ProviderRegistry` contract rather than duplicating provider or HTTP logic.

Imported paths remain user-owned. The service never deletes an original,
resolves a URL, or creates a persistent queue; conversion directories are
removed in a bounded `TemporaryDirectory` scope. A future Tk file-picker or
drag-and-drop surface should marshal the callback onto the Tk loop and own only
the presentation/lifecycle controls. Packaged UI and representative Windows
format acceptance remain follow-up evidence, not a claim of this extraction.

### `microphone_controls.py`

This module is the UI-free foundation for issue #52. It normalizes microphone
inventories into stable identities that do not persist transient PortAudio
indexes (while retaining a current-snapshot index for a future stream adapter),
resolves a saved endpoint to `selected`, `default`, visible
`fallback_default`, or explicit `unavailable` state, and serializes only the
selected identity for a future configuration boundary. Ambiguous same-name
endpoints without a backend-native ID fail closed instead of routing to an
arbitrary device.

`RecordingControls`, `MaximumDurationPolicy`, `SilenceVADPolicy`, and
`RecordingBoundaryPolicy` keep duration/VAD decisions deterministic and UI- or
provider-free. Hard duration limits and silence detection are opt-in; the
defaults preserve the current no-auto-stop behavior. Policies report terminal
reasons but do not stop streams, publish transcripts, or own temporary WAV
cleanup. Settings integration, hot-plug handling, audio cues, and packaged
Windows acceptance remain follow-up work so the existing `Recorder` and
`RecordingSession` cleanup/cancellation contract is not duplicated here.

### `desktop_state.py`

Contains the legacy `WorkflowController` used by the current Tk path to prevent
rewrite and translation from overlapping. It remains until `app.py` dispatches
the explicit commands from `workflows.py`.

### `provider_http.py`

Defines the shared provider transport policy: operation-specific connect/read
timeouts, bounded retries for safe requests, typed errors, cooperative
cancellation, redacted rotating logs, and user-requested safe diagnostic
exports. Provider-specific adapters keep ownership of payload and response
formats. See [Provider HTTP reliability and diagnostics](http-resilience.md).

### `windows_hotkeys.py`

Owns native Windows `RegisterHotKey` registration and Ctrl-key synthesis. The
typed `HotkeySettings` value in `hotkey_config.py` persists the four actions
(recording, rewrite, translation, and visibility) without coupling them to
provider adapters. Legacy installations are normalised to Alt+L, Alt+K, Alt+T,
and Alt+R. Conflicts are rejected before registration, and strict registration
rolls back every accepted ID if Windows rejects one combination; settings
therefore cannot leave a stale or partially active set.

The packaged native layer currently supports toggle recording only because
`RegisterHotKey` delivers key-down notifications and has no key-up edge. The
settings-facing activation API accepts push-to-talk only when a future
key-release-capable adapter explicitly opts in. Packaged Windows builds exclude
the optional cross-platform `keyboard` module.

### `windows_clipboard.py`

Provides the Windows-only clipboard adapter. It snapshots and restores the
supported global-memory formats (text, HTML, RTF, and DIB images) without
attempting to read arbitrary clipboard formats.

### `local_asr.py` (typed groundwork, not user-facing)

Defines an isolated installer and lifecycle manager for a separately downloaded,
checksummed `whisper.cpp` Windows sidecar and model. `LocalASRProviderAdapter`
implements the typed provider-registry contract over the narrow
`LocalTranscriptionBackend` lifecycle seam. It is registered as an explicit
local capability, while installation/progress remains an explicit product
action and no cloud fallback is implicit. The #32 signed MSI/update contract is for packaged ClarifyVoice
artifacts; this source-only sidecar harness does not claim signed-release
coverage or silently join that updater. It consumes the #18
`TranscriptionRequest.audio_bytes` snapshot;
`RecordingSession` remains the sole owner of the temporary WAV, while the local
adapter owns only inference cancellation and sidecar shutdown. Importing the
module never downloads assets; the application starts the sidecar only after an
explicit installed local route is selected.

The workflow capture path requires a sequence-bearing snapshot; it retries
transient snapshot contention and otherwise fails closed rather than falling
back to an unsafe text-only check-and-set. The copy records the sequence before
Ctrl+C and the sequence observed by that copy, then uses the adapter's atomic
ownership check for restoration. A concurrent clipboard write therefore keeps
the user's newer contents intact, even when the copied selection has no text.

### `update_security.py`

Owns the manual update trust boundary. A Windows-trusted, publisher-pinned CAB
authenticates the strict release manifest. The module rejects unexpected
channels, versions, names, origins, checksums, sizes, or publishers; downloads
through a sibling `.part` file; and revalidates the MSI immediately before
launch. It does not own UI confirmation and never runs an installer by itself.

### `tests/`

The unit suite tests provider routing, URL construction, configuration,
clipboard safety, workflow state, usage statistics, and geometry calculations.
`tests/test_workflows.py` is deliberately separate from the provider suite and
runs the core application services without importing or constructing Tk.
Windows UI acceptance still requires a real installed build because mocked
tests cannot reveal transparency, focus, DPI, or hotkey integration defects.

Repository behavior is covered separately by configuration, migration, and
statistics tests. Every repository write is JSON-encoded to a same-directory
temporary file, flushed, and atomically replaced. Unknown fields and malformed
values are ignored so a damaged or newer settings file cannot prevent startup.

### `repositories.py`

`ConfigRepository` and `UsageStatsRepository` are the application-facing
interfaces. `LocalConfigRepository` loads the existing flat settings format,
adds `schema_version`, and applies ordered idempotent migrations. Version 2
adds a nested workflow map while retaining flat keys as a compatibility
adapter, so existing transcription/refinement behavior survives migration and
older callers can continue to read their fields. Its typed `AppConfig` model
groups provider endpoints/model IDs, independent workflow routes, provider
selection, UI preferences, and startup settings. `LocalUsageStatsRepository` preserves the
anonymous event list and its existing `version` marker while also writing an
explicit `schema_version`. A file from a newer schema is loaded read-only for
compatibility; saves are refused until a version that understands that schema
is running, so a downgrade cannot delete newer fields. The same on-disk check
guards statistics appends, including when a fresh repository instance writes
without a preceding load. The legacy mapping adapter retains recognized
startup settings such as `autostart` during round-trips. Supported provider
choices and model canonicalization come from the provider registry while the
on-disk keys remain unchanged.

Settings apply through `LocalConfigRepository.apply`: all workflow routes are
validated first, then secure credentials and JSON are committed atomically.
Secret-store read-back or file-write failure restores the prior secret and
leaves the prior configuration recoverable. `reset_workflow` and
`test_workflow` provide UI-independent reset and capability-test operations;
neither operation sends transcript/prompt content to diagnostics.
Until the existing UI is migrated to typed routes, legacy flat saves compare
against the previous on-disk flat values and synchronize only the routes whose
legacy fields changed; endpoint-bearing routes in the independent rewrite,
translation, and local-ASR scopes therefore survive a shared legacy provider
change, while the primary refinement route drops its endpoint when its
provider/model is changed. All independent custom routes survive an unrelated
settings save.

### `dictionary_snippets.py`

The local dictionary and snippets are a separate, versioned non-secret profile
artifact. `LocalDictionarySnippetsRepository` owns its JSON schema, migration,
validation, and atomic replacement; it does not read or write provider keys
and therefore does not duplicate the `SecretStore` boundary. The
`DictionarySnippetService` keeps a validated in-memory snapshot for matching
and context construction. Snippet expansion derives canonical NFC spans from
the active Unicode database and walks a bounded prefix trie, so it is
provider-neutral and does not scale with every rule at every input character;
cloud adapters receive optional vocabulary through the typed
`TranscriptionRequest`, while offline adapters can ignore it. Usage statistics
never receive dictionary or snippet content. The settings UI and complete
Windows manual acceptance matrix remain staged in issue #50.

### `secret_store.py`

`SecretStore` is the provider-keyed `get`/`set`/`delete` boundary. Packaged and
source Windows runs use current-user DPAPI and persist only base64 ciphertext.
The configuration repository performs first-read migration: it writes the
legacy plaintext value, reads it back through the secret store, and only then
atomically removes that provider key from `config.json`. A backend, read-back,
or cleanup failure keeps the legacy value recoverable.

Experimental Linux and macOS source runs use a separate plaintext
`~/.clarifyvoice/secrets.json` fallback with mode `0600` where supported. This
fallback is intentionally explicit; it does not claim OS-backed protection.

## Data ownership

| Data | Location | Content |
| --- | --- | --- |
| Settings | `%APPDATA%\ClarifyVoice\config.json` | Provider endpoints, models, selections, and UI preferences; no API keys |
| Provider secrets | `%APPDATA%\ClarifyVoice\secrets.dpapi.json` | Current-user DPAPI ciphertext keyed by provider |
| Usage stats | `%APPDATA%\ClarifyVoice\usage_stats.json` | Counts, durations, model identifiers, and estimates; no transcript text |
| Dictionary and snippets | `%APPDATA%\ClarifyVoice\dictionary.json` | Versioned local terms, aliases, and bounded replacement rules; no credentials or usage events |
| Working audio | `%APPDATA%\ClarifyVoice\clarifyvoice-recording-*.wav` | One unique session-owned file, deleted after the provider no longer needs it |

On non-Windows source runs, the equivalent data directory is
`~/.clarifyvoice`.

For API keys, documented environment variables (`GEMINI_API_KEY`, legacy
`API_KEY`, `OPENAI_API_KEY`, and `GROQ_API_KEY`) override the stored credential
for that process and are never persisted. Other settings retain the precedence
of persisted settings, then their environment defaults, then built-in defaults.

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
upload, the worker snapshots the WAV into memory before entering provider
network I/O. Adapters upload that snapshot, so the filesystem handle is closed
before a request whose read timeout might be extended indefinitely; bounded
cleanup can therefore delete the WAV without waiting for the provider. The
non-daemon shutdown watcher still performs bounded retries and joins workers
for a finite initial/grace policy, retaining ownership and diagnostics when a
provider has not yet released. Shutdown is not marked complete until deletion
succeeds. A
persistent cleanup failure remains observable and retains session ownership so
the path cannot be overwritten by a later recording. UI ownership observers
wait for the watcher's explicit terminal signal rather than the shorter
two-second UI timeout; a late successful retry is released on Tk's event loop,
while exhausted cleanup remains owned and visible. Escape cancellation
likewise retains ownership until recorder shutdown completes, preventing
immediate restart from reusing the recorder concurrently.

## Packaging

PyInstaller creates a one-file Windows executable containing the Python runtime,
application modules, visual assets, immutable distribution policy, and the
vendored SoX runtime. `.env` is
never bundled. End-user provider settings are read from the user data directory.

The staged distribution workflow signs the executable, embeds it in a per-user
WiX MSI, signs that MSI and a release-manifest CAB, verifies the pinned
publisher, emits checksums, and creates GitHub provenance attestations. Public
enablement remains gated as documented in
[Windows distribution security](windows-distribution.md). The local-ASR
runtime and model are deliberately excluded from this one-file application.
Their groundwork manifest and maintainer harness remain source-only until the
remaining lifecycle, download, and #22 acceptance conventions land.

The GitHub release workflow builds on a Windows runner and publishes the
executable plus a SHA-256 checksum. `scripts/deploy.ps1` is a maintainer tool for
updating an existing local installation from WSL; contributors normally use
`scripts/build.ps1`.

## Legacy prototype

`legacy/electron-prototype/` contains the incomplete Electron implementation
that preceded the Python rewrite. It is not installed, tested, packaged, or used
at runtime. Keep changes to it separate from current application changes.
