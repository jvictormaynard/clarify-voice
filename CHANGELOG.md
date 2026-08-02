# Changelog

Notable user-facing changes are documented here. This project follows
[Semantic Versioning](https://semver.org/) for tagged releases.

## [Unreleased]

### Fixed

- Gave each recording an explicit session owner with unique temporary audio,
  bounded SoX shutdown, cancellation, stale-worker protection, and cleanup on
  success, failure, cancellation, and application exit
- Moved stale SoX discovery out of the first-audio hot path and coordinated
  shutdown with active provider uploads before the final cleanup retry
- Removed legacy and session-pattern WAVs left behind by an interrupted
  startup on Windows, while keeping cleanup limited to the exclusively owned
  app data directory
- Kept failed-cleanup sessions owned until a bounded retry succeeds, preventing
  a later recording from overwriting an unremoved temporary WAV
- Kept UI ownership observers alive through the bounded cleanup policy so a
  late successful retry releases the session, while exhausted cleanup remains
  deterministically owned and observable
- Made each recording's terminal outcome immutable; cleanup failures no longer
  rewrite a published `completed` or `cancelled` state
- Retained rapid stop requests issued before recorder startup publication and
  bounded provider-worker shutdown joins without deleting an in-use WAV

## [0.1.2] - 2026-07-31

### Fixed

- Allowed short and sub-second recordings to be transcribed while preventing
  an immediate stop from racing microphone startup

## [0.1.1] - 2026-07-27

### Fixed

- Preserved provider-card borders with CustomTkinter 6
- Removed the initial microphone capture delay caused by stale-recorder cleanup
- Restored reliable `Alt+R` visibility toggling after `Alt+T` translations
- Restored the standard fade-in when the main window returns after translation
- Shared native layered-window types to prevent Windows transparency failures
- Made Windows release-source downloads resilient to redirects and transient
  network failures

### Changed

- Updated CustomTkinter, Pillow, sounddevice, Requests, and PyInstaller
- Isolated maintainer deployments from system Python dependencies

## [0.1.0] - 2026-07-19

### Added

- Open-source project documentation and contribution guidelines
- Automated Windows setup, build, CI, and tagged-release workflows
- Native system-tray branding and expanded interface languages on the active
  development branch

### Changed

- Clarified the Python application as the maintained implementation
- Archived the incomplete Electron prototype under `legacy/`
- Removed the redundant vendored SoX ZIP while retaining the runtime and license

### Security

- Local `.env` files and API keys are excluded from portable builds

[Unreleased]: https://github.com/jvictormaynard/clarify-voice/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/jvictormaynard/clarify-voice/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/jvictormaynard/clarify-voice/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/jvictormaynard/clarify-voice/releases/tag/v0.1.0
