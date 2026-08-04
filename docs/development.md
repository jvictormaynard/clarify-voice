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
installs the pinned development and packaging tools for local checks. Both modes
consume the platform-specific `requirements-lock-linux.txt` or
`requirements-lock-windows.txt`, generated from the human-maintained
`requirements.txt` and `requirements-dev.txt` intent files. Generate each lock
on its matching runner only when intentionally changing dependency intent. In
particular, compile the Windows lock on Windows so PyInstaller's `colorama`,
`pefile`, and `pywin32-ctypes` markers are resolved and Linux-only `keyboard`
is omitted:

```text
python -m piptools compile --allow-unsafe --strip-extras --output-file=requirements-lock-linux.txt requirements-dev.txt
python -m piptools compile --allow-unsafe --strip-extras --output-file=requirements-lock-windows.txt requirements-dev.txt
python -m piptools compile --strip-extras --output-file=requirements-lock-runtime-windows.txt requirements.txt
```

Do not edit any lock file by hand. Each CI runner checks its corresponding
file in a temporary path, so Linux and Windows platform markers are not
silently treated as interchangeable.

The `requirements-lock-windows.txt` file includes the development and packaging
toolchain used to build and validate the executable. The separate
`requirements-lock-runtime-windows.txt` file is compiled only from
`requirements.txt`; it contains the runtime dependency graph and is the sole
input to the release SBOM. Keeping these locks separate prevents Ruff, mypy,
pip-audit, pip-tools, CycloneDX, and PyInstaller from being reported as shipped
application components while retaining pinned, reproducible build inputs.
Development locks are compiled with `--allow-unsafe`, so their exact
`pip==26.1.2`, `setuptools==83.0.0`, and `pip-tools==7.6.0` versions are
committed and installed before the rest of the toolchain. This pip release is
compatible with pip-tools and includes fixes for the audited pip advisories.
The runtime-only lock intentionally excludes those bootstrap tools.
The release checks that every shared runtime package has the same version in
both Windows locks before building or generating the SBOM.

Environment variables are optional because provider settings can be entered in
the UI. For local automation, copy `.env.example` to `.env` and fill only the
values you need. API-key environment variables override stored credentials for
that process and are never persisted. Never commit `.env`.

Windows uses current-user DPAPI for provider keys. Linux and macOS source runs
use the explicit plaintext `~/.clarifyvoice/secrets.json` fallback because
those platforms remain experimental; the file is written with owner-only
permissions where supported. Tests inject an in-memory store or use temporary
directories and never access the developer's credential store.

## Checks

Run the same core checks used in CI:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q app.py workflows.py repositories.py workflow_config.py voice_translation.py dictionary_snippets.py microphone_controls.py secret_store.py update_security.py version.py desktop_state.py windows_hotkeys.py windows_clipboard.py provider_types.py provider_adapters.py provider_http.py provider_registry.py local_asr.py audio_file_batch.py history_store.py scripts/create_release_manifest.py scripts/local_asr_harness.py tests
```

From Linux or WSL with the dependencies installed:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q app.py workflows.py repositories.py workflow_config.py voice_translation.py dictionary_snippets.py microphone_controls.py secret_store.py update_security.py version.py desktop_state.py windows_hotkeys.py windows_clipboard.py provider_types.py provider_adapters.py provider_http.py provider_registry.py local_asr.py audio_file_batch.py history_store.py scripts/create_release_manifest.py scripts/local_asr_harness.py tests
```

Repository-specific tests live in `tests/test_repositories.py` and
`tests/test_workflow_config.py`. They cover legacy file loading, the ordered
flat-to-workflow migration, independent routes, canonical model IDs,
capability/custom-endpoint validation, future/unknown fields, atomic writes,
and rollback when a secret backend cannot be verified. The backend contract and
corrupted-entry handling are covered in `tests/test_secret_store.py`.

Workflow settings are persisted below the `workflows` key in `config.json`:

```json
{
  "workflows": {
    "translation": {
      "provider_id": "groq",
      "model_id": "llama-3.3-70b-versatile",
      "prompt": "Translate literally.",
      "custom_endpoint": "",
      "enabled": true
    }
  }
}
```

Use `LocalConfigRepository.apply(...)` to validate and commit a candidate as a
single transaction. `repository.test_workflow(scope)` performs a local
capability check only; it does not send text or prompts. `reset_workflow(scope)`
restores the provider's canonical default model and prompt for that scope.
Provider API keys remain resolved through the secure `SecretStore` and are not
part of a workflow route or its diagnostics.

After a Windows package is built, the executable can validate its actual
credential backend without touching the developer profile:

