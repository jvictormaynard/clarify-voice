"""Qt-facing workflow boundary for the QML frontend.

The bridge owns only presentation state.  Dictation orchestration remains in
``workflows.WorkflowService`` and all long-running commands are submitted to
the injected dispatcher so a QML slot never waits for recording, providers,
clipboard work, or statistics.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Property, QObject, Signal, Slot

try:
    from workflows import (
        CancelDictation,
        CancelTranslation,
        ChooseTranslationLanguage,
        DismissMicrophoneUnavailable,
        StartDictation,
        StartRewrite,
        StartTranslation,
        StopDictation,
        WorkflowPhase,
        WorkflowState,
    )
except ImportError:  # PyInstaller may analyze the spike as a standalone file.
    from ...workflows import (  # type: ignore[no-redef]
        CancelDictation,
        CancelTranslation,
        ChooseTranslationLanguage,
        DismissMicrophoneUnavailable,
        StartDictation,
        StartRewrite,
        StartTranslation,
        StopDictation,
        WorkflowPhase,
        WorkflowState,
    )


class QmlWorkflowBridge(QObject):
    """Expose one injected :class:`WorkflowService` to QML.

    The service listener is expected to be delivered by the service scheduler
    on the Qt GUI thread.  ``dispatch_runner`` is deliberately injectable so
    tests can execute commands deterministically; the real entrypoint passes
    the Qt scheduler's background runner.
    """

    surfaceChanged = Signal()
    statusChanged = Signal()
    resultChanged = Signal()
    busyChanged = Signal()
    canShowResultChanged = Signal()
    modeChanged = Signal()
    languageChanged = Signal()
    copyCompleted = Signal(bool)

    _TRANSLATION_OPTIONS = (
        {"code": "en", "label": "English"},
        {"code": "pt", "label": "Portuguese"},
        {"code": "es", "label": "Spanish"},
        {"code": "de", "label": "German"},
        {"code": "ru", "label": "Russian"},
    )

    _STATUS = {
        WorkflowPhase.READY: "Ready to capture your voice",
        WorkflowPhase.RECORDING: "Listening to your microphone",
        WorkflowPhase.PROCESSING: "Polishing your words",
        WorkflowPhase.REWRITING: "Polishing your words",
        WorkflowPhase.PREPARING_TRANSLATION: "Preparing translation",
        WorkflowPhase.TRANSLATION_PICKER: "Choose a translation language",
        WorkflowPhase.TRANSLATING: "Translating your words",
        WorkflowPhase.PUBLISHING: "Publishing your result",
        WorkflowPhase.MICROPHONE_UNAVAILABLE: "Microphone unavailable",
        WorkflowPhase.COMPLETED: "Your result is ready",
        WorkflowPhase.FAILED: "The dictation could not be completed",
    }
    _STATUS_KEYS = {
        "error": "The dictation could not be completed",
        "no_audio": "No usable audio was captured",
    }
    _ERROR_PHASES = frozenset(
        {
            WorkflowPhase.MICROPHONE_UNAVAILABLE,
            WorkflowPhase.FAILED,
        }
    )
    _BUSY_PHASES = frozenset(
        {
            WorkflowPhase.RECORDING,
            WorkflowPhase.PROCESSING,
            WorkflowPhase.REWRITING,
            WorkflowPhase.PREPARING_TRANSLATION,
            WorkflowPhase.TRANSLATION_PICKER,
            WorkflowPhase.TRANSLATING,
            WorkflowPhase.PUBLISHING,
        }
    )

    def __init__(
        self,
        workflow_service: Any,
        *,
        app_config: Any | None = None,
        dispatch_runner: Callable[[Callable[[], None]], None] | None = None,
        copy_runner: Callable[[str], Any] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._workflow_service = workflow_service
        self._dispatch_runner = dispatch_runner or (lambda callback: callback())
        default_copy_runner = getattr(workflow_service, "copy_result", None)
        self._copy_runner = copy_runner or (
            default_copy_runner if callable(default_copy_runner) else lambda _text: None
        )
        self._state = workflow_service.state
        self._result_visible = False
        self._settings_visible = False
        self._finishing = False
        saved_config = app_config
        if saved_config is None:
            config_provider = getattr(workflow_service, "_config", None)
            current_config = getattr(config_provider, "current", None)
            if callable(current_config):
                saved_config = current_config()
        ui_preferences = getattr(saved_config, "ui", None)
        self._mode = self._normalize_mode(getattr(ui_preferences, "mode", "prompt"))
        self._language = self._normalize_language(
            getattr(ui_preferences, "language", "en")
        )
        workflow_service.subscribe(self._on_workflow_state)

    @Property(str, notify=surfaceChanged)
    def surface(self) -> str:
        if self._settings_visible:
            return "settings"
        if self._result_visible:
            return "result"
        if self._finishing:
            return "idle"
        return self._surface_for_phase(self._state.phase)

    @Property(str, notify=statusChanged)
    def status(self) -> str:
        if self._finishing:
            return self._STATUS[WorkflowPhase.READY]
        if self._state.status_key in self._STATUS_KEYS:
            return self._STATUS_KEYS[self._state.status_key]
        return self._STATUS.get(
            self._state.phase,
            "The dictation could not be completed",
        )

    @Property(str, notify=resultChanged)
    def result(self) -> str:
        return self._state.result_text or ""

    @Property(bool, notify=busyChanged)
    def busy(self) -> bool:
        return self._state.phase in self._BUSY_PHASES

    @Property(bool, notify=canShowResultChanged)
    def canShowResult(self) -> bool:
        return self._state.phase is WorkflowPhase.COMPLETED and bool(
            self._state.result_text
        )

    @Property(str, notify=modeChanged)
    def mode(self) -> str:
        return self._mode

    @Property(str, notify=languageChanged)
    def language(self) -> str:
        return self._language

    @Property("QVariantList", constant=True)
    def translationOptions(self) -> list[dict[str, str]]:
        """Return the supported target languages in a QML-friendly shape."""

        return [dict(option) for option in self._TRANSLATION_OPTIONS]

    @staticmethod
    def _surface_for_phase(phase: WorkflowPhase) -> str:
        if phase is WorkflowPhase.RECORDING:
            return "recording"
        if phase is WorkflowPhase.TRANSLATION_PICKER:
            return "translation_picker"
        if phase in (
            WorkflowPhase.PROCESSING,
            WorkflowPhase.REWRITING,
            WorkflowPhase.PREPARING_TRANSLATION,
            WorkflowPhase.TRANSLATING,
            WorkflowPhase.PUBLISHING,
        ):
            return "processing"
        if phase is WorkflowPhase.COMPLETED:
            return "success"
        if phase in QmlWorkflowBridge._ERROR_PHASES:
            return "error"
        return "idle"

    def _notify_all(self) -> None:
        self.surfaceChanged.emit()
        self.statusChanged.emit()
        self.resultChanged.emit()
        self.busyChanged.emit()
        self.canShowResultChanged.emit()

    @Slot(object)
    def _on_workflow_state(self, state: WorkflowState) -> None:
        self._state = state
        self._finishing = False
        if state.phase is not WorkflowPhase.COMPLETED:
            self._result_visible = False
        self._notify_all()

    def _submit(self, callback: Callable[[], None]) -> None:
        self._dispatch_runner(callback)

    @staticmethod
    def _normalize_mode(mode: Any) -> str:
        normalized = str(mode or "").strip().lower()
        return normalized if normalized in {"prompt", "transcription"} else "prompt"

    @staticmethod
    def _normalize_language(language: Any) -> str:
        normalized = str(language or "").strip().lower()
        return normalized or "en"

    @Slot(str)
    def setMode(self, mode: str) -> None:
        normalized = str(mode or "").strip().lower()
        if normalized in {"prompt", "transcription"} and normalized != self._mode:
            self._mode = normalized
            self.modeChanged.emit()

    @Slot(str)
    def setLanguage(self, language: str) -> None:
        normalized = str(language or "").strip().lower()
        if normalized and normalized != self._language:
            self._language = normalized
            self.languageChanged.emit()

    @Slot(str, result=bool)
    def chooseTranslation(self, language: str) -> bool:
        """Dispatch a real translation-language choice from the picker."""

        if self._state.phase is not WorkflowPhase.TRANSLATION_PICKER:
            return False
        normalized = str(language or "").strip().lower()
        supported = {option["code"] for option in self._TRANSLATION_OPTIONS}
        if normalized not in supported:
            return False
        self._submit(
            lambda: self._workflow_service.dispatch(
                ChooseTranslationLanguage(normalized)
            )
        )
        return True

    @Slot(result=bool)
    def cancelTranslation(self) -> bool:
        """Cancel the active picker through the workflow service."""

        if self._state.phase is not WorkflowPhase.TRANSLATION_PICKER:
            return False
        self._submit(lambda: self._workflow_service.dispatch(CancelTranslation()))
        return True

    @Slot()
    def startRecording(self) -> None:
        if self._state.phase is not WorkflowPhase.READY:
            return
        self._settings_visible = False
        self._result_visible = False
        self._submit(
            lambda: self._workflow_service.dispatch(
                StartDictation(None, self._mode, self._language)
            )
        )

    @Slot()
    def stopRecording(self) -> None:
        if self._state.phase is not WorkflowPhase.RECORDING:
            return
        self._submit(lambda: self._workflow_service.dispatch(StopDictation()))

    @Slot()
    def cancelRecording(self) -> None:
        if self._state.phase is not WorkflowPhase.RECORDING:
            return
        self._submit(lambda: self._workflow_service.dispatch(CancelDictation()))

    @Slot(str, result=bool)
    def handleHotkey(self, action: str) -> bool:
        """Dispatch a native-shell action through the real workflow service."""

        normalized = str(action or "").strip().lower()
        if normalized == "recording_hotkey":
            if self._state.phase is WorkflowPhase.RECORDING:
                self.stopRecording()
                return True
            if self._state.phase is WorkflowPhase.READY:
                self.startRecording()
                return True
            return False

        if normalized == "rewrite_hotkey":
            if self._state.phase is not WorkflowPhase.READY:
                return False
            self._submit(lambda: self._workflow_service.dispatch(StartRewrite()))
            return True

        if normalized == "translation_hotkey":
            if self._state.phase is not WorkflowPhase.READY:
                return False
            self._submit(lambda: self._workflow_service.dispatch(StartTranslation()))
            return True

        if normalized == "escape":
            if self._state.phase is WorkflowPhase.RECORDING:
                self.cancelRecording()
                return True
            if self._state.phase is WorkflowPhase.TRANSLATION_PICKER:
                return self.cancelTranslation()
        return False

    @Slot()
    def showResult(self) -> None:
        if not self.canShowResult:
            return
        self._result_visible = True
        self._settings_visible = False
        self._notify_all()

    @Slot(result=bool)
    def copyResult(self) -> bool:
        if not self.canShowResult:
            return False
        result = self.result

        def copy() -> None:
            try:
                self._copy_runner(result)
            except Exception:
                self.copyCompleted.emit(False)
            else:
                self.copyCompleted.emit(True)

        self._submit(copy)
        return True

    @Slot()
    def finish(self) -> None:
        if self._state.phase not in (
            WorkflowPhase.COMPLETED,
            WorkflowPhase.FAILED,
        ):
            return
        operation_id = self._state.operation_id
        self._finishing = True
        self._result_visible = False
        self._settings_visible = False
        self._notify_all()
        self._submit(lambda: self._workflow_service.finish(operation_id))

    @Slot()
    def reset(self) -> None:
        if self._settings_visible:
            self.closeSettings()
            return
        if self._state.phase is WorkflowPhase.RECORDING:
            self.cancelRecording()
            return
        if self._state.phase is WorkflowPhase.MICROPHONE_UNAVAILABLE:
            self._submit(
                lambda: self._workflow_service.dispatch(DismissMicrophoneUnavailable())
            )
            return
        if self._state.phase in (
            WorkflowPhase.COMPLETED,
            WorkflowPhase.FAILED,
        ):
            self.finish()

    @Slot()
    def openSettings(self) -> None:
        if self.busy or self._state.phase is not WorkflowPhase.READY:
            return
        self._settings_visible = True
        self._result_visible = False
        self._notify_all()

    @Slot()
    def closeSettings(self) -> None:
        if not self._settings_visible:
            return
        self._settings_visible = False
        self._notify_all()
