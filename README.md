<p align="center">
  <img src="assets/branding/clarify-logo.png" alt="ClarifyVoice logo" width="112">
</p>

<h1 align="center">ClarifyVoice</h1>

<p align="center">
  A lightweight desktop voice assistant that turns speech into polished text in any Windows app.
</p>

<p align="center">
  <a href="https://github.com/jvictormaynard/clarify-voice/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/jvictormaynard/clarify-voice/actions/workflows/ci.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-0b7285.svg"></a>
  <img alt="Platform: Windows" src="https://img.shields.io/badge/platform-Windows-0078d4.svg">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-3776ab.svg">
</p>

<p align="center">
  <a href="docs/README.pt-BR.md">Português (Brasil)</a> ·
  <a href="#installation">Installation</a> ·
  <a href="CONTRIBUTING.md">Contributing</a> ·
  <a href="SECURITY.md">Security</a>
</p>

ClarifyVoice records from a global shortcut, transcribes with Gemini, OpenAI,
Groq, or the optional Local Whisper provider, optionally improves the text, and
pastes the result back into the app you were using. It can also rewrite or
translate selected text without opening a separate editor.

> [!IMPORTANT]
> ClarifyVoice supports bring-your-own-key cloud providers. Cloud transcription
> or text refinement requires an API key. The optional Local Whisper provider
> does not require an API key for local transcription; it downloads its
> runtime/model only after an explicit action, and its packaged Windows/offline
> acceptance is still pending. Keys and local usage statistics stay on your
> computer. Cloud audio/text goes directly to the provider you configure; Local
> Whisper keeps transcription local unless you explicitly enable cloud
> refinement.

## Features

- Voice transcription and prompt-quality rewriting from any Windows app
- Optional Local Whisper transcription after an explicit, verified asset download
- Safe selected-text rewriting that checks focus before replacing content
- Translation picker for selected text
- Gemini, OpenAI, Groq, Local Whisper, and compatible custom endpoints
- Native Windows hotkeys and system tray integration
- English, Portuguese, Spanish, German, and Russian interface languages
- Local-only usage statistics without storing transcripts
- Bundled SoX runtime in the portable Windows build
- No ClarifyVoice account, hosted backend, or telemetry service

## Installation

### Windows installer (staged)

The repository now contains the fail-closed MSI and authenticated-update
contract, but it is not enabled for public use until managed signing,
credential-storage, provenance, and manual acceptance gates are complete. When
an MSI is attached to a future release, install only
`ClarifyVoice-windows-x64.msi` whose Authenticode publisher and published
SHA-256 match that release. See [Windows distribution and update
security](docs/windows-distribution.md) for the exact install, upgrade, repair,
rollback, uninstall, signing, and incident behavior.

### Portable Windows app

The release workflow produces a self-contained `ClarifyVoice.exe`, so end users
do not need Python, Node.js, or SoX.

