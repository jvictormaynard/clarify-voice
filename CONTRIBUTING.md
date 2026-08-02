# Contributing to ClarifyVoice

Thank you for helping improve ClarifyVoice. Contributions are welcome from
first-time and experienced open-source contributors.

## Good first contributions

- Reproduce and document a bug
- Improve installation or troubleshooting instructions
- Add or refine an interface translation
- Add tests around provider routing or workflow safety
- Improve accessibility, keyboard behavior, or DPI handling
- Support another compatible transcription or refinement provider

Large UI rewrites, new resident services, or broad dependency changes should be
discussed in an issue first. ClarifyVoice is intentionally lightweight and
focus-safe.

## Before opening an issue

1. Search existing issues.
2. Confirm the problem on the newest release or current default branch.
3. Remove API keys, selected text, transcripts, personal paths, and provider
   request bodies from screenshots and logs.
4. For provider failures, include the provider, model ID, endpoint type
   (official or custom), HTTP status, and sanitized error message.

Use the bug or feature template so maintainers receive enough context to act.
For usage questions, follow [SUPPORT.md](SUPPORT.md). Report vulnerabilities
privately as described in [SECURITY.md](SECURITY.md).

## Development setup

On Windows:

```powershell
git clone https://github.com/jvictormaynard/clarify-voice.git
cd clarify-voice
.\scripts\setup.ps1 -Dev
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe app.py
```

Read [docs/development.md](docs/development.md) for builds, platform notes, and
the full Windows acceptance checklist. Read
[docs/architecture.md](docs/architecture.md) before moving responsibilities
between modules.

## Making a change

1. Fork the repository and create a focused branch from the default branch.
2. Keep unrelated refactors out of the same pull request.
3. Add or update tests when behavior changes.
4. Update user documentation when installation, shortcuts, providers, privacy,
   or configuration changes.
5. Run the checks below.
6. Build and test the real Windows executable for integration or visual changes.

Use clear commit messages. Conventional prefixes such as `fix:`, `feat:`,
`docs:`, `test:`, and `chore:` are encouraged but not required.

## Required checks

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q app.py repositories.py secret_store.py desktop_state.py version.py windows_hotkeys.py windows_clipboard.py provider_types.py provider_adapters.py provider_http.py provider_registry.py local_asr.py scripts/local_asr_harness.py tests
ruff check desktop_state.py windows_hotkeys.py scripts/dependency_audit.py tests/test_repository.py
mypy desktop_state.py windows_hotkeys.py
python scripts/dependency_audit.py
```

Dependency intent is kept in `requirements.txt` and `requirements-dev.txt`;
the generated platform locks (`requirements-lock-linux.txt` and
`requirements-lock-windows.txt`) are the shared inputs for setup, CI, packaging,
and releases. If intent changes, regenerate each lock on its matching runner
with `python -m piptools compile --strip-extras --output-file=...` and include
both resulting diffs. Do not add an audit exception without a reviewed rationale
in `dependency-audit.json`.

For packaging-related changes:

```powershell
.\scripts\build.ps1
```

Do not accept a Windows UI change based only on unit tests. Check the installed
or built executable, including focus behavior, global shortcuts, the system
tray, window transparency, and at least the DPI setting where the defect was
reported.

## Pull requests

A useful pull request:

- explains the user-visible problem and solution;
- links the related issue when one exists;
- lists the exact validation performed;
- includes screenshots or a short recording for visual changes;
- calls out platform-specific limitations;
- contains no secrets or personal user data;
- keeps generated executables, `.env`, and local config files out of Git.

Maintainers may ask for a smaller scope when a change mixes unrelated concerns.
Review is collaborative, and specific technical disagreement is welcome when it
stays respectful and evidence-based.

## Translations

When adding a new interface language:

1. Add the language and flag mapping in `app.py`.
2. Translate every key in the interface catalog.
3. Add tests showing that the catalog is complete and the flag renders.
4. Check controls at normal and scaled DPI because translated labels vary in
   length.

## Licensing

By submitting a contribution, you agree that it may be distributed under the
project's [MIT License](LICENSE). Do not submit code, images, icons, or other
material that you do not have permission to redistribute. Identify any
third-party material and its license in the pull request.
