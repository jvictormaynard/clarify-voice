# Local ASR acceptance runbook

This runbook is the executable acceptance record for issue #23. It separates
checks that can run in the source/CI environment from checks that require the
published Windows x64 executable, a real sidecar, and a versioned audio
fixture. Unit tests and a successful PyInstaller job are useful gates, but they
do not prove the packaged Settings flow, offline inference, or process cleanup.

## Current evidence

The current source baseline is commit `fd23eeaa548745688706fdbb9933f6f52d231eaf`
(2026-08-04). From the isolated Linux worktree:

| Gate | Command/evidence | Result |
| --- | --- | --- |
| Full contract suite | `python3 -m unittest discover -s tests -p 'test_*.py'` | 711 passed, 4 skipped |
| Dependency locks | Included in the full suite (`test_dependency_lock`, `test_runtime_lock`) | Current |
| Python compilation | `python3 -m compileall -q app.py workflows.py repositories.py workflow_config.py voice_translation.py dictionary_snippets.py microphone_controls.py secret_store.py update_security.py version.py desktop_state.py windows_hotkeys.py windows_clipboard.py provider_types.py provider_adapters.py provider_http.py provider_registry.py local_asr.py audio_file_batch.py history_store.py local_asr_product.py scripts/create_release_manifest.py scripts/local_asr_harness.py tests` | Pass |
| Whitespace | `git diff --check` | Pass |
| Read-only harness status | `python3 scripts/local_asr_harness.py --root <empty-temp-root> status` | `not_installed`; no network operation |
| Unsupported-host install guard | `test_unsupported_platform_rejects_install_before_download` in the source suite | Pass; no install command or download was invoked in this documentation-only update |
| Cloud refinement regression | `test_local_asr_opted_out_refinement_is_not_accounted`, configuration fallback tests, and provider registry tests | Pass; local ASR cannot be selected as a text-refinement provider |
| CI package shape | Existing Windows test/package checks | Historical build evidence only; not rerun here and not a GUI acceptance result |

The empty-root harness run is intentionally not an installation attempt. It
proves that status is read-only and that no asset root is created by the status
check. The output is expected to contain `"state": "not_installed"` for
`status`. This documentation-only update did not invoke `install`,
`transcribe`, or `benchmark`, and did not download or install any asset.

## Windows evidence procedure

Run this section on a disposable Windows 10/11 x64 machine with AVX support,
the Microsoft Visual C++ 2015–2022 x64 Redistributable, at least 510 MB free
disk, and roughly 852 MB available RAM. Do not use a developer profile or a
production installation. Keep the evidence directory and audio fixture with
the run so the result can be reproduced.

```powershell
$run = Join-Path $env:TEMP ("clarify-local-asr-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
$evidence = Join-Path $run "evidence"
New-Item -ItemType Directory -Force $evidence | Out-Null
Get-CimInstance Win32_OperatingSystem |
    Select-Object Caption,Version,BuildNumber,OSArchitecture |
    ConvertTo-Json | Set-Content (Join-Path $evidence "os.json")
Get-CimInstance Win32_Processor |
    Select-Object Name,NumberOfLogicalProcessors,MaxClockSpeed |
    ConvertTo-Json | Set-Content (Join-Path $evidence "cpu.json")
Get-CimInstance Win32_ComputerSystem |
    Select-Object TotalPhysicalMemory |
    ConvertTo-Json | Set-Content (Join-Path $evidence "memory.json")
```

Build the current executable from the reviewed checkout and record its hash:

```powershell
.\scripts\build.ps1 -OutputDirectory $run\dist
$python = Join-Path (Get-Location) ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Build did not prepare the project Python environment" }
Get-FileHash $run\dist\ClarifyVoice.exe -Algorithm SHA256 |
    ConvertTo-Json | Set-Content $evidence\ClarifyVoice.exe.sha256.json
```

For the product UI, use a profile isolated to the run. Set these variables in
the same PowerShell session before launching the executable; they make the
Settings config and local-ASR asset root disposable:

```powershell
$env:APPDATA = Join-Path $run "roaming"
$env:LOCALAPPDATA = Join-Path $run "local"
$assetRoot = Join-Path $env:LOCALAPPDATA "ClarifyVoice\local-asr"
if (Test-Path $assetRoot) { throw "Fresh run already has local-ASR assets" }
Start-Process $run\dist\ClarifyVoice.exe
```

### Acceptance matrix

Record one JSON or text artifact per row under `$evidence`. A row is green
only when its expected observation is attached; a source test or a CI package
build alone is not a substitute.

