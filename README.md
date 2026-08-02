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
or Groq, optionally improves the text, and pastes the result back into the app
you were using. It can also rewrite or translate selected text without opening
a separate editor.

> [!IMPORTANT]
> ClarifyVoice is bring-your-own-key software. You need an API key for at least
> one supported AI provider. Keys and local usage statistics stay on your
> computer; audio and selected text are sent directly to the provider you
> configure.

## Features

- Voice transcription and prompt-quality rewriting from any Windows app
- Safe selected-text rewriting that checks focus before replacing content
- Translation picker for selected text
- Gemini, OpenAI, Groq, and compatible custom endpoints
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
4. Open **Models**, add an API key, validate the provider, and select the
   transcription and text-refinement models.

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

Open **Models** to configure a provider, base URL, API key, and models. Ordinary
settings are stored in `%APPDATA%\ClarifyVoice\config.json`; API keys are kept
separately with Windows Data Protection API (DPAPI).

| Provider | Transcription | Text refinement | Default endpoint |
| --- | --- | --- | --- |
| Gemini | Multimodal audio | Same Gemini model | `generativelanguage.googleapis.com/v1beta` |
| OpenAI | Audio transcription API | OpenAI-compatible text model | `api.openai.com/v1` |
| Groq | Whisper-compatible audio API | OpenAI-compatible text model | `api.groq.com/openai/v1` |

Custom endpoints must implement the corresponding provider-compatible routes.
Unknown custom models work, but their cost is intentionally shown as unpriced
instead of using an unreliable estimate.

## Privacy and local data

ClarifyVoice has no project-owned server. Provider requests go from your
computer to the endpoint you select. The app stores:

- provider settings in `%APPDATA%\ClarifyVoice\config.json`;
- DPAPI-encrypted provider keys in
  `%APPDATA%\ClarifyVoice\secrets.dpapi.json`, decryptable only by the same
  Windows user on the same machine;
- anonymous usage counters in `%APPDATA%\ClarifyVoice\usage_stats.json`;
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
repositories.py                Versioned configuration and statistics storage
update_security.py             Authenticated manifest and atomic update checks
version.py                     Packaged application version
desktop_state.py               Small workflow state controller
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

Optional local-transcription groundwork is being developed separately from the
current product runtime. It does not add a user-facing provider or bundle a
model. Maintainers can review the checksummed sidecar design, privacy trade-offs,
and pending Windows acceptance work in [Local ASR](docs/local-asr.md).

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
