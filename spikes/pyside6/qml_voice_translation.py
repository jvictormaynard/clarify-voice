"""Qt composition boundaries for the dedicated voice-translation runtime.

The voice-translation state machine remains in :mod:`voice_translation` and
its recording lifecycle remains in :mod:`voice_translation_runtime`.  This
module only composes those contracts with the typed application provider
registry and the existing Qt recording gateway so a future QML entrypoint can
own the feature without importing the legacy frontend.
"""

from __future__ import annotations

from collections.abc import Callable
import math
from pathlib import Path
import threading
import time
from typing import Any

from PySide6.QtCore import Property, QObject, Qt, Signal, Slot

try:
    import sounddevice as _sounddevice
except (ImportError, OSError):
    _sounddevice = None

from provider_http import ProviderCancelledError
from provider_registry import PROVIDER_REGISTRY
from provider_types import (
    ProviderCapability,
    ProviderConnection,
    TranslationRequest,
    TranslationResult,
    TranscriptionRequest,
    TranscriptionResult,
)
from repositories import AppConfig
from voice_translation import (
    VoiceTranslationConfigurationError,
    VoiceTranslationConfig,
    VoiceTranslationPhase,
    VoiceTranslationProvider,
    VoiceTranslationRequest,
    VoiceTranslationState,
    validate_voice_translation_config,
)
from voice_translation_runtime import (
    VoiceTranslationClipboard,
    VoiceTranslationRecording,
    VoiceTranslationRuntime,
    VoiceTranslationRuntimeState,
    VoiceTranslationScheduler,
)
from workflow_config import WorkflowScope, validate_workflow_route
from workflows import RecordingSnapshot

try:
    from .qml_runtime import QtRecordingAudioGateway
except ImportError:  # PyInstaller analyzes the entrypoint as top-level source.
    from qml_runtime import QtRecordingAudioGateway


_LANGUAGE_NAMES = {
    "en": "English",
    "pt": "Brazilian Portuguese",
    "es": "Spanish",
    "de": "German",
    "ru": "Russian",
}


def _language_display_name(language: str) -> str:
    normalized = str(language or "").strip().replace("_", "-")
    if not normalized:
        return "English"
    if normalized.casefold() == "auto":
        return "the detected source language"
    key = normalized.casefold()
    return _LANGUAGE_NAMES.get(
        key, _LANGUAGE_NAMES.get(key.split("-", 1)[0], normalized)
    )


def _provider_language(language: str) -> str:
    """Use the provider API's language form while retaining BCP-47 in state."""

    normalized = str(language or "").strip().replace("_", "-").lower()
    if normalized in {"", "auto"}:
        return ""
    return normalized.split("-", 1)[0]


class _EventCancellationToken:
    """Expose a ``threading.Event`` through the provider HTTP token contract."""

    def __init__(self, event: Any) -> None:
        self._event = event

    @property
    def cancelled(self) -> bool:
        return bool(self._event.is_set())

    def wait(self, timeout: float) -> bool:
        return bool(self._event.wait(timeout))

    def raise_if_cancelled(
        self,
        provider: str = "",
        operation: str = "",
        operation_id: str | None = None,
    ) -> None:
        if self.cancelled:
            raise ProviderCancelledError(
                provider=provider,
                operation=operation,
                operation_id=operation_id,
            )


def _registry_cancel_token(token: Any) -> Any:
    """Keep native provider tokens intact and bridge plain events when needed."""

    if token is None or callable(getattr(token, "raise_if_cancelled", None)):
        return token
    if callable(getattr(token, "is_set", None)) and callable(
        getattr(token, "wait", None)
    ):
        return _EventCancellationToken(token)
    return token


