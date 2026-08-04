"""Bounded local audio-file transcription orchestration.

This module is deliberately independent from Tk and the desktop application.
It provides the narrow seam a future file-picker/drag-and-drop surface can use
without creating a second provider implementation.  Imported files remain
owned by the caller.  Non-WAV inputs are normalized through an injected
converter (SoX in the packaged application) into a short-lived temporary WAV;
the temporary directory is always removed after the typed provider call.

The service only accepts local paths.  It never resolves URLs or starts an
unbounded queue.  At most ``max_workers`` provider operations are submitted at
once, and cancellation stops new work while propagating a token to active
providers/converters.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import Enum
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from itertools import islice
from typing import Any, Protocol

from provider_http import (
    CancellationToken,
    NetworkError,
    QuotaError,
    ProviderCancelledError,
    ProviderTimeoutError,
    RateLimitError,
    ServiceUnavailableError,
)
from provider_types import (
    ProviderConnection,
    TranscriptionRequest,
    TranscriptionResult,
)


# SoX 14.4.2 is bundled with FLAC, Ogg Vorbis, and WavPack support.  MP3/AAC
# codecs are optional DLLs and are intentionally not advertised by the first
# import surface.  A later release can expand this allowlist only after the
# packaged runtime has a deterministic codec acceptance test.
SUPPORTED_AUDIO_EXTENSIONS = frozenset({
    ".wav",
    ".aif",
    ".aiff",
    ".au",
    ".flac",
    ".oga",
    ".ogg",
    ".wv",
})
CANONICAL_AUDIO_EXTENSION = ".wav"
DEFAULT_MAX_WORKERS = 2
MAX_MAX_WORKERS = 4
DEFAULT_MAX_FILES = 64
MAX_MAX_FILES = 256
DEFAULT_MAX_ATTEMPTS = 1
MAX_MAX_ATTEMPTS = 3
DEFAULT_MAX_AUDIO_BYTES = 256 * 1024 * 1024
MAX_MAX_AUDIO_BYTES = 1024 * 1024 * 1024
_SNAPSHOT_CHUNK_BYTES = 1024 * 1024


class AudioBatchError(RuntimeError):
    """Base class for deterministic, user-facing batch failures."""


class AudioBatchConfigurationError(AudioBatchError):
    """Raised before work starts when the selection or limits are invalid."""


class UnsupportedAudioFormatError(AudioBatchError):
    """Raised when a local path uses a codec outside the packaged allowlist."""


class AudioFileValidationError(AudioBatchError):
    """Raised when an imported path is not a usable local file."""


class AudioConversionError(AudioBatchError):
    """Raised when a non-WAV file cannot be normalized locally."""


class AudioBatchCancelledError(AudioBatchError):
    """Raised internally when a conversion/provider operation is cancelled."""


class RetryableAudioBatchError(AudioBatchError):
    """Optional marker for a fake or future gateway's safe retryable error."""


class AudioFileStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class FileTranscriptionSelection:
    """Immutable route chosen before a batch is started.

    ``connection`` is a typed provider connection.  For local ASR it may be
    empty; for cloud providers it is supplied by the caller's secure settings
    boundary and is never copied into a result or log record.
    """

    provider_id: str
    model: str
    language: str
    mode: str = "transcription"
    connection: ProviderConnection = field(
        default_factory=lambda: ProviderConnection("", ""))
    instruction: str = ""
    prompt: str = ""
    temperature: float = 0.0

    def validate(self) -> None:
        provider = self.provider_id.strip().lower()
        model = self.model.strip()
        language = self.language.strip()
        mode = self.mode.strip().lower()
        if not provider:
            raise AudioBatchConfigurationError("A transcription provider is required")
        if not model:
            raise AudioBatchConfigurationError("A transcription model is required")
        if not language:
            raise AudioBatchConfigurationError("A transcription language is required")
        if mode not in {"transcription", "prompt"}:
            raise AudioBatchConfigurationError(
                "Transcription mode must be 'transcription' or 'prompt'")
        if not 0.0 <= float(self.temperature) <= 2.0:
            raise AudioBatchConfigurationError(
                "Transcription temperature must be between 0 and 2")

    @property
    def normalized_provider(self) -> str:
        return self.provider_id.strip().lower()

    @property
    def normalized_mode(self) -> str:
        return self.mode.strip().lower()


