# Local ASR sidecar groundwork

This document describes the isolated groundwork for issue #23. It is not yet a
user-facing provider. Final application integration must wait for the provider
registry (#16), shared recording/process lifecycle (#18), and download/update
conventions (#22) to land on the default branch.

## Pinned implementation

The first supported target is the official CPU-only Windows x64 build of
`whisper.cpp` v1.9.1 with the multilingual `ggml-small` model. The committed
[`local_asr_manifest.json`](../local_asr_manifest.json) records:

- exact binary and model versions, download and source URLs;
- byte sizes, license identifiers, and SHA-256 digests;
- the upstream MIT notices copied beside an isolated installation;
- an allowlist plus SHA-256 for every runtime file extracted from the ZIP;
- the pre-download disk, memory, platform, and compute expectations.

The model is approximately 466 MiB on disk and upstream documents about 852 MB
of memory for inference. The complete initial download is 495,584,068 bytes.
Allow roughly 510 MB of free disk space. This first target is Windows 10 or
newer on x64, CPU-only; four logical cores are recommended. Older x86-64 CPUs
without the instruction support required by the upstream build are not covered.
The official binary imports the Microsoft Visual C++ runtime, so the x64
Microsoft Visual C++ 2015-2022 Redistributable is also required and must be
reported before download by the future UI.

Nothing imports `local_asr.py` from the product runtime yet. No sidecar, model,
download library, or optional dependency is added to the portable application,
normal startup, or unit-test environment.

## Explicit installation and removal harness

Run the harness with Windows Python and an isolated root while this work is not
integrated:

```powershell
py scripts\local_asr_harness.py --root "$env:TEMP\clarify-local-asr" status
py scripts\local_asr_harness.py --root "$env:TEMP\clarify-local-asr" install
py scripts\local_asr_harness.py --root "$env:TEMP\clarify-local-asr" transcribe `
  --file C:\path\to\audio.wav --language en
py scripts\local_asr_harness.py --root "$env:TEMP\clarify-local-asr" remove
```

`install` displays requirements and asks for confirmation before the first
network request. `--yes` is available only for an already authorized automated
acceptance run. Downloads stream into a staging directory. The runtime archive
and model must match both the published size and SHA-256 before extraction or
installation. Only allowlisted archive members are copied, and every extracted
file is hashed again. A failed or interrupted install leaves no executable in
the final installation directory.

`status` is read-only and performs no network access. It hashes every installed
runtime file and the model. `remove` terminates only a recorded sidecar PID whose
Windows image path still matches this installation, then removes the complete
installer-owned asset root, including receipts and process metadata. A custom
non-empty directory without the ownership marker is refused during installation
and is never recursively deleted.

The future product adapter should use the narrow `LocalTranscriptionBackend`
protocol instead of coupling registry or UI types to the sidecar manager.

## Runtime and privacy design

Before each sidecar start, the complete installed runtime and model are verified
against the committed manifest. The manager then:

- launches `whisper-server.exe` without a console, bound to `127.0.0.1`;
- adds a fresh random request-path prefix and disables environment proxies;
- waits for the sidecar's `/health` response before accepting inference;
- uses CPU-only, no-context transcription and never enables conversion/ffmpeg;
- applies startup and inference timeouts;
- terminates the process on cancellation, application shutdown, or idle expiry;
- records the owned PID and verifies its executable path before stale cleanup;
- restarts the sidecar once after a crash or broken request.

Inference sends the WAV only to the random loopback endpoint. The sidecar does
not need a cloud credential and the local path contains no cloud fallback. When
integrated, switching to a cloud provider must remain an explicit user action;
there must be no silent fallback. Prompt/refinement mode also needs a clear UI
warning if it will send the locally produced transcript text to a cloud model.

The downloaded model and runtime are readable by processes running as the same
Windows user. The local HTTP server is not a general security sandbox; the
loopback bind and unpredictable path reduce accidental exposure but do not
protect against a malicious process already running as that user. The upstream
server warning against privileged execution still applies.

## Benchmark and acceptance plan

The harness has a `benchmark` command that reports model startup, inference
latency, real-time factor, peak Windows working set, transcript, and optional
word error rate:

```powershell
py scripts\local_asr_harness.py --root "$env:TEMP\clarify-local-asr" benchmark `
  --file C:\path\to\reference.wav --language en `
  --expected-text "Reference transcript"
```

No Windows benchmark or product acceptance result is claimed by this groundwork
PR. The following remains pending after #16, #18, and #22 merge:

| Check | Required evidence | Current state |
| --- | --- | --- |
| Startup and latency | Windows x64 hardware details, audio duration, startup, inference, RTF | Pending |
| Memory | Peak working set from the Windows sidecar process | Pending |
| Quality | Versioned audio fixture, reference transcript, WER plus transcript review | Pending |
| Offline | Successful second transcription with network disabled after installation | Pending |
| Cancellation | Cancel during inference; no sidecar and no temporary WAV remain | Pending integration |
| Exit cleanup | Quit during inference; no owned process or temporary WAV remains | Pending integration |
| Crash recovery | Kill sidecar during inference and observe one bounded restart | Unit covered; Windows pending |
| Removal | Asset root absent after removal | Unit covered; Windows pending |
| Cloud regression | Existing provider contract suite unchanged | Pending registry integration |

Do not mark issue #23 closed until those results and the final UI/provider and
shared-lifecycle integration are in a reviewed follow-up.
