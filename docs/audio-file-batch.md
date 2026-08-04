# Local audio-file import and bounded batch transcription

`audio_file_batch.py` is the UI-independent service boundary for a future
drag-and-drop or file-picker surface. It is intentionally a local-file
workflow, not a download manager and not a second provider implementation.

## Route selection

The caller must create one immutable `FileTranscriptionSelection` before the
batch starts. It names the provider ID, model, language, mode (`transcription`
or `prompt`), and typed `ProviderConnection`. Local ASR may use an empty
connection; cloud providers receive the connection resolved by the existing
secure configuration boundary. The selection is passed to the authoritative
`ProviderRegistry` through `RegistryAudioTranscriptionGateway`, so model and
endpoint routing remain in the provider adapters.

```python
from audio_file_batch import (
    AudioFileBatchService,
    FileTranscriptionSelection,
    RegistryAudioTranscriptionGateway,
)
from provider_registry import PROVIDER_REGISTRY
from provider_types import ProviderConnection

selection = FileTranscriptionSelection(
    provider_id="local_asr",
    model="ggml-small",
    language="en",
    connection=ProviderConnection("", ""),
)
service = AudioFileBatchService(
    RegistryAudioTranscriptionGateway(PROVIDER_REGISTRY),
    max_workers=2,
)
job = service.start(paths, selection, on_update=publish_file_state)
result = job.wait()
```

The UI must marshal `on_update` callbacks back to Tk's event loop. The service
does not import Tk and does not perform UI work while a provider call is in
flight.

## Format policy

The first packaged policy accepts the formats that the vendored SoX runtime has
deterministic codecs for: WAV, AIFF, AU, FLAC, Ogg Vorbis (`.ogg`/`.oga`), and
WavPack. The allowlist is case-insensitive. MP3, AAC/M4A, and arbitrary video
containers are rejected with a per-file error because the packaged runtime
does not ship the optional codecs needed to claim those formats.

WAV files are read into an immutable byte snapshot and sent to the typed
provider. Other accepted formats are converted by `SoxAudioConverter` to a
16 kHz, mono, signed-PCM temporary WAV. The source path is never passed to a
cleanup operation and is never deleted or replaced. Conversion output is
required to stay inside the service-owned temporary directory.

## Bounded lifecycle

`AudioFileBatchService` enforces a finite file limit (64 by default, 256 hard
maximum) and a worker limit (2 by default, 4 hard maximum). `AudioBatchJob`
submits no more than `max_workers` futures at once; a dropped folder therefore
cannot become an unbounded executor queue. Each file reports `pending`,
`processing`, `succeeded`, `failed`, or `cancelled` and retains its successful
text/provider/model or an actionable error. A failed file does not hide other
results. Each source snapshot is capped at 256 MiB by default (1 GiB hard
maximum) and is read in 1 MiB chunks with cancellation checks, so a large file
cannot create an unbounded allocation before provider work starts.

`job.cancel()` stops new submissions and cancels active provider/conversion
tokens. Cancellation is cooperative: a provider or converter must return from
its current call before the job can finish. All conversion directories are
removed by the `TemporaryDirectory` context even on cancellation or provider
failure.

Automatic attempts default to one. Callers may set `max_attempts` to at most
three; only explicitly retryable/transient typed errors are retried. The
provider HTTP layer remains responsible for its own safe request retry policy;
permanent quota exhaustion is never retried, and callers should keep one
attempt when a provider operation could be charged again.

The service accepts `Path`-like local values only. URL-looking values are
rejected before any network-capable code is reached. There is no URL download,
meeting capture, diarization, cloud sync, or persistent background queue.

## Acceptance evidence

Deterministic tests in `tests/test_audio_file_batch.py` cover the extension
allowlist, URL rejection, typed registry reuse, partial failures, retry,
bounded concurrency, cooperative cancellation, source preservation, and
temporary conversion cleanup. Packaged Windows acceptance should later add a
real file-picker/drag-and-drop surface, representative fixtures for every
advertised format, and offline local-ASR/cloud endpoint evidence; this issue's
service does not claim that UI evidence.