class QmlVoiceTranslationProvider(VoiceTranslationProvider):
    """Adapt the typed registry to ``VoiceTranslationWorkflow``.

    Audio always follows the normal transcription route.  Text translation is
    deliberately resolved from ``AppConfig.voice_translation`` and never from
    the normal text-generation workflow routes.
    """

    def __init__(
        self,
        config_factory: Callable[[], AppConfig],
        *,
        registry: Any = PROVIDER_REGISTRY,
    ) -> None:
        self._config_factory = config_factory
        self._registry = registry

    def _config(self) -> AppConfig:
        config = self._config_factory()
        if not isinstance(config, AppConfig):
            raise TypeError("voice translation config factory must return AppConfig")
        return config

    def _provider_connection(
        self,
        config: AppConfig,
        provider_id: str,
        custom_endpoint: str,
    ) -> ProviderConnection:
        metadata = self._registry.describe(provider_id)
        provider_config = getattr(config, provider_id)
        connection = ProviderConnection(
            api_key=str(getattr(provider_config, "api_key", "") or "").strip(),
            base_url=(
                str(getattr(provider_config, "base_url", "") or "").strip()
                or str(metadata.default_base_url or "").strip()
            ),
        )
        return self._registry.connection_for_route(
            provider_id,
            connection,
            custom_endpoint,
        )

    @staticmethod
    def _require_enabled(
        route: Any,
        *,
        scope: str,
        capability: ProviderCapability,
    ) -> None:
        if route.enabled:
            return
        raise VoiceTranslationConfigurationError(
            f"{scope} route is disabled",
            field=f"{scope}.enabled",
            provider_id=route.provider_id,
            capability=capability,
        )

    def _transcription_route(self, config: AppConfig) -> Any:
        route = validate_workflow_route(
            config.workflow(WorkflowScope.TRANSCRIPTION),
            WorkflowScope.TRANSCRIPTION,
            registry=self._registry,
        )
        self._require_enabled(
            route,
            scope=WorkflowScope.TRANSCRIPTION.value,
            capability=ProviderCapability.AUDIO_TRANSCRIPTION,
        )
        return route

    def _voice_translation_config(self, config: AppConfig) -> VoiceTranslationConfig:
        voice_config = validate_voice_translation_config(
            config.voice_translation,
            registry=self._registry,
        )
        self._require_enabled(
            voice_config.route,
            scope="voice_translation.route",
            capability=ProviderCapability.TEXT_GENERATION,
        )
        return voice_config

    def transcribe(
        self,
        audio_source: RecordingSnapshot,
        source_language: str,
    ) -> TranscriptionResult:
        config = self._config()
        route = self._transcription_route(config)
        provider_id = route.provider_id
        language = str(source_language or "").strip().replace("_", "-")
        language_label = _language_display_name(language)
        instruction = (
            "You are an expert transcriber. Transcribe the audio directly. "
            "Keep the original meaning and structure. Return ONLY the "
            f"transcribed text. Output MUST be in {language_label}."
        )
        request = TranscriptionRequest(
            audio_path=Path(audio_source.audio_path),
            model=route.model_id,
            language=_provider_language(language),
            instruction=instruction,
            prompt=route.prompt or instruction,
            temperature=0.0,
            audio_bytes=audio_source.audio_bytes,
        )
        result = self._registry.transcribe(
            provider_id,
            request,
            self._provider_connection(config, provider_id, route.custom_endpoint),
            _registry_cancel_token(getattr(audio_source, "cancel_token", None)),
        )
        if not isinstance(result, TranscriptionResult):
            raise TypeError("provider registry must return TranscriptionResult")
        return result

    def translate(self, request: VoiceTranslationRequest) -> TranslationResult:
        config = self._config()
        voice_config = self._voice_translation_config(config)
        route = voice_config.route
        provider_id = route.provider_id
        source_language = str(request.source_language or voice_config.source_language)
        target_language = str(request.target_language or voice_config.target_language)
        source_label = _language_display_name(source_language)
        target_label = _language_display_name(target_language)
        instruction = (
            "Translate the source text faithfully from "
            f"{source_label} to {target_label}. Return ONLY the translated text."
        )
        if route.prompt:
            instruction = (
                f"{instruction}\n\nWorkflow-specific instruction:\n{route.prompt}"
            )
        typed_request = TranslationRequest(
            text=str(request.text),
            model=route.model_id,
            target_language=target_language,
            instruction=instruction,
            source_message=str(request.text),
            temperature=0.0,
        )
        result = self._registry.translate(
            provider_id,
            typed_request,
            self._provider_connection(config, provider_id, route.custom_endpoint),
            _registry_cancel_token(request.cancel_event),
        )
        if not isinstance(result, TranslationResult):
            raise TypeError("provider registry must return TranslationResult")
        return result