1. Open the [latest release](https://github.com/jvictormaynard/clarify-voice/releases/latest).
2. Download `ClarifyVoice.exe` and place it in a folder you control.
3. Double-click the executable.
4. Open **Providers** first and choose the matching onboarding path:
   - **Cloud provider:** select Gemini, OpenAI, Groq, or a compatible custom
     endpoint, then add and validate an API key. After the provider is active,
     open **Models** to select the transcription model and, when using Prompt
     mode, the text-refinement model.
   - **Local Whisper:** select Local Whisper in **Providers**, review its
     requirements, and click **Download local ASR**. No API key is required for
     local transcription. After installation, open **Models** and select Local
     Whisper for transcription. Prompt-mode cloud refinement remains optional:
     configure a cloud provider in **Providers**, select its model in **Models**,
     and enable **Allow cloud refinement for Prompt mode** only if desired. The
     Windows/offline acceptance for Local Whisper is still pending; see [Local
     ASR](docs/local-asr.md).

Published executables through v0.1.2 are not code-signed. Windows SmartScreen may therefore ask
you to confirm the first launch. Verify the SHA-256 file published with the
release if you want to check the download before running it.

No release available yet? Use the source installation below. Maintainers can
publish the first portable build by pushing a tag such as `v0.1.0`.

### Run from source on Windows

Requirements: Windows 10 or 11, [Python 3.11 or newer](https://www.python.org/downloads/windows/),
and a working microphone.

```powershell
git clone https://github.com/jvictormaynard/clarify-voice.git
cd clarify-voice
.\start.bat
```

On the first run, `start.bat` creates an isolated `.venv`, installs the Python
dependencies from the checked-in lock, and launches ClarifyVoice. Later runs
reuse that environment.
You can refresh it at any time with:

```powershell
.\scripts\setup.ps1
```

Setup, CI, packaging, and release workflows consume the checked-in platform
lock (`requirements-lock-linux.txt` or `requirements-lock-windows.txt`), so a
runner resolves the same package set for its operating system. Top-level
dependency intent remains readable in `requirements.txt` and
`requirements-dev.txt`; maintainers regenerate the matching lock with
`python -m piptools compile` when that intent changes.

Linux and macOS source paths are experimental. See
[Development](docs/development.md#experimental-linux-and-macos-support) for
their current limitations.

## Usage

| Shortcut | Action |
| --- | --- |
| `Alt + L` | Start or stop recording |
| `Esc` | Cancel an active recording |
| `Alt + K` | Rewrite the selected text |
| `Alt + T` | Translate the selected text |
| `Alt + R` | Show or hide ClarifyVoice |

The floating bar remains available through the Windows system tray. Click the
tray icon to restore it, or right-click the icon to open ClarifyVoice or quit.
The minimize button hides the app to the tray instead of closing it.

For rewrite and translation, ClarifyVoice only pastes when the original window
and selection are still active. If focus changed while the provider was
processing, the result stays in the clipboard and appears in the result panel.
Automatic paste temporarily writes the result and restores the user's Unicode
text, HTML, RTF, and DIB image clipboard formats after a short bounded delay.
If focus, paste confirmation, or clipboard ownership is lost, the generated
result remains available for manual paste and the newer clipboard contents win.

## Providers

Open **Models** to configure a provider, base URL, API key, and models. Local
Whisper does not require an API key or cloud endpoint: choose **Download local
ASR** explicitly to install its verified optional assets. Ordinary settings are
stored in `%APPDATA%\ClarifyVoice\config.json`; cloud API keys are kept
separately with Windows Data Protection API (DPAPI).

| Provider | Transcription | Text refinement | Default endpoint |
| --- | --- | --- | --- |
| Gemini | Multimodal audio | Same Gemini model | `generativelanguage.googleapis.com/v1beta` |
| OpenAI | Audio transcription API | OpenAI-compatible text model | `api.openai.com/v1` |
| Groq | Whisper-compatible audio API | OpenAI-compatible text model | `api.groq.com/openai/v1` |
| Local Whisper | Local CPU sidecar (optional download) | None by default; cloud refinement requires explicit opt-in | Local loopback only |

Custom endpoints must implement the corresponding provider-compatible routes.
Unknown custom models work, but their cost is intentionally shown as unpriced
instead of using an unreliable estimate.

Local Whisper is transcription-only and is not bundled with the executable. Its
`whisper.cpp` runtime and model are downloaded only after the user authorizes the
action and are verified against the pinned manifest. Prompt mode returns the
local transcript without refinement by default; the **Allow cloud refinement for
Prompt mode** setting is the only route that sends that transcript to the
selected cloud model. The Windows/offline product acceptance remains pending.

## Privacy and local data

ClarifyVoice has no project-owned server. Cloud provider requests go from your
computer to the endpoint you select; Local Whisper sends audio only to its own
randomized loopback sidecar. The app stores:

- provider settings in `%APPDATA%\ClarifyVoice\config.json`;
- DPAPI-encrypted provider keys in
  `%APPDATA%\ClarifyVoice\secrets.dpapi.json`, decryptable only by the same
  Windows user on the same machine;
- anonymous usage counters in `%APPDATA%\ClarifyVoice\usage_stats.json`;
- optional Local ASR assets below `%LOCALAPPDATA%\ClarifyVoice\local-asr` after
  an explicit download; the runtime and model are not bundled in the executable;
- a unique temporary WAV file while processing a recording. It is deleted after
  the provider no longer needs it, including cancellation, failure, and app
  exit; it is not retained as a recording history.

Existing plaintext keys are migrated on first load and removed from
`config.json` only after the encrypted copy has been read back successfully.
Environment variables remain runtime-only overrides and are never copied into
either settings or secret storage.

Removing the portable executable does not remove its data. Delete
`%APPDATA%\ClarifyVoice\secrets.dpapi.json` (or the whole ClarifyVoice data
directory after preserving any settings you want) to remove stored keys.
Transcript and selected-text contents are not written to usage statistics.
Read [Security](SECURITY.md) for responsible reporting guidance.

## Build a portable executable

```powershell
.\build.bat
```

The output is `dist\ClarifyVoice.exe`. The build deliberately does **not**
bundle `.env` or any local API key. See [Development](docs/development.md) for
the full setup, checks, release process, and WSL maintainer workflow.

To build an unsigned local MSI for packaging tests after the executable:

```powershell
npm run installer
```

This installs the pinned WiX compiler into ignored `build\tools`, writes only
inside the repository, and does not install or replace ClarifyVoice. Public
MSIs must come from the protected signing workflow.

## Project structure

```text
app.py                         Main UI and desktop workflows
provider_types.py              Typed provider requests, results, and capabilities
provider_adapters.py           Gemini and OpenAI-compatible adapters
provider_registry.py           Provider metadata and request routing registry
local_asr.py                    Optional local-ASR adapter, installer, and sidecar
local_asr_product.py            Explicit install/progress/removal product boundary
local_asr_manifest.json         Pinned sidecar/model assets and SHA-256 allowlist
repositories.py                Versioned configuration and statistics storage
dictionary_snippets.py         Local dictionary/snippet storage and expansion
dictionary_settings.py         Tk-independent Settings operations for the local profile
microphone_controls.py         Microphone identity and recording boundary policies
update_security.py             Authenticated manifest and atomic update checks
version.py                     Packaged application version
desktop_state.py               Small workflow state controller
workflows.py                  Headless workflow commands and orchestration service
windows_hotkeys.py             Native Windows hotkey helpers
assets/                        Product branding and provider marks
extra/sox-14.4.2/              Bundled Windows audio runtime and license
scripts/                       Setup, build, and maintainer deploy scripts
installer/                     WiX per-user MSI definition
distribution/                  Immutable packaged update trust policy
tests/                         Unit and repository-safety tests
docs/                          Architecture and development documentation
legacy/electron-prototype/     Archived first implementation, not built
.github/                       CI, release, issue, and PR automation
.agents/skills/                Repository-specific maintainer workflows
```

The current application is Python. The Electron prototype is kept only for
historical context and is excluded from builds. See
[Architecture](docs/architecture.md) before making structural changes.

Local Whisper is an explicit provider in the current product runtime. The
portable executable includes the pinned Local ASR manifest and license notices,
but not the runtime or model; installation remains an explicit user action.
Maintainers can review the checksummed sidecar design, privacy trade-offs, and
pending Windows/offline acceptance work in [Local ASR](docs/local-asr.md).

## Contributing

Bug reports, documentation improvements, translations, provider integrations,
and focused code changes are welcome. Start with
[CONTRIBUTING.md](CONTRIBUTING.md), which includes the local setup, test command,
visual validation expectations, and pull-request checklist.

- Questions and usage help: [SUPPORT.md](SUPPORT.md)
- Security reports: [SECURITY.md](SECURITY.md)
- Community expectations: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
- Changes by release: [CHANGELOG.md](CHANGELOG.md)

## License

ClarifyVoice source code is available under the [MIT License](LICENSE).
Bundled third-party software and provider marks remain under their respective
licenses and terms. Tagged releases attach the corresponding SoX 14.4.2 source
archive alongside the portable binary. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