class AudioTranscriptionGateway(Protocol):
    """Typed provider seam consumed by :class:`AudioFileBatchService`."""

    def transcribe(
        self,
        request: TranscriptionRequest,
        selection: FileTranscriptionSelection,
        cancel_token: CancellationToken,
    ) -> TranscriptionResult:
        ...


class AudioFileConverter(Protocol):
    """Local conversion seam; implementations must never mutate the source."""

    def convert(
        self,
        source: Path,
        destination: Path,
        cancel_token: CancellationToken,
    ) -> Path:
        ...


class RegistryAudioTranscriptionGateway:
    """Adapt the authoritative typed provider registry to this service."""

    def __init__(self, registry: Any):
        self.registry = registry

    def transcribe(
        self,
        request: TranscriptionRequest,
        selection: FileTranscriptionSelection,
        cancel_token: CancellationToken,
    ) -> TranscriptionResult:
        return self.registry.transcribe(
            selection.normalized_provider,
            request,
            selection.connection,
            cancel_token,
        )


def _default_sox_path() -> str:
    """Resolve the bundled SoX executable, with PATH as the source fallback."""

    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    bundled = root / "extra" / "sox-14.4.2" / "sox.exe"
    if bundled.exists():
        return str(bundled)
    return "sox"


class SoxAudioConverter:
    """Normalize a supported local file to 16 kHz mono signed PCM WAV."""

    def __init__(self, executable: str | os.PathLike[str] | None = None,
            *, timeout_seconds: float = 120.0,
            popen: Callable[..., subprocess.Popen] | None = None):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.executable = str(executable or _default_sox_path())
        self.timeout_seconds = float(timeout_seconds)
        self._popen = popen or subprocess.Popen

    def convert(
        self,
        source: Path,
        destination: Path,
        cancel_token: CancellationToken,
    ) -> Path:
        source = Path(source)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if cancel_token.cancelled:
            raise AudioBatchCancelledError("Audio conversion was cancelled")
        args = [
            self.executable,
            str(source),
            "-r", "16000",
            "-c", "1",
            "-b", "16",
            "-e", "signed-integer",
            str(destination),
        ]
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000
            executable_path = Path(self.executable)
            if executable_path.parent != Path("."):
                kwargs["cwd"] = str(executable_path.parent)
        try:
            process = self._popen(args, **kwargs)
        except OSError as error:
            raise AudioConversionError(
                f"Could not start local audio conversion: {error}") from error

        started = time.monotonic()
        try:
            while process.poll() is None:
                if cancel_token.cancelled:
                    _terminate_process(process)
                    raise AudioBatchCancelledError("Audio conversion was cancelled")
                if time.monotonic() - started >= self.timeout_seconds:
                    _terminate_process(process)
                    raise AudioConversionError(
                        "Audio conversion exceeded its time limit")
                time.sleep(0.02)
            stdout, stderr = process.communicate()
        except AudioBatchError:
            raise
        except OSError as error:
            _terminate_process(process)
            raise AudioConversionError(
                f"Audio conversion could not be read: {error}") from error
        if process.returncode != 0:
            detail = (stderr or stdout or b"").decode(errors="replace").strip()
            raise AudioConversionError(
                f"Audio conversion failed{': ' + detail if detail else ''}")
        if cancel_token.cancelled:
            raise AudioBatchCancelledError("Audio conversion was cancelled")
        if not destination.is_file() or destination.stat().st_size == 0:
            raise AudioConversionError("Audio conversion produced no WAV output")
        return destination


def _terminate_process(process: Any) -> None:
    """Terminate a conversion process without masking the original failure."""

    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass
        try:
            # ``kill`` is asynchronous on Windows.  The temporary directory
            # must not be removed until the child has actually detached from
            # the normalized WAV.
            process.wait()
        except OSError:
            pass


