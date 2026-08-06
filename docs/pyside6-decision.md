# PySide6 UI adoption record (issue #24)

Status: **adopted for production**. The Qt Quick/QML frontend now replaces the
old CustomTkinter runtime. This record remains the home for Windows
performance, accessibility, DPI, and packaging evidence that must continue to
be collected after the migration.

Execution status for this migration: the real QML entrypoint, provider/audio/
clipboard adapters, settings, hotkeys, tray, voice translation, and file
import are wired into the production start and packaging paths. The Linux
worktree validates the source runtime and QML load; final Windows executable
and manual UI acceptance still run in CI and on the target Windows machine.

## Scope and integration

The production entrypoint is `spikes/pyside6/qml_app.py`. It composes the real
`WorkflowService`, typed provider registry, recording session, native clipboard
transaction, persisted settings, system tray, global hotkeys, voice translation,
and audio-file import controllers. `requirements.txt`, `start.bat`, the
PyInstaller scripts, CI, and release workflows all select PySide6 and this
entrypoint.

The old widget implementation and the Electron prototype are not imported,
started, or included in the production executable. The remaining comparison
scripts under `spikes/pyside6/` are historical measurement tooling only.

The original fake workflow remains useful for deterministic visual checks. The
production QML surface now uses the real runtime and additionally covers
settings persistence, native hotkeys, file import, translation flows, and the
provider/audio/clipboard boundaries.

## Evidence protocol

The comparable-build protocol in `spikes/pyside6/package.ps1` is historical
evidence tooling. It can still compare the old widget build with the Qt
implementation, but it is not the production build path. Production packaging
uses `scripts/build.ps1`, which packages the QML entrypoint and its QML assets
as the single `ClarifyVoice.exe` artifact. The scripts write
`build-environment.txt` and
`artifacts-manifest.json` containing the commit, dependency-file hashes, tool
versions, package sizes, and SHA-256 hashes. This makes a later CSV row
auditable against the exact binaries that were measured. The manifest is
evidence metadata only; it is not a release attestation.

`spikes/pyside6/benchmark.ps1` requires that manifest, verifies the selected
executable hash against its target entry before launching, and records the
executable SHA-256, expected manifest artifact SHA-256, and manifest SHA-256 in
every measurement row. It then records its Windows boot identifier plus a
stable, non-reversible `HostId`: the
lowercase `sha256:` plus 64-hex-character SHA-256 digest of the Windows
registry `MachineGuid` (the raw identifier is never written).
`spikes/pyside6/aggregate.ps1` rejects failed launches or invalid metrics before
counting samples, parses process/window/thread counts as positive invariant
integers, normalizes round spellings and timestamp-form boot IDs before
counting them, rejects malformed or ambiguous boot IDs/host IDs, rejects
mixed-host input, excludes its own output on reruns, rejects fewer than three
independent post-reboot rounds per target, and reports medians for cold-start
observations, working set/private memory, process count, thread count, and
package size. Every measurement row must contain all three nonblank SHA-256
fields; malformed, partially edited, or hashless rows are rejected. A single
manifest digest and one executable hash per target are required across the
imported dataset, so rounds cannot silently mix separate builds. A failed
launch is never silently converted into a zero or discarded as an outlier.
The executable hash and the manifest's expected artifact hash must also be
identical for each target; a forged pair is rejected before medians are
computed.

The protocol intentionally has no fixed target order: run only one target after
each reboot, alternate which target is first across at least three rounds, and
aggregate the resulting rows. This controls the documented order/cache bias as
far as a practical Windows spike can, but it does not prove a perfectly cold
OS/filesystem/antivirus state. No single run or target order is sufficient for
a framework comparison.

The following evidence remains a post-migration acceptance backlog for one
Windows 10/11 machine:

| Evidence | Baseline reference | Qt Quick production | Status |
| --- | --- | --- | --- |
| Cold-start observation (ms) | pending independent rounds | pending independent rounds | blocked in this Linux worktree |
| Idle working set/private memory | pending Windows run | pending Windows run | blocked in this Linux worktree |
| Process/thread count | pending Windows run | pending Windows run | blocked in this Linux worktree |
| One-file package size | pending Windows run | pending Windows run | blocked in this Linux worktree |
| 100/125/150% DPI screenshots | pending | pending | manual Windows validation required |
| Overlay/focus/hotkey/tray behavior video | pending | pending | manual Windows validation required |