| ID | Action | Expected observation/evidence | Current status |
| --- | --- | --- | --- |
| W1 | Open Settings → Providers → Local Whisper before clicking Download | Requirements show Windows/CPU/AVX, VC++ runtime, RAM, disk, and download; `$assetRoot` does not exist and no sidecar process is running | Source wiring covered; manual/blocked: packaged Windows UI |
| W2 | Click **Download local ASR** once and capture progress | Network starts only after the click; progress advances through runtime/model stages; final state is installed; `status` verifies every manifest digest | Source contract covered; manual/blocked: packaged Windows UI/network |
| W3 | Repeat `status` and inspect `$assetRoot` | `whisper-server.exe`, model, receipt, and license notices exist only below the owned root; no files are in the executable directory | Source contract covered; manual/blocked: Windows install |
| W4 | With a versioned WAV fixture, run `transcribe` once | Transcript is produced locally; harness JSON includes audio duration, engine/model version, and no cloud request | Harness contract covered; manual/blocked: Windows sidecar + fixture |
| W5 | Disable networking after W2, then run the same transcription again | Second transcription succeeds with network disabled; attach the offline transcript and network-isolation evidence | Code routing covered; manual/blocked: disposable offline Windows run |
| W6 | Start a fresh download and click **Cancel** during a transfer | UI becomes cancelled; no executable/model is published; no `.install-*` staging directory remains after the worker exits | Cancellation seams covered; manual/blocked: Windows UI/download |
| W7 | Start inference, then exercise Cancel and close/quit while the sidecar is active | No `whisper-server.exe`, owned process record, or temporary WAV remains after bounded shutdown; attach `Get-Process`/filesystem snapshots | Lifecycle seams covered; manual/blocked: packaged process lifecycle |
| W8 | While assets are installed, use **Remove assets** and cancel once during removal | Removal never traverses outside the owned root; a cancelled partial removal keeps the marker for retry; a second removal leaves `$assetRoot` absent | Removal safety covered by tests; manual/blocked: Windows UI/removal |
| W9 | Run the benchmark harness with a fixed audio/reference pair | Capture startup, inference, real-time factor, peak working set, transcript, WER, engine/model, and hardware JSON | Harness validation covered; manual/blocked: approved Windows host/fixture |
| W10 | With local ASR selected and cloud refinement switch off, run Prompt mode; then explicitly enable it and repeat | Opt-out run records no refinement provider/model and sends no transcript to cloud; opt-in run records the selected cloud refinement only after the switch is enabled | Source regression covered; manual/blocked: packaged UI/cloud evidence |

The harness commands for W4 and W9 are:

```powershell
$transcription = & $python scripts\local_asr_harness.py --root $assetRoot transcribe `
    --file C:\path\to\fixture.wav --language en > $evidence\offline-transcript.json
if ($LASTEXITCODE -ne 0) { throw "Local-ASR transcription failed with exit code $LASTEXITCODE" }
$benchmark = & $python scripts\local_asr_harness.py --root $assetRoot benchmark `
    --file C:\path\to\fixture.wav --language en `
    --expected-text "Reference transcript" > $evidence\benchmark.json
if ($LASTEXITCODE -ne 0) { throw "Local-ASR benchmark failed with exit code $LASTEXITCODE" }
```

The harness must be invoked with the `.venv\Scripts\python.exe` prepared by
`build.ps1`; the system `py` launcher is not guaranteed to have the locked
runtime dependencies. The `transcribe` JSON must include non-null
`audio_seconds`, `engine`, and `model` fields in addition to its local-only
transcript. The benchmark JSON must include non-null `startup_seconds`,
`inference_seconds`, `real_time_factor`, and `peak_working_set_bytes`. The
fixture, reference transcript, and WER review are part of the evidence and
must not be replaced by a synthetic unit-test audio buffer.

## Blocker and close rule

This documentation update has no authorized Windows x64 product run, no
packaged Settings session, no versioned audio fixture/reference transcript, and
no approved offline-network or process-profiler setup. Provisioning a Windows
machine, changing host networking, or using production credentials is outside
this task. Therefore W1–W9 remain **manual-only and blocked**; W10 is
source-covered but still **blocked for packaged/manual proof**.

Keep issue #23 open until the attached Windows artifacts make W1–W9 green.
When a row fails, preserve the exact executable SHA, manifest version, OS/CPU,
audio fixture hash, and raw JSON/log output so a follow-up fix can be reviewed
against the same conditions.