def _cancelled_error(error: BaseException) -> bool:
    return isinstance(error, (AudioBatchCancelledError, ProviderCancelledError))


def _retryable_error(error: BaseException) -> bool:
    # ProviderHttpClient already owns the safe request retry policy.  These
    # classes represent transient failures where an explicit batch attempt is
    # still useful; request bodies are reconstructed from the immutable bytes
    # snapshot, and callers can keep max_attempts at one when charges must not
    # be repeated.
    if isinstance(error, QuotaError):
        # QuotaError is deliberately a RateLimitError subtype in the shared
        # HTTP policy, but exhausted quota is permanent and must not be retried.
        return False
    return isinstance(error, (
        RetryableAudioBatchError,
        NetworkError,
        ProviderTimeoutError,
        ServiceUnavailableError,
        RateLimitError,
    ))


def _selection_request(
        path: Path, audio_bytes: bytes, selection: FileTranscriptionSelection
        ) -> TranscriptionRequest:
    mode = selection.normalized_mode
    language = selection.language.strip()
    instruction = selection.instruction.strip() or (
        "Transcribe the audio accurately."
        if mode == "transcription" else
        "Transcribe the audio and format the result clearly."
    )
    prompt = selection.prompt.strip() or (
        "Transcribe this audio."
        if mode == "transcription" else
        "Transcribe and rewrite this audio for clarity."
    )
    return TranscriptionRequest(
        audio_path=path,
        model=selection.model.strip(),
        language=language,
        instruction=instruction,
        prompt=prompt,
        temperature=float(selection.temperature),
        audio_bytes=audio_bytes,
    )


def validate_audio_path(
        path: str | os.PathLike[str] | Path,
        *,
        max_bytes: int | None = None,
        ) -> Path:
    """Resolve and validate one local import without opening or mutating it."""

    try:
        raw = os.fspath(path)
    except TypeError as error:
        raise AudioFileValidationError("Audio import must be a local path") from error
    if isinstance(raw, bytes):
        raw = os.fsdecode(raw)
    value = str(raw)
    if not value.strip():
        raise AudioFileValidationError("Audio import must be a local path")
    if "://" in value:
        raise AudioFileValidationError(
            "Only local audio files are supported; URL downloads are disabled")
    try:
        candidate = Path(value).expanduser()
        resolved = candidate.resolve(strict=False)
        exists = resolved.exists()
        is_file = resolved.is_file()
    except (OSError, RuntimeError, ValueError) as error:
        raise AudioFileValidationError(
            f"Could not resolve audio file: {value}") from error
    if not exists or not is_file:
        raise AudioFileValidationError(f"Audio file does not exist: {value}")
    extension = resolved.suffix.casefold()
    if extension not in SUPPORTED_AUDIO_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_AUDIO_EXTENSIONS))
        raise UnsupportedAudioFormatError(
            f"Unsupported audio format '{extension or '[none]'}'; supported: {supported}")
    try:
        size = resolved.stat().st_size
        if size <= 0:
            raise AudioFileValidationError(
                f"Audio file is empty: {resolved.name}")
        if max_bytes is not None and size > max_bytes:
            raise AudioFileValidationError(
                f"Audio file exceeds the {max_bytes} byte limit: "
                f"{resolved.name}")
    except (OSError, ValueError) as error:
        raise AudioFileValidationError(
            f"Could not inspect audio file: {resolved.name}") from error
    return resolved


@dataclass(frozen=True)
class AudioFileResult:
    path: Path
    status: AudioFileStatus
    text: str = ""
    provider_id: str = ""
    model: str = ""
    attempts: int = 0
    error: str = ""