Do not substitute Linux measurements or call one post-reboot row a cold-state
comparison: the acceptance question is Windows UI behavior and the production
executable uses Windows-specific hotkeys and packaging.

The operator-facing capture sheet is
`spikes/pyside6/evidence-template.md`. It records the build manifest, reboot
order, display scales, screenshots, video filenames, and manual pass/fail
notes. Attach the completed sheet and generated CSVs to issue #24; keep them
out of the repository so user paths, screenshots, and machine metadata do not
become source artifacts.

## Qualitative comparison

| Area | PySide6 potential benefit | Regression/cost to prove |
| --- | --- | --- |
| Composition and animation | Qt's mature scene/widget primitives may make transitions and rounded overlays easier to maintain. | Qt's platform integration and stylesheet/rendering choices still need visual checks at multiple DPI values. |
| Accessibility | Qt exposes accessible names, keyboard focus, tab order, and platform bridges. | Every production surface needs an accessibility audit; the spike only establishes a starting point. |
| DPI and window behavior | Qt 6 has built-in high-DPI handling and native window flags. | Windows no-activate, layered transparency, taskbar/tray, and global hotkey coexistence are OS-level behaviors that mocks cannot establish. |
| Packaging | PyInstaller can package a Python Qt app using the same broad delivery shape. | Qt DLL/plugin payloads increase package size and redistribution obligations; startup time must be measured. |
| Migration | Qt Quick keeps provider, workflow, recording, and clipboard responsibilities behind typed runtime seams. | Windows focus, DPI, accessibility, and packaging acceptance still need real-device evidence. |

## Licensing and redistribution

Qt for Python documents PySide6 as available under LGPLv3/GPLv3 and the Qt commercial license: <https://doc.qt.io/qtforpython-6/>. The detailed component notices are maintained by Qt for Python at <https://doc.qt.io/qtforpython-6/licenses.html>. Qt's commercial-use guidance warns commercial users not to use the community `pip install pyside6` distribution as a substitute for a commercial Qt license: <https://doc.qt.io/qtforpython-6.10/commercial/index.html>.

Production release engineering must:

- choose and record the applicable Community LGPLv3/GPLv3 or commercial path before shipping;
- ship the required Qt DLLs/plugins and the corresponding license/third-party notices;
- preserve the ability for LGPL users to relink or replace the Qt components as required by the selected distribution terms;
- audit PyInstaller output and every Qt add-on for its own license and notice requirements; and
- keep the production MIT license and Qt notices clearly separate.

This is a release gate, not legal advice. The Windows package now includes the
PySide6 runtime selected by the locked dependency set.

Before any production adoption, record a component-level license inventory for
the exact PySide6/Qt wheels and plugins selected by the package manifest. The
release checklist must identify whether the distribution follows the community
LGPLv3/GPLv3 path or a commercial Qt agreement, include the applicable notices,
and document how users can replace/relink the LGPL-covered components. The
MIT-licensed ClarifyVoice code remains independent of those obligations.

## Migration result and remaining acceptance

The migration is deliberately direct: there is no runtime feature flag, dual
frontend selection, rollback implementation, or compatibility path in the
production startup. `start.bat`, `start.sh`, PyInstaller, CI, and release all
select the QML entrypoint. The frontend consumes typed workflow/runtime
boundaries instead of importing the old UI.

Remaining work is validation, not a second implementation:

- run the packaged executable on Windows 10/11 at 100%, 125%, and 150% DPI;
- verify focus-safe paste, global hotkeys, tray activation, audio recording,
  file import, translation, and settings persistence on the target device;
- record cold-start, memory, thread-count, and package-size evidence; and
- complete the PySide6/Qt license and notices inventory for the exact release.

These checks are release acceptance criteria. They do not restore or preserve
the removed frontend as an alternate runtime.
