# Qt Quick frontend

This directory contains the Qt Quick/QML frontend entrypoint for ClarifyVoice.
The QML process uses the real `WorkflowService` and does not construct the
CustomTkinter `App` or show a Tk window. Provider, recording, clipboard,
configuration, statistics, and Qt scheduling are composed by the UI-free
`qml_runtime.py` module.

The old widget spike remains only as historical benchmark material in this
directory. It is not imported by the QML entrypoint.

## Run the frontend on Windows

From the repository root, install the optional Qt dependency into a disposable
environment:

```powershell
py -m venv spikes\pyside6\.venv
spikes\pyside6\.venv\Scripts\python.exe -m pip install -r spikes\pyside6\requirements.txt
spikes\pyside6\.venv\Scripts\python.exe -m spikes.pyside6.qml_app
```

The QML frontend exercises real idle, recording, processing, success, and
result states. Runtime, provider, microphone, and local-ASR failures surface
as actionable workflow errors; there is no fake-runtime flag or fallback path.
The local button starts/stops dictation. Global hotkeys, settings persistence,
file import, and translation remain later extraction steps and are not
silently claimed by this entrypoint.

## Comparable builds and measurements

Build both one-file, windowed artifacts into the spike directory only. The
script uses `requirements-lock-runtime-windows.txt` for the production side,
installs the optional PySide6 dependency into the same disposable environment,
and records the exact environment and artifact hashes:

```powershell
powershell -ExecutionPolicy Bypass -File spikes\pyside6\package.ps1
```

The generated `artifacts\build-environment.txt` and
`artifacts\artifacts-manifest.json` are provenance for the measurements, not
release artifacts. If a different lock or output location is required for a
controlled experiment, pass `-RuntimeRequirements`, `-SpikeRequirements`, or
`-OutputDirectory` explicitly and attach the resulting manifest.

`benchmark.ps1` requires the manifest emitted by `package.ps1`, verifies the
selected executable's SHA-256 against the target entry before launching, and
records both the executable and manifest SHA-256 in every CSV row. It then
records the Windows boot identifier plus a stable, non-reversible `HostId`.
`HostId` is the lowercase `sha256:` plus 64-hex-character SHA-256 digest of the
Windows registry `MachineGuid`; the raw machine identifier is never written to
the CSV. Run at least three independent post-reboot rounds per target,
alternating which target is measured first across rounds. Do not run both
targets in one invocation and do not describe a single row as a cold-state
comparison. For example, after each reboot run one of these commands, then use
the other target first after the next reboot:

```powershell
powershell -ExecutionPolicy Bypass -File spikes\pyside6\benchmark.ps1 `
  -Target CustomTkinter `
  -Executable spikes\pyside6\artifacts\customtkinter\ClarifyVoice-customtkinter.exe `
  -ArtifactManifest spikes\pyside6\artifacts\artifacts-manifest.json `
  -RunId round-1-ctk -Round 1 -OutputCsv measurements\round-1-ctk.csv

powershell -ExecutionPolicy Bypass -File spikes\pyside6\benchmark.ps1 `
  -Target PySide6 `
  -Executable spikes\pyside6\artifacts\pyside6\ClarifyVoice-pyside6.exe `
  -ArtifactManifest spikes\pyside6\artifacts\artifacts-manifest.json `
  -RunId round-2-qt -Round 2 -OutputCsv measurements\round-2-qt.csv
```

Aggregate only after both targets have three or more independent boot rounds:

```powershell
powershell -ExecutionPolicy Bypass -File spikes\pyside6\aggregate.ps1 `
  -InputCsv (Get-ChildItem measurements\*.csv | Select-Object -Expand FullName) `
  -OutputCsv measurements\summary.csv
```

The aggregator excludes its resolved `OutputCsv` path from the input list,
rejects failed launches or non-positive/non-numeric metrics before counting
samples, parses process/window/thread counts as positive invariant integers,
normalizes round spellings and timestamp-form boot IDs before counting them,
rejects malformed or ambiguous boot IDs/host IDs, rejects mixed-host inputs,
requires all three nonblank executable/manifest SHA-256 fields,
requires one manifest digest for the imported dataset and one executable hash
per target, and requires the executable hash to equal the manifest's expected
artifact hash,
rejects fewer than three unique boot IDs/rounds per target, and
reports medians for cold-start observations, working set/private memory,
process/thread counts, and package size. This makes reruns with
`measurements\*.csv` idempotent. The protocol provides repeated post-reboot
samples; it does not claim a perfectly controlled OS cold state.
Use 100%, 125%, and 150% display scaling where available, and attach the CSV
summary plus screenshots/video to the decision record.

Use [`evidence-template.md`](evidence-template.md) as the operator checklist.
It names the screenshot/video hooks and records missing Windows evidence
without turning it into a pass. Never commit the completed evidence sheet or
machine-specific artifacts to the repository.

## Manual behavior matrix

Record pass/fail notes for always-on-top, no-activate behavior, transparency, rounded corners, dragging, tray show/quit, DPI scaling, keyboard navigation/accessibility, animation smoothness, and coexistence with ClarifyVoice's production global hotkeys. The spike intentionally does not claim hotkey compatibility until that manual check is performed.
