"""Qt composition boundaries for the dedicated voice-translation runtime.

The voice-translation state machine remains in :mod:`voice_translation` and
its recording lifecycle remains in :mod:`voice_translation_runtime`.  This
module only composes those contracts with the typed application provider
registry and the existing Qt recording gateway so a future QML entrypoint can
own the feature without importing the legacy frontend.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import Property, QObject, Qt, Signal, Slot

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
    """Add the cancellation request seam to an existing Qt recording session."""

    def __init__(self, session: Any) -> None:
        self.session = session

    def attach_worker(self, worker: Any) -> None:
        """Forward scheduler ownership to the underlying Qt session."""

        self.session.attach_worker(worker)

    attach_workflow_worker = attach_worker

    def detach_worker(self, worker: Any) -> None:
        """Release scheduler ownership after the worker has finished."""

        self.session.detach_worker(worker)

    def start(self) -> None:
        self.session.start()

    def wait_until_started(self) -> None:
        self.session.wait_until_started()

    def stop(self) -> Any:
        return self.session.stop()

    def complete(self) -> bool | None:
        return self.session.complete()

    def fail(self, error: Exception) -> bool | None:
        return self.session.fail(error)

    def request_cancel(self) -> bool:
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
        result = self.session.cancel()
        return True if result is None else bool(result)


class QtVoiceTranslationRecordingFactory:
    """Create one wrapped session from the existing Qt audio gateway."""

    def __init__(self, recording_audio: QtRecordingAudioGateway) -> None:
        self.recording_audio = recording_audio

    def __call__(self) -> QtVoiceTranslationRecording | None:
        session = self.recording_audio.create_session()
        return None if session is None else QtVoiceTranslationRecording(session)


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
