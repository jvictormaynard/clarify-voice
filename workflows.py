"""UI-independent orchestration for ClarifyVoice desktop workflows.

The desktop view dispatches commands and renders :class:`WorkflowState` values.
All provider, audio, clipboard, configuration, statistics, scheduling, and
clock behavior enters through the small interfaces in this module.  This keeps
worker completion and cancellation rules testable without constructing Tk.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol

from provider_types import RewriteResult, TranscriptionResult, TranslationResult


class WorkflowKind(str, Enum):
    DICTATION = "dictation"
    REWRITE = "rewrite"
    TRANSLATION = "translation"


class WorkflowPhase(str, Enum):
    READY = "ready"
    RECORDING = "recording"
    PROCESSING = "processing"
    REWRITING = "rewriting"
    PREPARING_TRANSLATION = "preparing_translation"
    TRANSLATION_PICKER = "translation_picker"
    TRANSLATING = "translating"
    MICROPHONE_UNAVAILABLE = "microphone_unavailable"
    COMPLETED = "completed"
    FAILED = "failed"


class SelectionDisposition(str, Enum):
    PASTED = "pasted"
    COPIED = "copied"


class NoUsableAudioError(RuntimeError):
    """The recording session finished without a provider-ready audio source."""


@dataclass(frozen=True)
class SelectionTarget:
    """Opaque foreground target captured before a global shortcut yields focus."""

    window: Any
    executable: str | None = None


@dataclass(frozen=True)
class SelectionCapture:
    """Selected text plus adapter-owned restoration/ownership context."""

    target: SelectionTarget
    text: str
    context: Any = None


@dataclass(frozen=True)
class WorkflowState:
    phase: WorkflowPhase = WorkflowPhase.READY
    operation_id: int = 0
    kind: WorkflowKind | None = None
    target_executable: str | None = None
    result_text: str | None = None
    status_key: str | None = None


@dataclass(frozen=True)
class StartDictation:
    target_executable: str | None
    mode: str
    language: str


@dataclass(frozen=True)
class StopDictation:
    pass


@dataclass(frozen=True)
class CancelDictation:
    pass


@dataclass(frozen=True)
class DismissMicrophoneUnavailable:
    pass


@dataclass(frozen=True)
class StartRewrite:
    pass


@dataclass(frozen=True)
class StartTranslation:
    pass


@dataclass(frozen=True)
class ChooseTranslationLanguage:
    language: str


@dataclass(frozen=True)
class CancelTranslation:
    pass


WorkflowCommand = (
    StartDictation
    | StopDictation
    | CancelDictation
    | DismissMicrophoneUnavailable
    | StartRewrite
    | StartTranslation
    | ChooseTranslationLanguage
    | CancelTranslation
)


class ProviderGateway(Protocol):
    """Workflow facade over typed registry requests and results.

    Implementations own provider selection, request construction, connections,
    and errors; the workflow only consumes the registry's typed results.
    """

    def transcribe(
        self, audio_source: Any, mode: str, language: str
    ) -> TranscriptionResult: ...
    def rewrite(self, text: str) -> RewriteResult: ...
    def translate(
        self, text: str, target_language: str
    ) -> TranslationResult: ...


class RecordingSessionGateway(Protocol):
    """One recording lifecycle owned by the recording component."""

    def start(self) -> None: ...
    def wait_until_started(self) -> None:
        """Wait for startup to finish, raising its failure or cancellation."""
        ...
    def stop(self) -> Any: ...
    def cancel(self) -> None: ...
    def complete(self) -> None: ...
    def fail(self, error: Exception) -> None: ...


class AudioGateway(Protocol):
    def microphone_available(self) -> bool | None: ...
    def create_session(self) -> RecordingSessionGateway: ...


class ClipboardGateway(Protocol):
    """High-level, focus-safe clipboard transactions owned by the adapter.

    ``capture_selection`` preserves the user's clipboard and returns opaque
    ownership context. ``apply_result`` owns the conditional paste/restore
    transaction and reports whether it pasted or only left the result copied.
    """

    def capture_target(self) -> SelectionTarget | None: ...
    def is_target_current(self, target: SelectionTarget) -> bool: ...
    def capture_selection(
        self, target: SelectionTarget
    ) -> SelectionCapture | None: ...
    def restore(self, capture: SelectionCapture) -> None: ...
    def apply_result(
        self, capture: SelectionCapture, result: str
    ) -> SelectionDisposition: ...
    def write_dictation_result(self, text: str) -> None: ...
    def activate(self, target: SelectionTarget) -> None: ...
    def alt_pressed(self) -> bool: ...


class WorkflowConfig(Protocol):
    def recording_usage_context(self, mode: str) -> dict[str, Any]: ...


class StatisticsGateway(Protocol):
    def record_dictation(
        self, context: dict[str, Any], duration_seconds: float, result: str
    ) -> None: ...
    def record_rewrite(
        self, provider: str, model: str, source: str, result: str
    ) -> None: ...
    def record_translation(
        self,
        provider: str,
        model: str,
        source: str,
        result: str,
        target_language: str,
    ) -> None: ...


class Scheduler(Protocol):
    def call_soon(self, callback: Callable[[], None]) -> None: ...
    def run_in_background(self, callback: Callable[[], None]) -> None: ...


class Clock(Protocol):
    def time(self) -> float: ...
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def time(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass
class _Session:
    operation_id: int
    kind: WorkflowKind
    target: SelectionTarget | None = None
    target_executable: str | None = None
    mode: str = ""
    language: str = ""
    started_at: float = 0.0
    selection: SelectionCapture | None = None
    target_language: str = ""
    usage_context: dict[str, Any] = field(default_factory=dict)
    recording: RecordingSessionGateway | None = None


class WorkflowService:
    """Coordinate dictation, rewrite, and translation outside the desktop UI."""

    def __init__(
        self,
        provider: ProviderGateway,
        audio: AudioGateway,
        clipboard: ClipboardGateway,
        config: WorkflowConfig,
        statistics: StatisticsGateway,
        scheduler: Scheduler,
        clock: Clock | None = None,
    ):
        self._provider = provider
        self._audio = audio
        self._clipboard = clipboard
        self._config = config
        self._statistics = statistics
        self._scheduler = scheduler
        self._clock = clock or SystemClock()
        self._lock = threading.RLock()
        self._next_operation_id = 1
        self._session: _Session | None = None
        self._state = WorkflowState()
        self._listeners: list[Callable[[WorkflowState], None]] = []

    @property
    def state(self) -> WorkflowState:
        with self._lock:
            return self._state

    def subscribe(self, listener: Callable[[WorkflowState], None]) -> None:
        self._listeners.append(listener)

    def dispatch(self, command: WorkflowCommand) -> bool:
        if isinstance(command, StartDictation):
            return self._start_dictation(command)
        if isinstance(command, StopDictation):
            return self._stop_dictation()
        if isinstance(command, CancelDictation):
            return self._cancel_dictation()
        if isinstance(command, DismissMicrophoneUnavailable):
            return self._dismiss_microphone_unavailable()
        if isinstance(command, StartRewrite):
            return self._start_rewrite()
        if isinstance(command, StartTranslation):
            return self._start_translation()
        if isinstance(command, ChooseTranslationLanguage):
            return self._choose_translation_language(command.language)
        if isinstance(command, CancelTranslation):
            return self._cancel_translation()
        raise TypeError(f"Unsupported workflow command: {type(command).__name__}")

    def finish(self, operation_id: int) -> bool:
        """Release a terminal operation after the view finishes its animation."""
        with self._lock:
            if not self._is_current_locked(operation_id):
                return False
            if self._state.phase not in (
                WorkflowPhase.COMPLETED,
                WorkflowPhase.FAILED,
            ):
                return False
            self._session = None
            self._state = WorkflowState()
        self._scheduler.call_soon(lambda: self._deliver_ready())
        return True

    def cancel_active(self) -> None:
        """Invalidate any worker, used during application shutdown."""
        with self._lock:
            session = self._session
            self._session = None
            self._state = WorkflowState()
        if session and session.recording is not None:
            self._scheduler.run_in_background(session.recording.cancel)

    def _new_session(
        self,
        kind: WorkflowKind,
        *,
        target: SelectionTarget | None = None,
        target_executable: str | None = None,
    ) -> _Session | None:
        with self._lock:
            if self._session is not None or self._state.phase is not WorkflowPhase.READY:
                return None
            session = _Session(
                operation_id=self._next_operation_id,
                kind=kind,
                target=target,
                target_executable=(
                    target.executable if target is not None else target_executable
                ),
            )
            self._next_operation_id += 1
            self._session = session
            return session

    def _is_current_locked(self, operation_id: int) -> bool:
        return (
            self._session is not None
            and self._session.operation_id == operation_id
        )

    def _is_current(self, operation_id: int) -> bool:
        with self._lock:
            return self._is_current_locked(operation_id)

    def _transition(
        self,
        session: _Session,
        phase: WorkflowPhase,
        *,
        result_text: str | None = None,
        status_key: str | None = None,
    ) -> bool:
        with self._lock:
            if not self._is_current_locked(session.operation_id):
                return False
            state = WorkflowState(
                phase=phase,
                operation_id=session.operation_id,
                kind=session.kind,
                target_executable=session.target_executable,
                result_text=result_text,
                status_key=status_key,
            )
            self._state = state
        self._scheduler.call_soon(lambda: self._deliver(state))
        return True

    def _deliver(self, state: WorkflowState) -> None:
        with self._lock:
            if self._state != state:
                return
        for listener in tuple(self._listeners):
            listener(state)

    @staticmethod
    def _provider_failed(result: str | None) -> bool:
        return not result

    def _restore_selection_if_current(
        self, session: _Session, capture: SelectionCapture | None
    ) -> None:
        if capture is None:
            return
        with self._lock:
            if not self._is_current_locked(session.operation_id):
                return
            try:
                self._clipboard.restore(capture)
            except Exception:
                pass

    def _wait_for_alt_release(self, session: _Session) -> bool:
        deadline = self._clock.monotonic() + 0.8
        while self._clipboard.alt_pressed() and self._clock.monotonic() < deadline:
            if not self._is_current(session.operation_id):
                return False
            self._clock.sleep(0.01)
        return not self._clipboard.alt_pressed()

    def _target_is_current(self, session: _Session) -> bool:
        return (
            session.target is not None
            and self._clipboard.is_target_current(session.target)
        )

    # Dictation

    def _start_dictation(self, command: StartDictation) -> bool:
        if self._audio.microphone_available() is False:
            session = self._new_session(
                WorkflowKind.DICTATION,
                target_executable=command.target_executable,
            )
            if session is None:
                return False
            return self._transition(session, WorkflowPhase.MICROPHONE_UNAVAILABLE)

        session = self._new_session(
            WorkflowKind.DICTATION,
            target_executable=command.target_executable,
        )
        if session is None:
            return False
        session.mode = command.mode
        session.language = command.language
        session.started_at = self._clock.time()
        session.usage_context = self._config.recording_usage_context(command.mode)
        try:
            session.recording = self._audio.create_session()
        except Exception:
            self._transition(session, WorkflowPhase.FAILED, status_key="error")
            return True
        self._transition(session, WorkflowPhase.RECORDING)
        self._scheduler.run_in_background(lambda: self._start_audio(session))
        return True

    def _start_audio(self, session: _Session) -> None:
        try:
            if session.recording is None:
                raise RuntimeError("Recording session was not created")
            session.recording.start()
        except Exception as error:
            with self._lock:
                if (
                    self._is_current_locked(session.operation_id)
                    and self._state.phase is WorkflowPhase.RECORDING
                ):
                    if session.recording is not None:
                        try:
                            session.recording.fail(error)
                        except Exception:
                            pass
                    self._transition(session, WorkflowPhase.MICROPHONE_UNAVAILABLE)
        finally:
            if (
                not self._is_current(session.operation_id)
                and session.recording is not None
            ):
                try:
                    session.recording.cancel()
                except Exception:
                    pass

    def _stop_dictation(self) -> bool:
        with self._lock:
            session = self._session
            if (
                session is None
                or session.kind is not WorkflowKind.DICTATION
                or self._state.phase is not WorkflowPhase.RECORDING
            ):
                return False
        elapsed = self._clock.time() - session.started_at
        self._transition(session, WorkflowPhase.PROCESSING)
        self._scheduler.run_in_background(
            lambda: self._process_dictation(session, elapsed)
        )
        return True

    def _process_dictation(self, session: _Session, elapsed: float) -> None:
        try:
            if not self._is_current(session.operation_id):
                return
            if session.recording is None:
                raise RuntimeError("Recording session was not created")
            session.recording.wait_until_started()
            if not self._is_current(session.operation_id):
                return
            audio_source = session.recording.stop()
            provider_result = self._provider.transcribe(
                audio_source, session.mode, session.language
            )
            result = provider_result.text
            if not self._is_current(session.operation_id):
                return
            if self._provider_failed(result):
                session.recording.fail(
                    RuntimeError("Transcription returned no text")
                )
                self._transition(session, WorkflowPhase.FAILED, status_key="error")
                return
            with self._lock:
                if not self._is_current_locked(session.operation_id):
                    return
                session.recording.complete()
                try:
                    self._statistics.record_dictation(
                        session.usage_context, elapsed, result
                    )
                except OSError:
                    pass
                self._scheduler.run_in_background(
                    lambda: self._write_dictation_if_current(session, result)
                )
            self._transition(
                session, WorkflowPhase.COMPLETED, result_text=result
            )
        except NoUsableAudioError as error:
            if not self._is_current(session.operation_id):
                return
            if session.recording is not None:
                try:
                    session.recording.fail(error)
                except Exception:
                    pass
            self._transition(session, WorkflowPhase.FAILED, status_key="no_audio")
        except Exception as error:
            if not self._is_current(session.operation_id):
                return
            if session.recording is not None:
                try:
                    session.recording.fail(error)
                except Exception:
                    pass
            self._transition(session, WorkflowPhase.FAILED, status_key="error")

    def _write_dictation_if_current(
        self, session: _Session, result: str
    ) -> None:
        with self._lock:
            if not self._is_current_locked(session.operation_id):
                return
            self._clipboard.write_dictation_result(result)

    def _cancel_dictation(self) -> bool:
        with self._lock:
            session = self._session
            if (
                session is None
                or session.kind is not WorkflowKind.DICTATION
                or self._state.phase is not WorkflowPhase.RECORDING
            ):
                return False
            self._session = None
            self._state = WorkflowState()
        if session.recording is not None:
            self._scheduler.run_in_background(session.recording.cancel)
        self._scheduler.call_soon(lambda: self._deliver_ready())
        return True

    def _dismiss_microphone_unavailable(self) -> bool:
        with self._lock:
            if self._state.phase is not WorkflowPhase.MICROPHONE_UNAVAILABLE:
                return False
            self._session = None
            self._state = WorkflowState()
        self._scheduler.call_soon(lambda: self._deliver_ready())
        return True

    def _deliver_ready(self) -> None:
        if self.state.phase is not WorkflowPhase.READY:
            return
        for listener in tuple(self._listeners):
            listener(self.state)

    # Rewrite

    def _start_rewrite(self) -> bool:
        target = self._clipboard.capture_target()
        if target is None:
            return False
        session = self._new_session(
            WorkflowKind.REWRITE,
            target=target,
        )
        if session is None:
            return False
        self._transition(session, WorkflowPhase.REWRITING)
        self._scheduler.run_in_background(lambda: self._rewrite_worker(session))
        return True

    def _rewrite_worker(self, session: _Session) -> None:
        capture = None
        try:
            if not self._wait_for_alt_release(session):
                self._transition(
                    session, WorkflowPhase.FAILED, status_key="no_selection"
                )
                return
            with self._lock:
                if not self._is_current_locked(session.operation_id):
                    return
                target = session.target
                if (
                    target is None
                    or not self._clipboard.is_target_current(target)
                ):
                    self._transition(
                        session,
                        WorkflowPhase.FAILED,
                        status_key="no_selection",
                    )
                    return
                capture = self._clipboard.capture_selection(target)
            if capture is None or not capture.text.strip():
                self._restore_selection_if_current(session, capture)
                self._transition(
                    session, WorkflowPhase.FAILED, status_key="no_selection"
                )
                return
            with self._lock:
                if not self._is_current_locked(session.operation_id):
                    return
                if not self._target_is_current(session):
                    self._restore_selection_if_current(session, capture)
                    self._transition(
                        session,
                        WorkflowPhase.FAILED,
                        status_key="no_selection",
                    )
                    return
            provider_result = self._provider.rewrite(capture.text)
            rewritten = provider_result.text
            if not self._is_current(session.operation_id):
                return
            if self._provider_failed(rewritten):
                self._restore_selection_if_current(session, capture)
                self._transition(
                    session, WorkflowPhase.FAILED, status_key="rewrite_failed"
                )
                return
            self._apply_selection_result(
                session,
                capture,
                rewritten,
                copied_status="rewrite_copied",
                statistic=lambda: self._statistics.record_rewrite(
                    provider_result.provider_id,
                    provider_result.model,
                    capture.text,
                    rewritten,
                ),
            )
        except Exception:
            self._restore_selection_if_current(session, capture)
            self._transition(
                session, WorkflowPhase.FAILED, status_key="rewrite_failed"
            )

    # Translation

    def _start_translation(self) -> bool:
        target = self._clipboard.capture_target()
        if target is None:
            return False
        session = self._new_session(
            WorkflowKind.TRANSLATION,
            target=target,
        )
        if session is None:
            return False
        # The state is explicit even though the current presentation keeps this
        # preparation step visually silent until the picker is ready.
        self._transition(session, WorkflowPhase.PREPARING_TRANSLATION)
        self._scheduler.run_in_background(
            lambda: self._prepare_translation(session)
        )
        return True

    def _prepare_translation(self, session: _Session) -> None:
        capture = None
        try:
            if (
                not self._wait_for_alt_release(session)
                or session.target is None
                or not self._clipboard.is_target_current(session.target)
            ):
                self._transition(
                    session, WorkflowPhase.FAILED, status_key="no_selection"
                )
                return
            with self._lock:
                if not self._is_current_locked(session.operation_id):
                    return
                capture = self._clipboard.capture_selection(session.target)
            if capture is None or not capture.text.strip():
                self._restore_selection_if_current(session, capture)
                self._transition(
                    session, WorkflowPhase.FAILED, status_key="no_selection"
                )
                return
            with self._lock:
                if not self._is_current_locked(session.operation_id):
                    return
                if not self._target_is_current(session):
                    self._restore_selection_if_current(session, capture)
                    self._transition(
                        session,
                        WorkflowPhase.FAILED,
                        status_key="no_selection",
                    )
                    return
                self._clipboard.restore(capture)
                session.selection = capture
            self._transition(session, WorkflowPhase.TRANSLATION_PICKER)
        except Exception:
            self._restore_selection_if_current(session, capture)
            self._transition(
                session, WorkflowPhase.FAILED, status_key="translation_failed"
            )

    def _choose_translation_language(self, language: str) -> bool:
        with self._lock:
            session = self._session
            if (
                session is None
                or session.kind is not WorkflowKind.TRANSLATION
                or self._state.phase is not WorkflowPhase.TRANSLATION_PICKER
                or not language
            ):
                return False
            session.target_language = language
            if session.target is not None:
                self._clipboard.activate(session.target)
        self._transition(session, WorkflowPhase.TRANSLATING)
        self._scheduler.run_in_background(
            lambda: self._translation_worker(session)
        )
        return True

    def _translation_worker(self, session: _Session) -> None:
        try:
            if session.selection is None:
                raise RuntimeError("Translation selection was not captured")
            provider_result = self._provider.translate(
                session.selection.text, session.target_language
            )
            translated = provider_result.text
            if not self._is_current(session.operation_id):
                return
            if self._provider_failed(translated):
                self._transition(
                    session,
                    WorkflowPhase.FAILED,
                    status_key="translation_failed",
                )
                return
            self._apply_selection_result(
                session,
                session.selection,
                translated,
                copied_status="translation_copied",
                statistic=lambda: self._statistics.record_translation(
                    provider_result.provider_id,
                    provider_result.model,
                    session.selection.text,
                    translated,
                    session.target_language,
                ),
            )
        except Exception:
            self._transition(
                session, WorkflowPhase.FAILED, status_key="translation_failed"
            )

    def _cancel_translation(self) -> bool:
        with self._lock:
            session = self._session
            if (
                session is None
                or session.kind is not WorkflowKind.TRANSLATION
                or self._state.phase is not WorkflowPhase.TRANSLATION_PICKER
            ):
                return False
            if session.target is not None:
                self._clipboard.activate(session.target)
            self._session = None
            self._state = WorkflowState()
        self._scheduler.call_soon(lambda: self._deliver_ready())
        return True

    # Shared selection completion

    def _apply_selection_result(
        self,
        session: _Session,
        capture: SelectionCapture,
        result: str,
        *,
        copied_status: str,
        statistic: Callable[[], None],
    ) -> None:
        with self._lock:
            if not self._is_current_locked(session.operation_id):
                return
            # Once capture identity has been validated, the clipboard adapter
            # owns the later focus-safe fallback: it pastes only into the
            # original selection, otherwise it leaves the result copied.
            disposition = self._clipboard.apply_result(capture, result)
            status_key = (
                None
                if disposition is SelectionDisposition.PASTED
                else copied_status
            )
            try:
                statistic()
            except OSError:
                pass
        self._transition(
            session,
            WorkflowPhase.COMPLETED,
            result_text=result,
            status_key=status_key,
        )
