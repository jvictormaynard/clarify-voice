"""Qt/QML presentation boundary for local audio-file batch imports.

The bounded batch implementation remains in :mod:`audio_file_batch` and its
retry/lifecycle state remains in :mod:`audio_file_batch_ui`.  This module only
adapts that controller to QObject properties, signals, and slots.  In
particular, the service callback never mutates QObject state directly: it is
forwarded through the injected ``call_soon`` boundary first.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
import inspect
import os
from pathlib import Path
import threading
from typing import Any

from PySide6.QtCore import QObject, Property, Signal, Slot

from audio_file_batch import (
    AudioBatchJob,
    AudioFileBatchService,
    AudioFileResult,
    AudioFileStatus,
    FileTranscriptionSelection,
)
from audio_file_batch_ui import AudioFileImportController


PathValue = str | os.PathLike[str] | Path
SelectionFactory = Callable[..., FileTranscriptionSelection]
CallSoon = Callable[[Callable[[], None]], None]
ServiceFactory = Callable[[], AudioFileBatchService]
CopyRunner = Callable[[str], Any]
DispatchRunner = Callable[[Callable[[], None]], None]


def _default_selection_factory(
    provider_id: str,
    model: str,
    language: str,
    mode: str,
) -> FileTranscriptionSelection:
    """Build the simplest typed selection for standalone use and tests.

    Production composition should inject the existing route/configuration
    boundary so cloud credentials and endpoint policy remain outside QML.
    """

    return FileTranscriptionSelection(
        provider_id=provider_id,
        model=model,
        language=language,
        mode=mode,
    )


def _coerce_qml_paths(paths: Iterable[Any] | None) -> tuple[str, ...]:
    """Convert a QML ``QVariantList`` to path strings without deduplicating.

    Deduplication belongs to ``AudioFileImportController.start``.  Keeping the
    values ordered here lets the existing controller remain the single owner
    of duplicate-path semantics.
    """

    if paths is None:
        return ()
    if isinstance(paths, (str, bytes, os.PathLike)):
        values = (paths,)
    else:
        values = tuple(paths)
    result: list[str] = []
    for value in values:
        try:
            raw = os.fspath(value)
            if isinstance(raw, bytes):
                raw = os.fsdecode(raw)
        except (TypeError, OSError, ValueError):
            raw = str(value)
        result.append(str(raw))
    return tuple(result)


def _path_text(path: PathValue) -> str:
    try:
        return os.fsdecode(os.fspath(path))
    except (TypeError, OSError, ValueError):
        return str(path)


class QmlAudioFileImportController(QObject):
    """Expose one injectable audio-file import controller to QML.

    ``service`` or ``service_factory`` supplies the existing
    :class:`AudioFileBatchService`.  ``selection_factory`` receives
    ``(provider_id, model, language, mode)`` and must return the typed
    :class:`FileTranscriptionSelection` used by that service.  ``scheduler``
    may be an object exposing ``call_soon`` (such as ``QtWorkflowScheduler``),
    while ``call_soon`` is a direct seam useful for tests.
    """

    selectedFilesChanged = Signal()
    filesChanged = Signal()
    resultsChanged = Signal()
    runningChanged = Signal()
    doneChanged = Signal()
    failedCountChanged = Signal()
    canRetryChanged = Signal()
    lastErrorChanged = Signal()
    copyCompleted = Signal(str, bool)

    def __init__(
        self,
        service: AudioFileBatchService | None = None,
        *,
        service_factory: ServiceFactory | None = None,
        selection_factory: SelectionFactory | None = None,
        scheduler: Any | None = None,
        call_soon: CallSoon | None = None,
        copy_runner: CopyRunner | None = None,
        dispatch_runner: DispatchRunner | None = None,
        controller_factory: Callable[..., AudioFileImportController] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if service is None:
            if service_factory is None:
                raise ValueError(
                    "An audio batch service or service factory is required"
                )
            service = service_factory()
        if call_soon is None and scheduler is not None:
            call_soon = getattr(scheduler, "call_soon", scheduler)
        if not callable(call_soon):
            raise ValueError("A Qt scheduler with call_soon is required")
        self._call_soon = call_soon
        if dispatch_runner is None and scheduler is not None:
            dispatch_runner = getattr(scheduler, "run_dispatch", None)
        self._dispatch_runner = dispatch_runner or self._call_soon
        self._copy_runner = copy_runner
        self._selection_factory = selection_factory or _default_selection_factory
        factory = controller_factory or AudioFileImportController
        self._controller = factory(service, on_update=self._on_batch_update)

        self._lock = threading.RLock()
        self._selected_paths: tuple[str, ...] = ()
        self._items: tuple[AudioFileResult, ...] = ()
        self._running = False
        self._done = False
        self._failed_count = 0
        self._can_retry = False
        self._last_error = ""
        self._job: AudioBatchJob | None = None
        self._job_generation = 0

    @Property("QVariantList", notify=selectedFilesChanged)
    def selectedFiles(self) -> list[str]:
        with self._lock:
            return list(self._selected_paths)

    @Property("QVariantList", notify=filesChanged)
    def files(self) -> list[dict[str, Any]]:
        return self._file_maps()

    @Property("QVariantList", notify=resultsChanged)
    def results(self) -> list[dict[str, Any]]:
        return self._file_maps()

    @Property("QVariantList", notify=resultsChanged)
    def fileResults(self) -> list[dict[str, Any]]:
        return self._file_maps()

    @Property(bool, notify=runningChanged)
    def running(self) -> bool:
        with self._lock:
            return self._running

    @Property(bool, notify=doneChanged)
    def done(self) -> bool:
        with self._lock:
            return self._done

    @Property(int, notify=failedCountChanged)
    def failedCount(self) -> int:
        with self._lock:
            return self._failed_count

    @Property(bool, notify=canRetryChanged)
    def canRetry(self) -> bool:
        with self._lock:
            return self._can_retry

    @Property(str, notify=lastErrorChanged)
    def lastError(self) -> str:
        with self._lock:
            return self._last_error

    @Slot("QVariantList", result=bool)
    def setSelectedFiles(self, paths: Iterable[Any] | None) -> bool:
        """Replace the pending QML selection without starting a batch."""

        selected = _coerce_qml_paths(paths)
        with self._lock:
            if self._running:
                self._set_last_error_locked("An audio import is already running")
                return False
            self._selected_paths = selected
            self._items = ()
            self._running = False
            self._done = False
            self._failed_count = 0
            self._can_retry = False
            self._last_error = ""
        self._emit_state_changes(
            selected=True,
            files=True,
            results=True,
            running=True,
            done=True,
            failed=True,
            retry=True,
            error=True,
        )
        return True

    @Slot("QVariantList", str, str, str, result=bool)
    @Slot("QVariantList", str, str, str, str, result=bool)
    def start(
        self,
        paths: Iterable[Any] | None,
        provider_id: str,
        model: str,
        language: str,
        mode: str = "transcription",
    ) -> bool:
        """Select local paths, create a typed route, and start the batch."""

        if not self.setSelectedFiles(paths):
            return False
        return self.startSelected(provider_id, model, language, mode)

    @Slot(str, str, str, result=bool)
    @Slot(str, str, str, str, result=bool)
    def startSelected(
        self,
        provider_id: str,
        model: str,
        language: str,
        mode: str = "transcription",
    ) -> bool:
        """Start the current selection using the injected route factory."""

        with self._lock:
            paths = self._selected_paths
            if self._running:
                self._set_last_error_locked("An audio import is already running")
                return False
        self._clear_last_error()
        if not paths:
            self._set_last_error("At least one local audio file is required")
            return False
        try:
            selection = self._make_selection(
                provider_id,
                model,
                language,
                mode,
            )
            if not isinstance(selection, FileTranscriptionSelection):
                raise TypeError(
                    "selection_factory must return FileTranscriptionSelection"
                )
            job = self._controller.start(paths, selection)
        except BaseException as error:
            self._set_last_error(str(error))
            return False

        with self._lock:
            self._job_generation += 1
            generation = self._job_generation
            self._job = job
        self._refresh_from_controller()
        self._watch_job(job, generation)
        return True

    @Slot(result=bool)
    def cancel(self) -> bool:
        """Request cooperative cancellation of the active batch."""

        try:
            cancelled = bool(self._controller.cancel())
        except BaseException as error:
            self._set_last_error(str(error))
            return False
        self._refresh_from_controller()
        return cancelled

    @Slot(result=bool)
    @Slot(str, str, str, result=bool)
    @Slot(str, str, str, str, result=bool)
    def retryFailed(
        self,
        provider_id: str = "",
        model: str = "",
        language: str = "",
        mode: str = "",
    ) -> bool:
        """Retry only failed files through the existing import controller."""

        with self._lock:
            if self._running:
                self._set_last_error_locked("An audio import is already running")
                return False
            previous = self._controller.selection
            paths = self._selected_paths
        self._clear_last_error()
        if not paths:
            self._set_last_error("No audio import has been started")
            return False

        try:
            selection = None
            values = (provider_id, model, language, mode)
            if any(str(value or "").strip() for value in values):
                if previous is None:
                    selection = self._make_selection(*values)
                else:
                    selection = self._make_selection(
                        provider_id or previous.provider_id,
                        model or previous.model,
                        language or previous.language,
                        mode or previous.mode,
                    )
                if not isinstance(selection, FileTranscriptionSelection):
                    raise TypeError(
                        "selection_factory must return FileTranscriptionSelection"
                    )
            job = self._controller.retry_failed(selection)
        except BaseException as error:
            self._set_last_error(str(error))
            return False

        with self._lock:
            self._job_generation += 1
            generation = self._job_generation
            self._job = job
        self._refresh_from_controller()
        self._watch_job(job, generation)
        return True

    @Slot(result=bool)
    def retry(self) -> bool:
        """QML-friendly alias for retrying with the previous typed selection."""

        return self.retryFailed()

    @Slot(str, result=str)
    def statusFor(self, path: str) -> str:
        item = self._item_for(path)
        return item.status.value if item is not None else ""

    @Slot(str, result=str)
    def textFor(self, path: str) -> str:
        item = self._item_for(path)
        return item.text if item is not None else ""

    @Slot(str, result=str)
    def errorFor(self, path: str) -> str:
        item = self._item_for(path)
        return item.error if item is not None else ""

    @Slot(str, result=bool)
    def copyFile(self, path: str) -> bool:
        """Copy one completed transcript through the injected runtime seam."""

        item = self._item_for(path)
        if (
            item is None
            or item.status is not AudioFileStatus.SUCCEEDED
            or not item.text
        ):
            self._set_last_error("That file has no completed transcript to copy")
            return False
        if self._copy_runner is None:
            self._set_last_error("Transcript copying is unavailable")
            return False

        copied_text = item.text

        def copy() -> None:
            try:
                self._copy_runner(copied_text)
            except Exception as error:
                message = str(error)
                self._call_soon(
                    lambda message=message: self._finish_copy(path, False, message)
                )
            else:
                self._call_soon(lambda: self._finish_copy(path, True, ""))

        try:
            self._dispatch_runner(copy)
        except Exception as error:
            self._set_last_error(str(error))
            return False
        return True

    def _make_selection(
        self,
        provider_id: str,
        model: str,
        language: str,
        mode: str,
    ) -> FileTranscriptionSelection:
        factory = self._selection_factory
        # A four-argument factory is the public seam.  Supporting a compact
        # three-argument factory keeps simple QML tests readable without
        # catching TypeErrors raised inside a factory implementation.
        try:
            signature = inspect.signature(factory)
        except (TypeError, ValueError):
            return factory(provider_id, model, language, mode)
        try:
            signature.bind(provider_id, model, language, mode)
        except TypeError:
            return factory(provider_id, model, language)
        return factory(provider_id, model, language, mode)

    def _on_batch_update(self, _item: AudioFileResult) -> None:
        """Receive a service callback and schedule all QObject work on Qt."""

        self._call_soon(self._refresh_from_controller)

    def _watch_job(self, job: AudioBatchJob, generation: int) -> None:
        wait = getattr(job, "wait", None)
        if not callable(wait):
            return

        def wait_for_completion() -> None:
            try:
                wait()
            except BaseException as error:
                message = str(error)
                self._call_soon(
                    lambda message=message: self._finish_job(generation, message)
                )
            else:
                self._call_soon(lambda: self._finish_job(generation, ""))

        threading.Thread(
            target=wait_for_completion,
            name="ClarifyVoiceQmlAudioBatch",
            daemon=True,
        ).start()

    @Slot(int, str)
    def _finish_job(self, generation: int, error: str = "") -> None:
        with self._lock:
            if generation != self._job_generation:
                return
        if error:
            self._set_last_error(error)
        self._refresh_from_controller()

    @Slot(str, bool, str)
    def _finish_copy(self, path: str, success: bool, error: str) -> None:
        if error:
            self._set_last_error(error)
        self.copyCompleted.emit(path, success)

    @Slot()
    def _refresh_from_controller(self) -> None:
        """Copy the UI snapshot only from the scheduler/Qt side."""

        try:
            items = tuple(self._controller.snapshot())
            paths = tuple(_path_text(path) for path in self._controller.paths)
            running = bool(self._controller.running)
            done = bool(self._controller.done)
        except BaseException as error:
            self._set_last_error(str(error))
            return

        failed_count = sum(item.status is AudioFileStatus.FAILED for item in items)
        can_retry = done and failed_count > 0
        batch_error = next(
            (item.error for item in reversed(items) if item.error),
            "",
        )
        with self._lock:
            selected_changed = self._selected_paths != paths
            items_changed = self._items != items
            running_changed = self._running != running
            done_changed = self._done != done
            failed_changed = self._failed_count != failed_count
            retry_changed = self._can_retry != can_retry
            error_changed = bool(batch_error) and self._last_error != batch_error
            self._selected_paths = paths
            self._items = items
            self._running = running
            self._done = done
            self._failed_count = failed_count
            self._can_retry = can_retry
            if batch_error:
                self._last_error = batch_error
        self._emit_state_changes(
            selected=selected_changed,
            files=items_changed,
            results=items_changed,
            running=running_changed,
            done=done_changed,
            failed=failed_changed,
            retry=retry_changed,
            error=error_changed,
        )

    def _file_maps(self) -> list[dict[str, Any]]:
        with self._lock:
            items = self._items
        return [
            {
                "path": _path_text(item.path),
                "name": item.path.name,
                "status": item.status.value,
                "text": item.text,
                "error": item.error,
                "providerId": item.provider_id,
                "model": item.model,
                "attempts": item.attempts,
            }
            for item in items
        ]

    def _item_for(self, path: str) -> AudioFileResult | None:
        expected = _path_text(path)
        with self._lock:
            for item in self._items:
                if _path_text(item.path) == expected:
                    return item
        return None

    def _set_last_error(self, error: str) -> None:
        with self._lock:
            changed = self._last_error != str(error or "")
            self._last_error = str(error or "")
        if changed:
            self.lastErrorChanged.emit()

    def _clear_last_error(self) -> None:
        with self._lock:
            changed = bool(self._last_error)
            self._last_error = ""
        if changed:
            self.lastErrorChanged.emit()

    def _set_last_error_locked(self, error: str) -> None:
        self._last_error = str(error or "")
        self.lastErrorChanged.emit()

    def _emit_state_changes(
        self,
        *,
        selected: bool,
        files: bool,
        results: bool,
        running: bool,
        done: bool,
        failed: bool,
        retry: bool,
        error: bool,
    ) -> None:
        if selected:
            self.selectedFilesChanged.emit()
        if files:
            self.filesChanged.emit()
        if results:
            self.resultsChanged.emit()
        if running:
            self.runningChanged.emit()
        if done:
            self.doneChanged.emit()
        if failed:
            self.failedCountChanged.emit()
        if retry:
            self.canRetryChanged.emit()
        if error:
            self.lastErrorChanged.emit()


QmlAudioBatchController = QmlAudioFileImportController


__all__ = ["QmlAudioBatchController", "QmlAudioFileImportController"]