class QtVoiceTranslationRecording(VoiceTranslationRecording):
    """Add boundary, level, and cancellation seams to a Qt recording session."""

    def __init__(
        self,
        session: Any,
        *,
        monotonic: Callable[[], float] | None = None,
        sounddevice_module: Any | None = None,
    ) -> None:
        self.session = session
        self._monotonic = monotonic or time.monotonic
        self._sounddevice = (
            _sounddevice if sounddevice_module is None else sounddevice_module
        )
        self._boundary_lock = threading.RLock()
        self._boundary_callback: Callable[[Any], None] | None = None
        self._boundary_claimed = False
        self._vad_stop = threading.Event()
        self._vad_boundary = threading.Event()
        self._vad_worker: threading.Thread | None = None
        self._vad_stream: Any | None = None
        self._vad_reason: Any = None

    def set_boundary_callback(self, callback: Callable[[Any], None] | None) -> None:
        """Route session policy notifications through this recording owner."""

        if callback is not None and not callable(callback):
            raise TypeError("recording boundary callback must be callable")
        with self._boundary_lock:
            self._boundary_callback = callback
            self._boundary_claimed = False
        set_callback = getattr(self.session, "set_boundary_callback", None)
        if callable(set_callback):
            set_callback(self._session_boundary_callback if callback else None)

    def _session_boundary_callback(self, reason: Any = None) -> None:
        """Deliver duration boundaries from the session's lifecycle worker."""

        self._notify_boundary(reason)

    def _notify_boundary(self, reason: Any = None) -> None:
        with self._boundary_lock:
            if self._boundary_claimed:
                return
            self._boundary_claimed = True
            callback = self._boundary_callback
        if callback is not None:
            callback(reason)

    def _boundary_policy(self) -> Any | None:
        policy = getattr(self.session, "boundary_policy", None)
        if policy is None:
            policy = getattr(self.session, "_boundary_policy", None)
        return policy

    def _selected_stream_device(self) -> int | str | None:
        """Resolve the current PortAudio handle from the session's inventory."""

        recorder = getattr(self.session, "recorder", None)
        inventory_reader = getattr(recorder, "microphone_inventory", None)
        if not callable(inventory_reader):
            return None
        inventory = inventory_reader()
        if inventory is None:
            return None
        config = getattr(recorder, "config", None)
        selected_id = None
        if config is not None:
            current = getattr(config, "current", None)
            if callable(current):
                current_config = current()
                selected_id = getattr(
                    getattr(current_config, "microphone", None),
                    "selected_id",
                    None,
                )
        selection = inventory.resolve(selected_id)
        if not getattr(selection, "can_record", False):
            return None
        device = getattr(selection, "device", None)
        backend_index = getattr(device, "backend_index", None)
        if isinstance(backend_index, int):
            return backend_index
        # PortAudio can expose a usable endpoint without a numeric handle in
        # injected/native inventory adapters. Match the recorder's existing
        # test path and let sounddevice resolve the endpoint by name.
        return getattr(device, "name", None) or None

    @staticmethod
    def _input_level(indata: Any) -> float | None:
        try:
            raw_samples = memoryview(indata)
            try:
                samples = raw_samples.cast("h")
            except (TypeError, ValueError):
                samples = memoryview(raw_samples.tobytes()).cast("h")
        except (TypeError, ValueError):
            return None
        if not len(samples):
            return 0.0
        mean_square = sum(sample * sample for sample in samples) / len(samples)
        return min(1.0, math.sqrt(mean_square) / 32768.0 * 16)

    def _start_vad_monitor(self) -> None:
        policy = self._boundary_policy()
        controls = getattr(policy, "controls", None)
        vad = getattr(controls, "vad", None)
        with self._boundary_lock:
            callback_registered = self._boundary_callback is not None
        if (
            policy is None
            or not getattr(vad, "enabled", False)
            or not callback_registered
        ):
            return
        sounddevice_module = self._sounddevice
        stream_type = getattr(sounddevice_module, "RawInputStream", None)
        if not callable(stream_type):
            raise RuntimeError("VAD requires the sounddevice input-level monitor")

        self._vad_stop.clear()
        self._vad_boundary.clear()
        self._vad_reason = None

        def observe_input(indata: Any, *_args: Any) -> None:
            if self._vad_stop.is_set():
                return
            level = self._input_level(indata)
            if level is None:
                return
            try:
                decision = policy.observe(
                    self._monotonic(),
                    input_level=level,
                )
            except Exception:
                return
            if not getattr(decision, "should_stop", False):
                return
            with self._boundary_lock:
                self._vad_reason = getattr(decision, "reason", None)
            self._vad_boundary.set()

        def wait_for_boundary() -> None:
            try:
                self._vad_boundary.wait()
                if (
                    self._vad_stop.is_set()
                    or getattr(
                        getattr(self.session, "cancel_event", None),
                        "is_set",
                        lambda: False,
                    )()
                ):
                    return
                with self._boundary_lock:
                    reason = self._vad_reason
                self._notify_boundary(reason)
            finally:
                with self._boundary_lock:
                    if self._vad_worker is threading.current_thread():
                        self._vad_worker = None

        worker = threading.Thread(
            target=wait_for_boundary,
            name="ClarifyVoiceQmlVoiceTranslationVAD",
            daemon=True,
        )
        with self._boundary_lock:
            self._vad_worker = worker
        worker.start()
        try:
            kwargs: dict[str, Any] = {
                "channels": 1,
                "samplerate": 16000,
                "blocksize": 256,
                "dtype": "int16",
                "callback": observe_input,
            }
            device = self._selected_stream_device()
            if device is not None:
                kwargs["device"] = device
            stream = stream_type(**kwargs)
            with self._boundary_lock:
                self._vad_stream = stream
            stream.start()
        except Exception:
            self._stop_vad_monitor()
            raise

    def _stop_vad_monitor(self) -> None:
        self._vad_stop.set()
        self._vad_boundary.set()
        with self._boundary_lock:
            stream = self._vad_stream
            worker = self._vad_worker
            self._vad_stream = None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=1.0)
        with self._boundary_lock:
            if self._vad_worker is worker:
                self._vad_worker = None

    def _block_boundary_callbacks(self) -> None:
        with self._boundary_lock:
            self._boundary_claimed = True
        self._stop_vad_monitor()

    def attach_worker(self, worker: Any) -> None:
        """Forward scheduler ownership to the underlying Qt session."""

        self.session.attach_worker(worker)

    attach_workflow_worker = attach_worker

    def detach_worker(self, worker: Any) -> None:
        """Release scheduler ownership after the worker has finished."""

        self.session.detach_worker(worker)

    def start(self) -> None:
        try:
            self.session.start()
            self._start_vad_monitor()
        except Exception:
            self._block_boundary_callbacks()
            try:
                self.session.cancel()
            except Exception:
                pass
            raise

    def wait_until_started(self) -> None:
        self.session.wait_until_started()

    def stop(self) -> Any:
        self._block_boundary_callbacks()
        return self.session.stop()

    def complete(self) -> bool | None:
        self._block_boundary_callbacks()
        return self.session.complete()

    def fail(self, error: Exception) -> bool | None:
        self._block_boundary_callbacks()
        return self.session.fail(error)

    def request_cancel(self) -> bool:
        self._block_boundary_callbacks()
        request_cancel = getattr(self.session, "request_cancel", None)
        if callable(request_cancel):
            return bool(request_cancel())
        requested = False
        cancel_event = getattr(self.session, "cancel_event", None)
        if cancel_event is not None:
            cancel_event.set()
            requested = True
        provider_token = getattr(self.session, "provider_cancel_token", None)
        cancel = getattr(provider_token, "cancel", None)
        if callable(cancel):
            cancel()
            requested = True
        return requested

    def cancel(self) -> bool:
        self._block_boundary_callbacks()
        result = self.session.cancel()
        return True if result is None else bool(result)