```powershell
.\dist\ClarifyVoice.exe secret-store-self-test
```

The command writes non-production markers to a temporary directory through the
same `LocalConfigRepository` used by the UI, creates a fresh repository/store
instance to verify restart reads, constructs the provider connections used by
the adapters, and then deletes every entry. It also verifies that neither the
marker values nor `gemini_api_key`, `openai_api_key`, or `groq_api_key` are
present in the temporary `config.json`. It prints only safe booleans, a success
flag, and provider names when stdout is available.
Because the release executable is a `--windowed` PyInstaller package, CI uses
`secret-store-self-test --result-file <runner-temp-file>` and validates that
file plus the exit code. A non-zero exit means the packaged backend is
unavailable or failed its config-isolation, read-back, provider-use, or
delete checks.

### Manual provider-key acceptance

The packaged self-test is the automated acceptance gate for the storage
boundary. It does not prove that the Settings UI, a real Windows restart, or a
provider request can use a key. Record those remaining checks only with
revocable, non-production credentials in a disposable Windows account or VM;
never use a personal production key and never run this procedure against an
existing ClarifyVoice profile.

Use a temporary profile for the complete manual pass:

```powershell
$dataRoot = Join-Path ([IO.Path]::GetTempPath()) ("clarifyvoice-secret-accept-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $dataRoot | Out-Null
$env:APPDATA = $dataRoot
& .\dist\ClarifyVoice.exe
```

For each of Gemini, OpenAI, and Groq:

1. In **Models → Providers**, enter a revocable test key and validate/save it
   (use the provider's documented test endpoint or a local provider-compatible
   fixture when available).
2. Before closing the app, copy the path shown below and confirm that
   `config.json` contains no provider key field and no test-key text:

   ```powershell
   $profile = Join-Path $dataRoot "ClarifyVoice"
   $config = Get-Content (Join-Path $profile "config.json") -Raw
   if ($config -match 'gemini_api_key|openai_api_key|groq_api_key') {
       throw "A provider key field was written to config.json"
   }
   ```

3. Close ClarifyVoice completely, launch the same executable again with the
   same `$env:APPDATA`, and confirm that the provider remains active and can
   complete one harmless validation/request. The masked key field may remain
   blank by design; do not overwrite it with a blank value.
4. Deactivate/clear the provider, repeat for all three providers, close the
   app, and verify that the secure store is gone or contains no entries. Keep
   the `config.json` assertion from step 2 after each clear.
5. Save the exit code, executable SHA-256, Windows version, provider names, and
   redacted screenshots/logs as evidence. Do not attach key values, request
   headers, or the DPAPI container.

Delete the disposable `$dataRoot` after the evidence is captured. This manual
matrix is still required before closing security issue #14 because CI cannot
provide the real-user UI/restart/request evidence.

Headless orchestration tests live in `tests/test_workflows.py`. They exercise
dictation, rewrite, translation, overlap prevention, focus-safe target capture,
publication ordering, shutdown cancellation, and stale-worker rejection without
constructing Tk widgets. A small app-level harness also verifies Escape cannot
reset the UI while a terminal publication is blocked.

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
6. For clipboard changes, verify plain text, HTML/RTF, image data, rapid
   repeated hotkeys, a user clipboard write during the restore delay, failed
   paste, and focus change. The generated result must remain available when
   automatic paste is unsafe.
7. Attach before and after screenshots to visual pull requests.
8. Confirm that the executable contains no `.env` or personal key.

### Hotkey configuration acceptance

The persisted `hotkeys` object keeps the original Alt+L/Alt+K/Alt+T/Alt+R
bindings when upgrading a legacy `config.json`. Settings capture validates
modifier/key support and rejects duplicates before applying them. The native
Windows registration is transactional: if another application owns one
combination, all newly accepted IDs are unregistered and the previous set is
restored. Confirm this manually from a packaged build by reserving one test
combination in another application, attempting Apply, and checking that the
other three ClarifyVoice actions still use their previous bindings.

Recording defaults to toggle mode. Push-to-talk remains unavailable in the
packaged Windows layer until a key-release-capable input adapter is present;
the settings API reports that capability instead of silently accepting a mode
that would get stuck recording.

## Maintainer deploy from WSL

```bash
npm run deploy
```

This invokes `scripts/deploy.ps1` with Windows PowerShell, stages source files on
the native Windows temporary directory, builds, backs up the installed
executable, replaces it, and restarts ClarifyVoice. Override the discovered
target with `CLARIFYVOICE_INSTALL_PATH` when needed.