@dataclass(frozen=True)
class AudioBatchResult:
    files: tuple[AudioFileResult, ...]
    cancelled: bool = False

    @property
    def succeeded(self) -> tuple[AudioFileResult, ...]:
        return tuple(item for item in self.files
                     if item.status is AudioFileStatus.SUCCEEDED)

    @property
    def failed(self) -> tuple[AudioFileResult, ...]:
        return tuple(item for item in self.files
                     if item.status is AudioFileStatus.FAILED)

    @property
    def cancelled_files(self) -> tuple[AudioFileResult, ...]:
        return tuple(item for item in self.files
                     if item.status is AudioFileStatus.CANCELLED)


@dataclass(frozen=True)
class _PreparedAudio:
    """One immutable request snapshot shared by all provider attempts."""

    request_path: Path
    audio_bytes: bytes


ProgressCallback = Callable[[AudioFileResult], None]


class AudioBatchJob:
    """One bounded batch, optionally run from a UI-owned background thread."""

    def __init__(
        self,
        service: "AudioFileBatchService",
        paths: Sequence[Path],
        selection: FileTranscriptionSelection,
        callback: ProgressCallback | None,
    ):
        self._service = service
        self._paths = tuple(paths)
        self._selection = selection
        self._callback = callback
        self._cancel_token = CancellationToken()
        self._active_tokens: dict[int, CancellationToken] = {}
        self._lock = threading.RLock()
        self._done = threading.Event()
        self._results: list[AudioFileResult] = [
            AudioFileResult(path, AudioFileStatus.PENDING) for path in self._paths
        ]
        self._result: AudioBatchResult | None = None
        self._thread = threading.Thread(
            target=self._run, name="ClarifyVoiceAudioBatch", daemon=True)

    def start(self) -> "AudioBatchJob":
        # Invalid imports are published before the worker starts so a future
        # file-picker can render a deterministic rejection immediately.
        for index, item in enumerate(self._results):
            if item.status is AudioFileStatus.FAILED:
                self._publish(index, item)
        self._thread.start()
        return self

    def cancel(self) -> None:
        """Stop new submissions and cooperatively cancel active operations."""

        callbacks: list[AudioFileResult] = []
        with self._lock:
            # Cancellation and pending/active state transitions are one
            # linearizable operation.  A terminal success publication either
            # acquires this lock first, or observes the cancellation token
            # after this block and is converted to ``CANCELLED``.
            self._cancel_token.cancel()
            tokens = tuple(self._active_tokens.values())
            pending = tuple(
                index for index, item in enumerate(self._results)
                if item.status is AudioFileStatus.PENDING
            )
            for index in pending:
                item = replace(
                    self._results[index],
                    status=AudioFileStatus.CANCELLED,
                    error="Batch cancelled before processing",
                )
                self._results[index] = item
                callbacks.append(item)
            for token in tokens:
                token.cancel()
        for item in callbacks:
            self._notify(item)

    @property
    def done(self) -> bool:
        return self._done.is_set()

    @property
    def result(self) -> AudioBatchResult | None:
        with self._lock:
            return self._result

    def wait(self, timeout: float | None = None) -> AudioBatchResult:
        if not self._done.wait(timeout):
            raise TimeoutError("Audio batch has not completed")
        assert self._result is not None
        return self._result

    def _publish(
        self,
        index: int,
        item: AudioFileResult,
        *,
        active_token: CancellationToken | None = None,
    ) -> None:
        with self._lock:
            if (active_token is not None
                    and item.status is AudioFileStatus.SUCCEEDED
                    and (active_token.cancelled or self._cancel_token.cancelled)):
                item = replace(
                    item,
                    status=AudioFileStatus.CANCELLED,
                    text="",
                    provider_id="",
                    model="",
                    error="File processing was cancelled",
                )
            self._results[index] = item
            if active_token is not None:
                self._active_tokens.pop(index, None)
        self._notify(item)

    def _notify(self, item: AudioFileResult) -> None:
        callback = self._callback
        if callback is not None:
            try:
                callback(item)
            except Exception:
                # UI listeners are observers; they must not kill the worker or
                # skip cleanup for a provider operation.
                pass

    def _run(self) -> None:
        # Keep only max_workers futures submitted at a time.  This avoids the
        # executor becoming a hidden unbounded queue for a dropped folder.
        executor = ThreadPoolExecutor(
            max_workers=self._service.max_workers,
            thread_name_prefix="ClarifyVoiceAudioFile",
        )
        pending = [
            index for index, item in enumerate(self._results)
            if item.status is AudioFileStatus.PENDING
        ]
        cursor = 0
        active: dict[Future[AudioFileResult], tuple[int, CancellationToken]] = {}
        try:
            while active or cursor < len(pending):
                while (not self._cancel_token.cancelled
                       and len(active) < self._service.max_workers
                       and cursor < len(pending)):
                    index = pending[cursor]
                    cursor += 1
                    claimed = self._claim_processing(index)
                    if claimed is None:
                        continue
                    token, processing = claimed
                    self._notify(processing)
                    active[executor.submit(
                        self._process_one, index, token)] = (index, token)
                if not active:
                    break
                completed, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in completed:
                    index, token = active[future]
                    try:
                        item = future.result()
                    except BaseException as error:
                        item = self._failure_result(index, error, token)
                    self._publish(index, item, active_token=token)
                    active.pop(future, None)
            # A cancellation can be observed between the final submission and
            # this loop.  Mark any never-submitted records explicitly.
            if self._cancel_token.cancelled:
                for index, item in enumerate(self._results):
                    if item.status is AudioFileStatus.PENDING:
                        self._publish(index, replace(
                            item, status=AudioFileStatus.CANCELLED,
                            error="Batch cancelled before processing"))
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            with self._lock:
                self._result = AudioBatchResult(
                    tuple(self._results), self._cancel_token.cancelled)
                self._done.set()

    def _claim_processing(
        self, index: int
    ) -> tuple[CancellationToken, AudioFileResult] | None:
        """Atomically claim a pending entry for a worker submission."""

        with self._lock:
            if (self._cancel_token.cancelled
                    or self._results[index].status is not AudioFileStatus.PENDING):
                return None
            token = CancellationToken()
            processing = replace(
                self._results[index],
                status=AudioFileStatus.PROCESSING,
                error="",
            )
            self._active_tokens[index] = token
            self._results[index] = processing
            return token, processing

    def _failure_result(
        self, index: int, error: BaseException, token: CancellationToken
    ) -> AudioFileResult:
        current = self._results[index]
        if token.cancelled or self._cancel_token.cancelled or _cancelled_error(error):
            return replace(
                current,
                status=AudioFileStatus.CANCELLED,
                error="File processing was cancelled",
            )
        return replace(current, status=AudioFileStatus.FAILED, error=str(error))

    def _process_one(self, index: int, token: CancellationToken) -> AudioFileResult:
        path = self._paths[index]
        current = self._results[index]
        attempts = 0
        last_error: BaseException | None = None
        try:
            # Normalize and snapshot once.  Every provider attempt below uses
            # these same bytes, even if the caller mutates or removes the
            # original path after a transient failure.
            with self._service._prepare_audio(path, token) as prepared:
                while attempts < self._service.max_attempts:
                    attempts += 1
                    if token.cancelled or self._cancel_token.cancelled:
                        return replace(current, status=AudioFileStatus.CANCELLED,
                                       attempts=attempts - 1,
                                       error="File processing was cancelled")
                    try:
                        result = self._service._transcribe_prepared(
                            prepared, self._selection, token)
                        # A provider may finish concurrently with
                        # ``job.cancel()`` after its final token check.  Do
                        # not publish that result as successful once
                        # cancellation has won the batch boundary.
                        if token.cancelled or self._cancel_token.cancelled:
                            return replace(
                                current, status=AudioFileStatus.CANCELLED,
                                attempts=attempts,
                                error="File processing was cancelled")
                        return AudioFileResult(
                            path=path,
                            status=AudioFileStatus.SUCCEEDED,
                            text=result.text,
                            provider_id=result.provider_id,
                            model=result.model,
                            attempts=attempts,
                        )
                    except BaseException as error:
                        last_error = error
                        if (token.cancelled or self._cancel_token.cancelled
                                or _cancelled_error(error)):
                            return replace(
                                current, status=AudioFileStatus.CANCELLED,
                                attempts=attempts,
                                error="File processing was cancelled")
                        if (attempts >= self._service.max_attempts
                                or not _retryable_error(error)):
                            break
        except BaseException as error:
            last_error = error
            if (token.cancelled or self._cancel_token.cancelled
                    or _cancelled_error(error)):
                return replace(current, status=AudioFileStatus.CANCELLED,
                               attempts=attempts,
                               error="File processing was cancelled")
        return replace(current, status=AudioFileStatus.FAILED,
                       attempts=attempts, error=str(last_error or "Unknown error"))


