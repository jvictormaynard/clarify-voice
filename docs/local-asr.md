# Local ASR sidecar groundwork

This document describes the isolated groundwork for issue #23. It is not yet a
user-facing provider. The typed provider registry from #16 is now integrated by
an explicit adapter, and the recording lifecycle from #18 now supplies the
in-memory audio snapshot contract. The secure Windows installer/update
contract staged by #32 is now on `master`, but its real signing, provenance,
and VM/manual acceptance gates remain open in #22. Local-ASR default
registration and application integration still wait for the concrete product
download/update and progress conventions; this branch does not claim that a
local sidecar/model is covered by the signed MSI update path.

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
the final installation directory. Install-root mutations use a nonblocking
cross-process lock in a deterministic sibling file: it is acquired before
claim/status and held through staging cleanup and final publication; the root
ownership marker is validated again under that lock before any cleanup. Unix
uses `flock`; Windows uses an OS byte-range lock, both of which are released by
the operating system if the installer crashes. A second installer fails with
a typed retryable error and cannot remove an active staging directory. Once the lock is released, the next owner removes only
direct, conservatively named, non-reparse `.install-*` directories; ambiguous
or cleanup-failing paths are preserved and reported as a typed error rather
than accumulating another staging attempt. Manifest-derived engine, version,
model, and asset filename components use a portable allowlist, relative paths
reject both Windows and POSIX traversal, and the resolved install directory
must remain below the owned asset root.

`status` is read-only and performs no network access. It hashes every installed
runtime file and the model. `remove` terminates only a recorded sidecar PID whose
Windows image path still matches this installation, then removes the complete
installer-owned asset root, including receipts and process metadata. A custom
non-empty directory without the ownership marker is refused during installation
and is never recursively deleted. If Windows cannot determine the recorded
process image or liveness conclusively, cleanup fails closed and preserves the
record for a later attempt.

`LocalASRProviderAdapter` implements the merged typed registry contract around
the narrow `LocalTranscriptionBackend` protocol. It is deliberately not added
to the default registry yet: the current provider UI assumes credentials and
model discovery, while local installation/progress and final product wiring
still need the conventions from #22. The explicit harness installer remains a
source-only acceptance tool with its own pinned asset manifest; it does not
silently inherit or advertise the staged signed-MSI/update contract from #32.

The recording lifecycle from #18 remains authoritative: `RecordingSession`
owns and deletes its unique WAV, then passes the in-memory `audio_bytes`
snapshot through `TranscriptionRequest`. The local adapter forwards that
snapshot to the sidecar and never deletes or claims the recording path. Its
`cancel()` and `shutdown()` methods own only local inference and the sidecar;
they do not stop SoX or finalize a recording session. The standalone harness
can still supply a path, which the manager snapshots before starting inference.

## Runtime and privacy design

Before each sidecar start, the complete installed runtime and model are verified
against the committed manifest. The manager then:

- launches `whisper-server.exe` without a console, bound to `127.0.0.1`;
- refuses to launch it from an elevated Windows process, and also fails closed
  if the current privilege state cannot be determined;
- adds a fresh random request-path prefix and disables environment proxies;
- waits for the sidecar's `/health` response before accepting inference;
- uses CPU-only, no-context transcription and never enables conversion/ffmpeg;
- applies startup and inference timeouts;
- observes cancellation during verification and startup health checks, without
  waiting for the full startup timeout;
- terminates on cancellation or application shutdown without retrying a
  cancelled request, and starts idle expiry only after inference finishes;
- serializes inference calls through one sidecar; per-call cancellation affects
  only that call, while manager cancellation or shutdown also cancels queued
  calls;
- holds the same crash-released, cross-process asset-root lock for the lifetime
  of a healthy sidecar, so another harness cannot terminate an active peer;
- records the owned PID and absolute executable path, validates that path under
  the app-owned asset root, and preserves the record if the live image differs
  or cannot be verified;
- on Windows, retains that record and blocks asset removal unless sidecar
  termination is confirmed;
- restarts the sidecar once after a crash or broken request.

Inference sends the in-memory WAV snapshot only to the random loopback endpoint.
The sidecar does not need a cloud credential and the local path contains no
cloud fallback. When
integrated, switching to a cloud provider must remain an explicit user action;
there must be no silent fallback. Prompt/refinement mode also needs a clear UI
warning if it will send the locally produced transcript text to a cloud model.

The downloaded model and runtime are readable by processes running as the same
Windows user. The local HTTP server is not a general security sandbox; the
loopback bind and unpredictable path reduce accidental exposure but do not
protect against a malicious process already running as that user. Run the
harness from a normal, non-administrator PowerShell; elevated launch is rejected.

## Benchmark and acceptance plan

The harness has a `benchmark` command that reports model startup, inference
latency, real-time factor, peak Windows working set, transcript, and optional
word error rate:

```powershell
py scripts\local_asr_harness.py --root "$env:TEMP\clarify-local-asr" benchmark `
  --file C:\path\to\reference.wav --language en `
  --expected-text "Reference transcript"
```

The benchmark validates the WAV header and duration before starting the sidecar;
invalid or unsupported formats return a structured harness error without paying
the startup/inference cost. Product integration still needs its own audio
format policy.

No Windows benchmark or product acceptance result is claimed by this groundwork
PR. The following remains pending before #23 can be closed. The #32 staged
installer/update contract does not substitute for these local-ASR checks, and
#22 still needs real signed-artifact and clean-VM/manual evidence:

| Check | Required evidence | Current state |
| --- | --- | --- |
| Startup and latency | Windows x64 hardware details, audio duration, startup, inference, RTF | Pending |
| Memory | Peak working set from the Windows sidecar process | Pending |
| Quality | Versioned audio fixture, reference transcript, WER plus transcript review | Pending |
| Offline | Successful second transcription with network disabled after installation | Pending |
| Cancellation | Cancel during inference; no sidecar and no temporary WAV remain | Unit seam covered; product/Windows pending |
| Exit cleanup | Quit during inference; no owned process or temporary WAV remains | Unit seam covered; product/Windows pending |
| Crash recovery | Kill sidecar during inference and observe one bounded restart | Unit covered; Windows pending |
| Removal | Asset root absent after removal | Unit covered; Windows pending |
| Cloud regression | Existing provider contract suite unchanged | Typed adapter unit covered; default registration pending |

Do not mark issue #23 closed until those results, the final UI/provider and
download-progress integration, and the required #22 signing/VM acceptance
evidence are in a reviewed follow-up.