## Automated quality and supply-chain checks

The CI quality gate runs staged Ruff linting, a focused Ruff format check, mypy for the
typed `desktop_state.py` and `windows_hotkeys.py` modules, the full dependency
audit, the unit-test baseline, and Python compile checks on Ubuntu and Windows.
The Windows packaging job is required before CI is green.

`scripts/dependency_audit.py` audits the locked set with `pip-audit`. The
reviewed-exception policy lives in `dependency-audit.json`; it is intentionally
empty today. Any future exception must include a maintainer-approved rationale
in that file and should be removed as soon as the dependency can be upgraded.

Tagged releases also publish `ClarifyVoice.sbom.json` (CycloneDX) from the
runtime-only `requirements-lock-runtime-windows.txt` lock, include it in the
portable ZIP. The SBOM is augmented with the bundled SoX 14.4.2 component and
the SHA-256 of every `sox.exe`/DLL selected by
`scripts/sox-runtime-manifest.json`; the source-archive SHA-256 remains source
offer evidence only. The same manifest drives `scripts/build.ps1` before GitHub
artifact attestations are created for the release files.
Each selected DLL is represented as its own CycloneDX library component with a
bundle-scoped version, SPDX license, SHA-256, and a dependency edge from the
SoX aggregate component. This avoids attributing the source archive digest to
the executable or hiding codec/runtime libraries in a generic property.
The attestation is verifiable with GitHub's artifact-attestation tooling and is
separate from the existing SHA-256 checksum.

## Experimental Linux and macOS support

The source contains fallback clipboard and hotkey paths, but the polished and
packaged product is currently Windows-first. Linux requires SoX, `xclip`,
`xdotool`, Tk, PortAudio, and permissions suitable for global keyboard events.
Wayland global shortcuts and paste automation are not supported. macOS is not
packaged and may require Accessibility permission for simulated paste.

Contributions that improve these platforms are welcome when they preserve the
Windows path and include platform-specific validation.

## Decision spikes

The isolated [PySide6 decision spike](pyside6-decision.md) is not part of
startup, runtime requirements, or production packaging. Run its Windows-only
build, benchmark, and manual validation protocol from `spikes/pyside6/` before
using its provisional recommendation. The package script records a locked
runtime, exact artifact hashes, and tool versions; use the
[`evidence-template.md`](../spikes/pyside6/evidence-template.md) to attach
Windows CSVs, screenshots, and video without committing machine-specific
artifacts.

## Release process

The repository-local `$clarifyvoice-release` skill under
`.agents/skills/clarifyvoice-release/` is the canonical maintainer procedure.
It standardizes the release PR, CI gates, tag provenance, assets, checksums, and
post-release verification.

The signed installer/update contract, Azure OIDC configuration, manual
acceptance matrix, rotation, and revocation procedure are documented in
[Windows distribution and update security](windows-distribution.md). Do not
publish an MSI or authenticated manifest until every rollout gate there is
complete.

For a local unsigned packaging check, run `npm run build` followed by
`npm run installer`. This creates `dist\ClarifyVoice-windows-x64.msi`; it does
not install it. The build requires a .NET SDK because
`scripts/build-installer.ps1` installs pinned WiX 6.0.2 under ignored
`build\tools`.

1. Choose the next semantic version from the changes since the latest tag.
2. Set `__version__` in `version.py` and the top-level `version` in
   `package.json` to the same exact `X.Y.Z` value. Diagnostics and packaged
   executables import the module, and preflight rejects metadata drift.
3. Update `CHANGELOG.md` and any documentation whose behavior changed.
4. Run the release preflight, replacing the example version:

   ```bash
   python3 .agents/skills/clarifyvoice-release/scripts/release_preflight.py \
     --repo . --version 0.1.1
   ```

5. Open and merge a focused release-preparation PR after local, Ubuntu,
   Windows, and packaging checks pass.
6. Create an annotated `vX.Y.Z` tag on the exact green `master` commit and push
   only that tag.
7. The release workflow builds on Windows, requires Azure Artifact Signing,
   verifies the EXE/MSI/manifest-CAB publisher and timestamps, generates the
   runtime-lock CycloneDX SBOM, creates checksums and provenance attestations,
   verifies the official SoX source archive, and publishes all required assets
   to the same GitHub release.
8. Download the published assets, verify signatures, checksums, manifest and
   SBOM/ZIP contents, inspect attestations, and confirm that `/releases/latest`
   resolves to the new version.

Releases through v0.1.2 remain unsigned. The staged workflow must not be run as
a public release until its protected signing configuration and manual gates are
complete.