class AudioFileBatchService:
    """Validate, normalize, and transcribe a finite local file batch."""

    def __init__(
        self,
        gateway: AudioTranscriptionGateway,
        converter: AudioFileConverter | None = None,
        *,
        max_workers: int = DEFAULT_MAX_WORKERS,
        max_files: int = DEFAULT_MAX_FILES,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        max_audio_bytes: int = DEFAULT_MAX_AUDIO_BYTES,
        temp_root: Path | None = None,
    ):
        if not 1 <= int(max_workers) <= MAX_MAX_WORKERS:
            raise AudioBatchConfigurationError(
                f"max_workers must be between 1 and {MAX_MAX_WORKERS}")
        if not 1 <= int(max_files) <= MAX_MAX_FILES:
            raise AudioBatchConfigurationError(
                f"max_files must be between 1 and {MAX_MAX_FILES}")
        if not 1 <= int(max_attempts) <= MAX_MAX_ATTEMPTS:
            raise AudioBatchConfigurationError(
                f"max_attempts must be between 1 and {MAX_MAX_ATTEMPTS}")
        if not 1 <= int(max_audio_bytes) <= MAX_MAX_AUDIO_BYTES:
            raise AudioBatchConfigurationError(
                "max_audio_bytes must be between 1 byte and 1 GiB")
        self.gateway = gateway
        self.converter = converter or SoxAudioConverter()
        self.max_workers = int(max_workers)
        self.max_files = int(max_files)
        self.max_attempts = int(max_attempts)
        self.max_audio_bytes = int(max_audio_bytes)
        self.temp_root = Path(temp_root) if temp_root is not None else None

    def start(
        self,
        paths: Iterable[str | os.PathLike[str] | Path],
        selection: FileTranscriptionSelection,
        *,
        on_update: ProgressCallback | None = None,
    ) -> AudioBatchJob:
        selection.validate()
        # Materialize at most one item beyond the finite policy limit.  A
        # dropped directory or generator cannot force an unbounded import
        # list into memory before the user sees a clear limit error.
        raw_paths = tuple(islice(paths, self.max_files + 1))
        if not raw_paths:
            raise AudioBatchConfigurationError("At least one local audio file is required")
        if len(raw_paths) > self.max_files:
            raise AudioBatchConfigurationError(
                f"Batch contains {len(raw_paths)} files; maximum is {self.max_files}")
        validated: list[Path] = []
        initial: list[AudioFileResult] = []
        for raw in raw_paths:
            try:
                path = validate_audio_path(raw, max_bytes=self.max_audio_bytes)
            except AudioBatchError as error:
                # Invalid imports remain visible to the caller, while valid
                # files in the same batch continue independently.
                try:
                    path = Path(os.fspath(raw)).expanduser()
                except (TypeError, OSError, RuntimeError, ValueError):
                    path = Path("<invalid>")
                validated.append(path)
                initial.append(AudioFileResult(
                    path=path,
                    status=AudioFileStatus.FAILED,
                    attempts=0,
                    error=str(error),
                ))
            else:
                validated.append(path)
                initial.append(AudioFileResult(path, AudioFileStatus.PENDING))
        job = AudioBatchJob(self, tuple(validated), selection, on_update)
        # Replace initial records before start.  Invalid paths are never
        # submitted, but remain in the ordered result and callback stream.
        job._results = initial
        return job.start()

    def run(
        self,
        paths: Iterable[str | os.PathLike[str] | Path],
        selection: FileTranscriptionSelection,
        *,
        on_update: ProgressCallback | None = None,
    ) -> AudioBatchResult:
        return self.start(paths, selection, on_update=on_update).wait()

    def _transcribe_one(
        self,
        path: Path,
        selection: FileTranscriptionSelection,
        cancel_token: CancellationToken,
    ) -> TranscriptionResult:
        """Transcribe one file for callers that do not need retries."""

        with self._prepare_audio(path, cancel_token) as prepared:
            return self._transcribe_prepared(prepared, selection, cancel_token)

    def _transcribe_prepared(
        self,
        prepared: _PreparedAudio,
        selection: FileTranscriptionSelection,
        cancel_token: CancellationToken,
    ) -> TranscriptionResult:
        if cancel_token.cancelled:
            raise AudioBatchCancelledError("File processing was cancelled")
        return self.gateway.transcribe(
            _selection_request(
                prepared.request_path, prepared.audio_bytes, selection),
            selection,
            cancel_token,
        )

    @contextmanager
    def _prepare_audio(
        self,
        path: Path,
        cancel_token: CancellationToken,
    ) -> Iterator[_PreparedAudio]:
        """Create one bounded immutable snapshot for all provider attempts."""

        if cancel_token.cancelled:
            raise AudioBatchCancelledError("File processing was cancelled")
        # A private directory is used only when conversion is necessary.  No
        # cleanup path ever points at the imported source.
        if path.suffix.casefold() == CANONICAL_AUDIO_EXTENSION:
            yield _PreparedAudio(
                request_path=path,
                audio_bytes=_snapshot_audio(
                    path, cancel_token, max_bytes=self.max_audio_bytes),
            )
            return
        if self.temp_root is not None:
            self.temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
                prefix="clarifyvoice-audio-", dir=str(self.temp_root) if self.temp_root else None
        ) as temporary:
            destination = Path(temporary) / "normalized.wav"
            normalized = self.converter.convert(path, destination, cancel_token)
            normalized = Path(normalized)
            try:
                normalized_resolved = normalized.resolve(strict=True)
                temporary_resolved = Path(temporary).resolve(strict=True)
            except OSError as error:
                raise AudioConversionError(
                    "Audio conversion did not produce a readable temporary WAV") from error
            if (normalized_resolved == temporary_resolved
                    or temporary_resolved not in normalized_resolved.parents
                    or normalized_resolved.suffix.casefold() != CANONICAL_AUDIO_EXTENSION):
                raise AudioConversionError(
                    "Audio converter returned a path outside its temporary directory")
            audio_bytes = _snapshot_audio(
                normalized, cancel_token, max_bytes=self.max_audio_bytes)
            yield _PreparedAudio(
                request_path=normalized,
                audio_bytes=audio_bytes,
            )


def _snapshot_audio(
        path: Path,
        cancel_token: CancellationToken,
        *,
        max_bytes: int = DEFAULT_MAX_AUDIO_BYTES,
        ) -> bytes:
    if cancel_token.cancelled:
        raise AudioBatchCancelledError("File processing was cancelled")
    try:
        chunks: list[bytes] = []
        total = 0
        with Path(path).open("rb") as stream:
            while True:
                if cancel_token.cancelled:
                    raise AudioBatchCancelledError("File processing was cancelled")
                chunk = stream.read(_SNAPSHOT_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise AudioFileValidationError(
                        f"Audio file exceeds the {max_bytes} byte limit: "
                        f"{Path(path).name}")
                chunks.append(chunk)
        audio = b"".join(chunks)
    except OSError as error:
        raise AudioFileValidationError(
            f"Could not read audio file: {path.name}") from error
    if not audio:
        raise AudioFileValidationError(f"Audio file is empty: {path.name}")
    if cancel_token.cancelled:
        raise AudioBatchCancelledError("File processing was cancelled")
    return audio
