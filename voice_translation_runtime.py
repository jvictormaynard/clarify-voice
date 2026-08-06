"""Recording/lifecycle bridge for the dedicated voice-translation contract.

The core :mod:`voice_translation` module intentionally has no desktop or
threading policy beyond its transaction state machine.  This adapter supplies
the small lifecycle seam needed by a global hotkey: one recording owner, one
worker per stage, cooperative cancellation, and a callback boundary for Tk.
It deliberately does not know about Tk, Win32, or provider names.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from voice_translation import (
    VoiceTranslationConfig,
    VoiceTranslationPhase,
    VoiceTranslationState,
    VoiceTranslationWorkflow,
)


class VoiceTranslationRecording(Protocol):
    """Minimal recording owner implemented by the desktop adapter."""

    def set_boundary_callback(self, callback: Callable[[Any], None] | None) -> None: ...
    def start(self) -> None: ...
    def wait_until_started(self) -> None: ...
    def stop(self) -> Any: ...
    def complete(self) -> bool | None: ...
    def fail(self, error: Exception) -> bool | None: ...
    def request_cancel(self) -> bool: ...
    def cancel(self) -> bool: ...


class VoiceTranslationProvider(Protocol):
    def transcribe(self, audio_source: Any, source_language: str) -> Any: ...
    def translate(self, request: Any) -> Any: ...


class VoiceTranslationClipboard(Protocol):
    def capture_target(self) -> Any | None: ...
    def is_target_current(self, target: Any) -> bool: ...
    def owns_clipboard(self) -> bool: ...
    def publish(self, text: str, target: Any, disposition: Any) -> Any: ...


class VoiceTranslationScheduler(Protocol):
    def run_in_background(self, callback: Callable[[], None]) -> None: ...
    def run_recording(
        self, recording: VoiceTranslationRecording, callback: Callable[[], None]
    ) -> None: ...


@dataclass(frozen=True)
class VoiceTranslationRuntimeState:
    """Lifecycle snapshot emitted before/after a headless workflow run."""

    phase: VoiceTranslationPhase
    operation_id: int = 0
    workflow_state: VoiceTranslationState | None = None
    error: str = ""
    # Keep desktop-specific exception handling at the adapter boundary while
    # still identifying well-known failures that need a dedicated UI flow.
    error_code: str = ""


class VoiceTranslationRuntime:
    """Own one recording-to-publication transaction for a global hotkey."""

    def __init__(
        self,
        provider: VoiceTranslationProvider,
        clipboard: VoiceTranslationClipboard,
        recording_factory: Callable[[], VoiceTranslationRecording | None],
        scheduler: VoiceTranslationScheduler,
        config_factory: Callable[[], VoiceTranslationConfig],
        *,
        on_state: Callable[[VoiceTranslationRuntimeState], None] | None = None,
        on_usage: Callable[[VoiceTranslationConfig, VoiceTranslationState, float], None]
        | None = None,
    ) -> None:
        self._provider = provider
        self._clipboard = clipboard
        self._recording_factory = recording_factory
        self._scheduler = scheduler
        self._config_factory = config_factory
        self._on_state = on_state or (lambda _state: None)
        self._on_usage = on_usage or (lambda _config, _state, _duration: None)
        self._lock = threading.RLock()
        self._active = False
        self._phase = VoiceTranslationPhase.READY
        self._recording: VoiceTranslationRecording | None = None
        self._target: Any | None = None
        self._cancel_event: threading.Event | None = None
        self._operation_id = 0

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    @property
    def phase(self) -> VoiceTranslationPhase:
        with self._lock:
            return self._phase

    @property
    def operation_id(self) -> int:
        with self._lock:
            return self._operation_id

    def _emit(
        self,
        phase: VoiceTranslationPhase,
        *,
        workflow_state: VoiceTranslationState | None = None,
        error: str = "",
    ) -> None:
        with self._lock:
            self._phase = phase
            operation_id = self._operation_id
        self._on_state(
            VoiceTranslationRuntimeState(
                phase, operation_id, workflow_state, str(error or "")
            )
        )

    def _run_recording(
        self, recording: VoiceTranslationRecording, callback: Callable[[], None]
    ) -> None:
        run_recording = getattr(self._scheduler, "run_recording", None)
        if callable(run_recording):
            run_recording(recording, callback)
        else:
            self._scheduler.run_in_background(callback)

    def _current(self, operation_id: int) -> bool:
        with self._lock:
            return self._active and self._operation_id == operation_id

    def start(self, target: Any | None = None) -> bool:
        """Reserve the recorder and begin capture; return False on overlap."""

        with self._lock:
            if self._active:
                return False
            try:
                recording = self._recording_factory()
            except Exception:
                return False
            if recording is None:
                return False
            self._active = True
            self._operation_id += 1
            operation_id = self._operation_id
            self._recording = recording
            self._target = target
            self._cancel_event = threading.Event()
        set_boundary_callback = getattr(recording, "set_boundary_callback", None)
        if callable(set_boundary_callback):
            set_boundary_callback(
                lambda _reason=None, operation_id=operation_id: (
                    self.stop() if self._current(operation_id) else False
                )
            )
        self._emit(VoiceTranslationPhase.RECORDING)
        self._run_recording(
            recording,
            lambda: self._start_recording(recording, operation_id),
        )
        return True

    def _start_recording(
        self, recording: VoiceTranslationRecording, operation_id: int
    ) -> None:
        try:
            recording.start()
        except Exception as error:
            with self._lock:
                current = (
                    self._active
                    and self._operation_id == operation_id
                    and self._phase is VoiceTranslationPhase.RECORDING
                )
            if current:
                try:
                    recording.fail(error)
                except Exception:
                    pass
                self._finish(
                    VoiceTranslationRuntimeState(
                        VoiceTranslationPhase.FAILED,
                        operation_id,
                        error=str(error) or type(error).__name__,
                        error_code=type(error).__name__,
                    )
                )

    def stop(self) -> bool:
        """Stop capture and process its immutable audio snapshot."""

        with self._lock:
            if not self._active or self._phase is not VoiceTranslationPhase.RECORDING:
                return False
            recording = self._recording
            operation_id = self._operation_id
            # Claim the recording-to-processing transition while holding the
            # lifecycle lock. A boundary callback and a manual hotkey can
            # arrive together; only the first caller may enqueue stop().
            self._phase = VoiceTranslationPhase.TRANSCRIBING
        if recording is None:
            return False
        self._emit(VoiceTranslationPhase.TRANSCRIBING)
        self._run_recording(
            recording,
            lambda: self._process_recording(recording, operation_id),
        )
        return True

    def _process_recording(
        self, recording: VoiceTranslationRecording, operation_id: int
    ) -> None:
        try:
            recording.wait_until_started()
            if not self._current(operation_id):
                return
            snapshot = recording.stop()
            recording_duration = getattr(snapshot, "duration_seconds", None)
            if recording_duration is None:
                started_at = getattr(recording, "started_at", None)
                try:
                    recording_duration = max(0.0, time.time() - float(started_at))
                except (TypeError, ValueError):
                    # Small embedding/test recording owners may not expose a
                    # wall-clock start marker. Keep usage best-effort without
                    # making an otherwise successful translation fail.
                    recording_duration = 0.0
            else:
                try:
                    recording_duration = max(0.0, float(recording_duration))
                except (TypeError, ValueError):
                    recording_duration = 0.0
            with self._lock:
                cancel_event = self._cancel_event
                target = self._target
            if cancel_event is not None and cancel_event.is_set():
                self._finish(
                    VoiceTranslationRuntimeState(
                        VoiceTranslationPhase.CANCELLED, operation_id
                    )
                )
                return
            self._emit(VoiceTranslationPhase.TRANSLATING)
            config = self._config_factory()
            workflow = VoiceTranslationWorkflow(
                self._provider,
                self._clipboard,
                config,
            )
            provider_cancel_token = getattr(snapshot, "cancel_token", None)
            result = workflow.run(
                snapshot,
                target=target,
                cancel_event=provider_cancel_token or cancel_event,
            )
            if not self._current(operation_id):
                return
            if result.raw_transcript and result.translated_text:
                try:
                    self._on_usage(config, result, recording_duration)
                except OSError:
                    # Usage persistence is anonymous and best effort; never
                    # turn a successful publication into a failed workflow.
                    pass
            if result.phase is VoiceTranslationPhase.CANCELLED:
                try:
                    recording.cancel()
                except Exception:
                    pass
            else:
                try:
                    recording.complete()
                except Exception:
                    # The result remains available even if cleanup is deferred
                    # to the recording owner during shutdown.
                    pass
            self._finish(
                VoiceTranslationRuntimeState(
                    result.phase,
                    operation_id,
                    result,
                    error=result.failure_message,
                )
            )
        except Exception as error:
            if not self._current(operation_id):
                return
            with self._lock:
                cancelled = bool(
                    self._cancel_event is not None and self._cancel_event.is_set()
                )
            try:
                recording.cancel() if cancelled else recording.fail(error)
            except Exception:
                pass
            self._finish(
                VoiceTranslationRuntimeState(
                    VoiceTranslationPhase.CANCELLED
                    if cancelled
                    else VoiceTranslationPhase.FAILED,
                    operation_id,
                    error=str(error) or type(error).__name__,
                    error_code=type(error).__name__,
                )
            )

    def cancel(self) -> bool:
        """Invalidate processing immediately; recorder cancellation is async."""

        with self._lock:
            if not self._active:
                return False
            operation_id = self._operation_id
            recording = self._recording
            event = self._cancel_event
            phase = self._phase
            if event is not None:
                event.set()
            # Publish the cancellation claim before releasing the lock so a
            # concurrent duration/VAD callback cannot also claim stop().
            if phase is VoiceTranslationPhase.RECORDING:
                self._phase = VoiceTranslationPhase.CANCELLED
        if recording is not None:
            try:
                recording.request_cancel()
            except Exception:
                pass
            # Recorder.stop/cancel can wait on SoX; never block the UI/hotkey
            # callback while asking the owner to release its resources.
            if phase is VoiceTranslationPhase.RECORDING:
                self._scheduler.run_in_background(recording.cancel)
        if phase is VoiceTranslationPhase.RECORDING:
            self._finish(
                VoiceTranslationRuntimeState(
                    VoiceTranslationPhase.CANCELLED, operation_id
                )
            )
        return True

    def _finish(self, state: VoiceTranslationRuntimeState) -> None:
        with self._lock:
            if not self._active or state.operation_id != self._operation_id:
                return
            self._active = False
            self._phase = state.phase
            self._recording = None
            self._target = None
            self._cancel_event = None
        self._on_state(state)


__all__ = [
    "VoiceTranslationRecording",
    "VoiceTranslationRuntime",
    "VoiceTranslationRuntimeState",
    "VoiceTranslationScheduler",
]