class QtVoiceTranslationRecordingFactory:
    """Create one wrapped session from the existing Qt audio gateway."""

    def __init__(
        self,
        recording_audio: QtRecordingAudioGateway,
        *,
        monotonic: Callable[[], float] | None = None,
        sounddevice_module: Any | None = None,
    ) -> None:
        self.recording_audio = recording_audio
        self.monotonic = monotonic
        self.sounddevice_module = sounddevice_module

    def __call__(self) -> QtVoiceTranslationRecording | None:
        session = self.recording_audio.create_session()
        return (
            None
            if session is None
            else QtVoiceTranslationRecording(
                session,
                monotonic=self.monotonic,
                sounddevice_module=self.sounddevice_module,
            )
        )


class QmlVoiceTranslationController(QObject):
    """Qt-thread state facade around :class:`VoiceTranslationRuntime`."""

    stateChanged = Signal(object)
    activeChanged = Signal()
    phaseChanged = Signal()
    resultChanged = Signal()
    errorChanged = Signal()

    _runtime_state_arrived = Signal(object)

    def __init__(
        self,
        config_factory: Callable[[], AppConfig],
        clipboard: VoiceTranslationClipboard,
        recording_factory: Callable[[], VoiceTranslationRecording | None],
        scheduler: VoiceTranslationScheduler,
        *,
        provider: VoiceTranslationProvider | None = None,
        registry: Any = PROVIDER_REGISTRY,
        on_usage: Callable[[VoiceTranslationConfig, VoiceTranslationState, float], None]
        | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._config_factory = config_factory
        self._runtime_state = VoiceTranslationRuntimeState(VoiceTranslationPhase.READY)
        self._runtime_state_arrived.connect(
            self._deliver_runtime_state,
            Qt.ConnectionType.QueuedConnection,
        )
        actual_provider = provider or QmlVoiceTranslationProvider(
            config_factory,
            registry=registry,
        )
        self._runtime = VoiceTranslationRuntime(
            actual_provider,
            clipboard,
            recording_factory,
            scheduler,
            lambda: self._config_factory().voice_translation,
            on_state=self._queue_runtime_state,
            on_usage=on_usage,
        )

    @Property(object, notify=stateChanged)
    def state(self) -> VoiceTranslationRuntimeState:
        return self._runtime_state

    @Property(bool, notify=activeChanged)
    def active(self) -> bool:
        return self._runtime.active

    @Property(str, notify=phaseChanged)
    def phase(self) -> str:
        return self._runtime_state.phase.value

    @Property(str, notify=resultChanged)
    def result(self) -> str:
        workflow_state = self._runtime_state.workflow_state
        if workflow_state is None:
            return ""
        return str(
            getattr(workflow_state, "published_text", "")
            or getattr(workflow_state, "translated_text", "")
            or getattr(workflow_state, "raw_transcript", "")
            or ""
        )

    @Property(str, notify=errorChanged)
    def error(self) -> str:
        return str(
            self._runtime_state.error
            or getattr(self._runtime_state.workflow_state, "failure_message", "")
            or ""
        )

    def _queue_runtime_state(self, state: VoiceTranslationRuntimeState) -> None:
        self._runtime_state_arrived.emit(state)

    @Slot(object)
    def _deliver_runtime_state(self, state: VoiceTranslationRuntimeState) -> None:
        self._runtime_state = state
        self.stateChanged.emit(state)
        self.activeChanged.emit()
        self.phaseChanged.emit()
        self.resultChanged.emit()
        self.errorChanged.emit()

    @Slot(result=bool)
    def start(self) -> bool:
        return self._runtime.start()

    @Slot(object, result=bool)
    def startForTarget(self, target: Any) -> bool:
        return self._runtime.start(target)

    @Slot(result=bool)
    def stop(self) -> bool:
        return self._runtime.stop()

    @Slot(result=bool)
    def cancel(self) -> bool:
        return self._runtime.cancel()

    @Slot()
    def clear(self) -> None:
        """Return the presentation state to ready after a terminal run."""

        if self._runtime.active:
            return
        self._runtime_state = VoiceTranslationRuntimeState(VoiceTranslationPhase.READY)
        self.stateChanged.emit(self._runtime_state)
        self.activeChanged.emit()
        self.phaseChanged.emit()
        self.resultChanged.emit()
        self.errorChanged.emit()


def create_qml_voice_translation_controller(
    config_factory: Callable[[], AppConfig],
    recording_audio: QtRecordingAudioGateway,
    clipboard: VoiceTranslationClipboard,
    scheduler: VoiceTranslationScheduler,
    *,
    provider: VoiceTranslationProvider | None = None,
    registry: Any = PROVIDER_REGISTRY,
    on_usage: Callable[[VoiceTranslationConfig, VoiceTranslationState, float], None]
    | None = None,
    parent: QObject | None = None,
) -> QmlVoiceTranslationController:
    """Compose the QML controller without adding a second clipboard adapter."""

    return QmlVoiceTranslationController(
        config_factory,
        clipboard,
        QtVoiceTranslationRecordingFactory(recording_audio),
        scheduler,
        provider=provider,
        registry=registry,
        on_usage=on_usage,
        parent=parent,
    )


__all__ = [
    "QmlVoiceTranslationController",
    "QmlVoiceTranslationProvider",
    "QtVoiceTranslationRecording",
    "QtVoiceTranslationRecordingFactory",
    "create_qml_voice_translation_controller",
]
