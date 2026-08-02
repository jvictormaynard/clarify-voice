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
.\.venv\Scripts\python.exe -m compileall -q app.py repositories.py secret_store.py desktop_state.py windows_hotkeys.py windows_clipboard.py tests
```

From Linux or WSL with the dependencies installed:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q app.py repositories.py secret_store.py desktop_state.py windows_hotkeys.py windows_clipboard.py tests
```

Repository-specific tests live in `tests/test_repositories.py` and are split
into configuration, migration, and usage-statistics cases. They cover legacy
file loading, future/unknown fields, idempotent migrations, atomic writes, and
rollback when a secret backend cannot be verified. The backend contract and
corrupted-entry handling are covered in `tests/test_secret_store.py`.

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
using its provisional recommendation.

## Release process

The repository-local `$clarifyvoice-release` skill under
`.agents/skills/clarifyvoice-release/` is the canonical maintainer procedure.
It standardizes the release PR, CI gates, tag provenance, assets, checksums, and
post-release verification.

1. Choose the next semantic version from the changes since the latest tag.
2. Update `CHANGELOG.md` and any documentation whose behavior changed.
3. Run the release preflight, replacing the example version:

   ```bash
   python3 .agents/skills/clarifyvoice-release/scripts/release_preflight.py \
     --repo . --version 0.1.1
   ```

4. Open and merge a focused release-preparation PR after local, Ubuntu,
   Windows, and packaging checks pass.
5. Create an annotated `vX.Y.Z` tag on the exact green `master` commit and push
   only that tag.
6. The release workflow builds on Windows, creates the executable, CycloneDX
   SBOM, ZIP, and SHA-256 checksum, verifies the official SoX source archive,
   publishes artifact attestations, and uploads all five assets to the same
   GitHub release.
7. Download the published assets, verify the executable checksum and ZIP
   contents, and confirm that `/releases/latest` resolves to the new version.

The executable is currently unsigned. Code signing should be introduced before
representing the app as a warning-free consumer installer.
