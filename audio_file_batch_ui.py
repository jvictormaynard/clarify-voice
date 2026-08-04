"""Presentation/lifecycle seams for ClarifyVoice local audio imports.

The transcription service deliberately does not know about Tk.  This module
keeps the small amount of state needed by a file-picker separate from the
widgets: callbacks still arrive from the service worker, while the caller is
responsible for marshalling them onto its UI loop.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
import os
from pathlib import Path
import threading

from audio_file_batch import (
    AudioBatchJob,
    AudioBatchResult,
    AudioFileBatchService,
    AudioFileResult,
    AudioFileStatus,
    FileTranscriptionSelection,
)
from provider_types import ProviderCapability


@dataclass(frozen=True)
class AudioImportProviderOption:
    """One provider choice that can be shown by the import window."""

    provider_id: str
    display_name: str
    execution: str


def audio_import_provider_options(registry, execution: str) -> tuple[AudioImportProviderOption, ...]:
    """Return transcription-capable providers for the selected route.

    ``local`` is intentionally limited to the installed Local ASR provider;
    every registered cloud provider is grouped under ``cloud``.  The helper
    does not validate credentials or perform model discovery/network I/O.
    """

    normalized_execution = str(execution or "").strip().lower()
    if normalized_execution not in {"local", "cloud"}:
        raise ValueError("Audio import execution must be local or cloud")
    options = []
    for metadata in registry.metadata:
        provider_id = metadata.provider_id.strip().lower()
        provider_execution = "local" if provider_id == "local_asr" else "cloud"
        if provider_execution != normalized_execution:
            continue
        if not metadata.supports(ProviderCapability.AUDIO_TRANSCRIPTION):
            continue
        options.append(AudioImportProviderOption(
            provider_id=provider_id,
            display_name=metadata.display_name,
            execution=provider_execution,
        ))
    return tuple(options)


def deduplicate_audio_paths(
        paths: Iterable[str | os.PathLike[str] | Path],
        ) -> tuple[Path, ...]:
    """Keep a finite picker result ordered while removing duplicate paths."""

    result: list[Path] = []
    seen: set[str] = set()
    for raw in paths:
        try:
            value = os.fspath(raw)
            if isinstance(value, bytes):
                value = os.fsdecode(value)
            path = Path(value)
        except (TypeError, OSError, ValueError):
            # Preserve an invalid value as a path-like marker so the service
            # can publish a per-file validation error instead of hiding it.
            path = Path(str(raw))
        key = os.path.normcase(os.path.abspath(str(path)))
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return tuple(result)


ProgressListener = Callable[[AudioFileResult], None]


class AudioFileImportController:
    """Own one finite import selection and its retryable UI state.

    A retry is a new bounded service job containing only files that failed in
    the previous run.  Existing successful results remain in the snapshot,
    which lets the UI show partial output while failed files are retried with
    the same or a newly selected route.
    """

    def __init__(
            self,
            service: AudioFileBatchService,
            *,
            on_update: ProgressListener | None = None,
            ) -> None:
        self.service = service
        self._on_update = on_update
        self._lock = threading.RLock()
        self._generation = 0
        self._job: AudioBatchJob | None = None
        self._selection: FileTranscriptionSelection | None = None
        self._paths: tuple[Path, ...] = ()
        self._results: dict[Path, AudioFileResult] = {}

    @property
    def job(self) -> AudioBatchJob | None:
        with self._lock:
            return self._job

    @property
    def selection(self) -> FileTranscriptionSelection | None:
        with self._lock:
            return self._selection

    @property
    def paths(self) -> tuple[Path, ...]:
        with self._lock:
            return self._paths

    @property
    def running(self) -> bool:
        job = self.job
        return job is not None and not job.done

    @property
    def done(self) -> bool:
        job = self.job
        return job is not None and job.done

    @property
    def failed_paths(self) -> tuple[Path, ...]:
        return tuple(item.path for item in self.snapshot()
                     if item.status is AudioFileStatus.FAILED)

    def snapshot(self) -> tuple[AudioFileResult, ...]:
        with self._lock:
            return tuple(self._results[path] for path in self._paths)

    @property
    def result(self) -> AudioBatchResult | None:
        with self._lock:
            if self._job is None:
                return None
            return AudioBatchResult(
                self.snapshot(),
                cancelled=bool(self._job.result and self._job.result.cancelled),
            )

    def start(
            self,
            paths: Iterable[str | os.PathLike[str] | Path],
            selection: FileTranscriptionSelection,
            ) -> AudioBatchJob:
        """Start a new selection, replacing any previous result snapshot."""

        selected = deduplicate_audio_paths(paths)
        return self._start(selected, selection, preserve_existing=False)

    def retry_failed(
            self,
            selection: FileTranscriptionSelection | None = None,
            ) -> AudioBatchJob:
        """Retry only failed files and retain successful/other terminal rows."""

        with self._lock:
            if self._job is None:
                raise RuntimeError("No audio import has been started")
            failed = tuple(
                path for path in self._paths
                if self._results[path].status is AudioFileStatus.FAILED
            )
            chosen_selection = selection or self._selection
        if not failed:
            raise ValueError("There are no failed audio files to retry")
        if chosen_selection is None:
            raise RuntimeError("No audio import route is available for retry")
        return self._start(failed, chosen_selection, preserve_existing=True)

    def cancel(self) -> bool:
        """Request cooperative cancellation of the active service job."""

        job = self.job
        if job is None or job.done:
            return False
        job.cancel()
        return True

    def wait(self, timeout: float | None = None) -> AudioBatchResult:
        job = self.job
        if job is None:
            raise RuntimeError("No audio import has been started")
        job.wait(timeout)
        result = self.result
        assert result is not None
        return result

    def _start(
            self,
            selected: tuple[Path, ...],
            selection: FileTranscriptionSelection,
            *,
            preserve_existing: bool,
            ) -> AudioBatchJob:
        if not selected:
            raise ValueError("At least one local audio file is required")
        with self._lock:
            if self._job is not None and not self._job.done:
                raise RuntimeError("An audio import is already running")
            self._generation += 1
            generation = self._generation
            if preserve_existing:
                paths = self._paths
                for path in selected:
                    self._results[path] = AudioFileResult(
                        path, AudioFileStatus.PENDING)
            else:
                paths = selected
                self._paths = paths
                self._results = {
                    path: AudioFileResult(path, AudioFileStatus.PENDING)
                    for path in paths
                }
            self._selection = selection
            pending = tuple(self._results[path] for path in selected)
            self._job = None
        for item in pending:
            self._notify(item)
        try:
            job = self.service.start(
                selected,
                selection,
                on_update=lambda item: self._publish(generation, item),
            )
        except BaseException:
            with self._lock:
                if generation == self._generation:
                    self._job = None
            raise
        with self._lock:
            if generation == self._generation:
                self._job = job
        return job

    def _publish(self, generation: int, item: AudioFileResult) -> None:
        with self._lock:
            if generation != self._generation or item.path not in self._results:
                return
            # A retry cannot allow a late callback from the previous job to
            # overwrite a newer pending/processing state.
            self._results[item.path] = replace(item)
        self._notify(item)

    def _notify(self, item: AudioFileResult) -> None:
        if self._on_update is None:
            return
        try:
            self._on_update(item)
        except Exception:
            # A UI observer must not affect service cleanup or worker state.
            pass


__all__ = [
    "AudioFileImportController",
    "AudioImportProviderOption",
    "audio_import_provider_options",
    "deduplicate_audio_paths",
]
