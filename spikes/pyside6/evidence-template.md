# PySide6 decision-spike evidence record

Copy this file outside the repository for the Windows run and attach the
completed copy to issue [#24](https://github.com/jvictormaynard/clarify-voice/issues/24).
Do not commit executable artifacts, screenshots, recordings, or machine
identifiers to the source repository. The benchmark's `HostId` is a one-way
SHA-256 digest intended only to prove that both targets used the same machine.

## Run identity

| Field | Value |
| --- | --- |
| Repository commit | |
| Windows edition/build | |
| Architecture | |
| Python version | |
| PyInstaller version | |
| Display scaling (default) | |
| Display scaling (alternate) | |
| Antivirus/Defender state | |
| HostId from CSV rows | |
| Artifact manifest SHA-256 | |
| Source tree status | clean (required) |
| Operator/date | |

The same physical Windows 10/11 machine must be used for every row. Record
whether the machine was rebooted immediately before each row; do not call a
resume, logoff, or process restart a post-reboot round.

## Measurement rows

Build once with `package.ps1`, then reboot before each target invocation. Use
one target per reboot and alternate which target runs first:

```powershell
powershell -ExecutionPolicy Bypass -File spikes\pyside6\package.ps1

# Round 1: CustomTkinter first, then reboot before PySide6.
powershell -ExecutionPolicy Bypass -File spikes\pyside6\benchmark.ps1 `
  -Target CustomTkinter `
  -Executable spikes\pyside6\artifacts\customtkinter\ClarifyVoice-customtkinter.exe `
  -ArtifactManifest spikes\pyside6\artifacts\artifacts-manifest.json `
  -RunId round-1-ctk -Round 1 -OutputCsv measurements\round-1-ctk.csv

# Round 2: PySide6 first, then reboot before CustomTkinter.
powershell -ExecutionPolicy Bypass -File spikes\pyside6\benchmark.ps1 `
  -Target PySide6 `
  -Executable spikes\pyside6\artifacts\pyside6\ClarifyVoice-pyside6.exe `
  -ArtifactManifest spikes\pyside6\artifacts\artifacts-manifest.json `
  -RunId round-2-qt -Round 2 -OutputCsv measurements\round-2-qt.csv
```

Each row includes the executable SHA-256, the expected manifest artifact
SHA-256, and the manifest SHA-256. `benchmark.ps1` verifies these values before
launching, so replacing either the executable or the manifest fails closed.
Repeat until every target has at least three distinct `BootId` values and
three distinct logical `Round` values. Attach the individual CSV files and
the generated `artifacts-manifest.json`, then run:

```powershell
powershell -ExecutionPolicy Bypass -File spikes\pyside6\aggregate.ps1 `
  -InputCsv (Get-ChildItem measurements\*.csv | Select-Object -Expand FullName) `
  -OutputCsv measurements\summary.csv
```

The aggregator must accept the summary and report two rows. If it rejects a
row, correct the measurement procedure or attach the failure as evidence; do
not edit a CSV to make a result pass. The summary is not valid if the rows
come from mixed hosts, failed launches, malformed IDs, or fewer than three
independent post-reboot rounds per target. It must also use one manifest
digest for the entire dataset and one executable digest per target; if the
package step is rerun, start a new evidence set instead of mixing CSV files.

## Manual behavior matrix

For each target, mark `PASS`, `FAIL`, or `N/A` and add a short observation.
Attach a screenshot for every DPI row and a short screen recording for the
focus/tray/hotkey sequence. The recording must show the target name and the
Windows display scale so the two captures are comparable.

| Behavior | CustomTkinter | PySide6 | Evidence filename / note |
| --- | --- | --- | --- |
| Idle surface and result panel | | | |
| Recording/processing/success pill | | | |
| Always-on-top | | | |
| No-activate overlay (background app keeps focus) | | | |
| Transparency and rounded corners | | | |
| Frameless dragging | | | |
| Tray show/hide/quit | | | |
| Keyboard navigation and accessible names | | | |
| Animation smoothness | | | |
| Production global-hotkey coexistence | | | |
| 100% DPI screenshot | | | |
| 125% DPI screenshot | | | |
| 150% DPI screenshot | | | |

If a display scale is unavailable, record the actual scale and why the row is
`N/A`; do not extrapolate from another scale. If a behavior cannot be tested
without touching the shared installation, leave it pending and describe the
missing access rather than claiming a pass.

## Decision sign-off

- [ ] Measurements were captured on one Windows host with alternating target
      order and at least three reboot rounds per target.
- [ ] The PowerShell aggregator accepted only the intended rows and produced a
      summary without warnings.
- [ ] Screenshots/video cover the manual matrix or every missing item is
      explicitly recorded as pending.
- [ ] The artifact/environment manifest is attached and matches the tested
      commit.
- [ ] `package.ps1` was run from a clean tree and every CSV row contains the
      executable, expected manifest-artifact, and manifest SHA-256 values.
- [ ] Licensing/redistribution review is recorded in `docs/pyside6-decision.md`.

The source recommendation remains **defer** until this record is complete. A
later decision may **adopt** PySide6 only if the proposed resource budgets and
all focus/DPI/accessibility/hotkey gates pass. Otherwise record **reject** and
close issue #24 without a production migration.
