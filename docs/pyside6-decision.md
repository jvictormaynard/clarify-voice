# PySide6 UI decision record (issue #24)

Status: **provisional defer**. This spike answers whether PySide6 is worth a measured follow-up; it does not authorize a production rewrite.

Execution status for this PR: **no Windows measurements, screenshots/video, or
manual UI validation were performed**. The Linux worktree only validates the
fake model, source isolation, Python compilation, and PowerShell syntax. Every
Windows evidence row below remains pending; the recommendation is deliberately
scoped to that limitation.

## Scope and isolation

The prototype lives in `spikes/pyside6/`. It has its own optional dependency file, a Qt-free fake workflow model, and a standalone entry point. Production startup (`app.py`), provider/audio/clipboard logic, `requirements.txt`, and the normal PyInstaller scripts are unchanged. The spike's package script writes only below `spikes/pyside6/`.

The fake workflow covers the idle surface, recording/processing/success pill, result panel, settings page, tray show/quit, frameless dragging, rounded corners, transparency, always-on-top, and no-activate overlay flags. It deliberately does not duplicate provider or recording logic and does not register a global hotkey.

## Evidence protocol

The comparable-build protocol is `spikes/pyside6/package.ps1`; it packages the current CustomTkinter entry point and the PySide6 prototype as separate one-file windowed executables in one isolated environment. `spikes/pyside6/benchmark.ps1` measures one target per invocation and records its Windows boot identifier, while `spikes/pyside6/aggregate.ps1` rejects failed launches or invalid metrics before counting samples, excludes its own output on reruns, rejects fewer than three independent post-reboot rounds per target, and reports medians for cold-start observations, working set/private memory, process count, thread count, and package size.

The protocol intentionally has no fixed target order: run only one target after
each reboot, alternate which target is first across at least three rounds, and
aggregate the resulting rows. This controls the documented order/cache bias as
far as a practical Windows spike can, but it does not prove a perfectly cold
OS/filesystem/antivirus state. No single run or target order is sufficient for
a framework comparison.

The following evidence must be captured on one Windows 10/11 machine before adopting anything:

| Evidence | CustomTkinter | PySide6 | Status |
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

## Qualitative comparison

| Area | PySide6 potential benefit | Regression/cost to prove |
| --- | --- | --- |
| Composition and animation | Qt's mature scene/widget primitives may make transitions and rounded overlays easier to maintain. | Qt's platform integration and stylesheet/rendering choices still need visual checks at multiple DPI values. |
| Accessibility | Qt exposes accessible names, keyboard focus, tab order, and platform bridges. | Every production surface needs an accessibility audit; the spike only establishes a starting point. |
| DPI and window behavior | Qt 6 has built-in high-DPI handling and native window flags. | Windows no-activate, layered transparency, taskbar/tray, and global hotkey coexistence are OS-level behaviors that mocks cannot establish. |
| Packaging | PyInstaller can package a Python Qt app using the same broad delivery shape. | Qt DLL/plugin payloads increase package size and redistribution obligations; startup time must be measured. |
| Migration | A UI-only seam could preserve providers, workflow controller, and clipboard safety. | The current `app.py` owns UI and core responsibilities; extracting stable interfaces is a multi-stage project with rollback risk. |

## Licensing and redistribution

Qt for Python documents PySide6 as available under LGPLv3/GPLv3 and the Qt commercial license: <https://doc.qt.io/qtforpython-6/>. The detailed component notices are maintained by Qt for Python at <https://doc.qt.io/qtforpython-6/licenses.html>. Qt's commercial-use guidance warns commercial users not to use the community `pip install pyside6` distribution as a substitute for a commercial Qt license: <https://doc.qt.io/qtforpython-6.10/commercial/index.html>.

If PySide6 were adopted, release engineering would need to:

- choose and record the applicable Community LGPLv3/GPLv3 or commercial path before shipping;
- ship the required Qt DLLs/plugins and the corresponding license/third-party notices;
- preserve the ability for LGPL users to relink or replace the Qt components as required by the selected distribution terms;
- audit PyInstaller output and every Qt add-on for its own license and notice requirements; and
- keep the production MIT license and Qt notices clearly separate.

This is a release gate, not legal advice. No Qt binary is included by this PR.

## Migration and rollback outline

1. Extract provider/audio/clipboard/workflow protocols from `app.py` behind tests, without changing the existing Tk implementation.
2. Port one non-critical settings/result surface behind a feature flag and compare Windows evidence.
3. Port the overlay and tray, then manually validate focus, hotkeys, DPI, and accessibility on supported Windows versions.
4. Keep the Tk path as the rollback implementation until two release cycles of equivalent evidence pass.
5. Remove the flag and legacy path only after a separately approved migration issue.

Rollback is deleting the feature flag and returning to the unchanged CustomTkinter entry point; no provider data or user configuration format should depend on the Qt surface.

## Recommendation

**Defer adoption pending Windows evidence.** The prototype shows that the requested surfaces can be expressed without rewriting the core, and Qt has plausible benefits for accessibility, DPI, and animation. It is not a clear improvement until the comparable package/memory/startup measurements and manual focus/hotkey/tray/DPI checks are recorded. If those checks do not show a material UX or maintenance win after accounting for Qt payload/licensing cost, reject a production migration and close issue #24 with this record.
