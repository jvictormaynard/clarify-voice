"""UI-free runtime composition for the Qt Quick frontend.

The QML process owns the desktop surface; this module owns only the concrete
adapters required by :mod:`workflows`.  It deliberately does not import the
legacy frontend or any Tk/CustomTkinter module.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, Qt, Signal, Slot

try:
    from provider_registry import PROVIDER_REGISTRY
    from provider_http import CancellationToken
    from provider_types import (
        ProviderCapability,
        ProviderConnection,
        RewriteRequest,
        TranslationRequest,
        TranscriptionRequest,
        RewriteResult,
        TranslationResult,
        TranscriptionResult,
    )
    from repositories import (
        ApplicationRepositories,
        LocalConfigRepository,
        LocalUsageStatsRepository,
        environment_defaults,
    )
    from workflow_config import WorkflowScope
    from workflows import (
        ClipboardGateway,
        MicrophoneUnavailableError,
        NoUsableAudioError,
        RecordingSessionGateway,
        RecordingSnapshot,
        SelectionDisposition,
        SelectionTarget,
        WorkflowService,
    )
    from windows_clipboard import WindowsClipboardAdapter
except ImportError:  # PyInstaller analyzes this file as a standalone entry point.
    from ...provider_registry import PROVIDER_REGISTRY  # type: ignore[no-redef]
    from ...provider_http import CancellationToken  # type: ignore[no-redef]
    from ...provider_types import (  # type: ignore[no-redef]
        ProviderCapability,
        ProviderConnection,
        RewriteRequest,
        TranslationRequest,
        TranscriptionRequest,
        RewriteResult,
        TranslationResult,
        TranscriptionResult,
    )
    from ...repositories import (  # type: ignore[no-redef]
        ApplicationRepositories,
        LocalConfigRepository,
        LocalUsageStatsRepository,
        environment_defaults,
    )
    from ...workflow_config import WorkflowScope  # type: ignore[no-redef]
    from ...workflows import (  # type: ignore[no-redef]
        ClipboardGateway,
        MicrophoneUnavailableError,
        NoUsableAudioError,
        RecordingSessionGateway,
        RecordingSnapshot,
        SelectionDisposition,
        SelectionTarget,
        WorkflowService,
    )
    from ...windows_clipboard import WindowsClipboardAdapter  # type: ignore[no-redef]


TRANSCRIPTION_INSTRUCTION = (
    "Transcribe the audio accurately. Preserve the speaker's meaning and "
    "return only the transcript."
)
PROMPT_INSTRUCTION = (
    "Transcribe the audio and improve clarity while preserving the speaker's "
    "meaning. Return only the final text."
)
LANGUAGE_NAMES = {
    "en": "English",
    "pt": "Portuguese",
    "es": "Spanish",
    "de": "German",
    "ru": "Russian",
}


class QtRuntimeError(RuntimeError):
    """The real Qt runtime cannot start with the current local installation."""


class QtWorkflowScheduler(QObject):
    """Run workflow callbacks on Qt's GUI thread and workers off it."""

    _callback_ready = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._callback_ready.connect(
            self._run_callback,
            Qt.ConnectionType.QueuedConnection,
        )

    @Slot(object)
    def _run_callback(self, callback: Callable[[], None]) -> None:
        callback()

    def call_soon(self, callback: Callable[[], None]) -> None:
        self._callback_ready.emit(callback)

    def run_in_background(self, callback: Callable[[], None]) -> None:
        threading.Thread(
            target=callback,
            name="ClarifyVoiceQmlWorkflow",
            daemon=True,
        ).start()

    def run_recording(
        self,
        recording: RecordingSessionGateway,
        callback: Callable[[], None],
    ) -> None:
        def run() -> None:
            try:
                callback()
            finally:
                recording.detach_worker(threading.current_thread())

        worker = threading.Thread(
            target=run,
            name="ClarifyVoiceQmlRecording",
            daemon=True,
        )
        attach = getattr(
            recording,
            "attach_workflow_worker",
            recording.attach_worker,
        )
        attach(worker)
        worker.start()


def _data_directory() -> Path:
    configured = os.environ.get("CLARIFYVOICE_DATA_DIR", "").strip()
    if configured:
        path = Path(configured)
    elif platform.system() == "Windows":
        path = Path(os.environ.get("APPDATA", Path.home())) / "ClarifyVoice"
    else:
        path = Path.home() / ".clarifyvoice"
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_runtime_repositories() -> ApplicationRepositories:
    data_dir = _data_directory()
    return ApplicationRepositories(
        config=LocalConfigRepository(
            data_dir / "config.json",
            defaults=environment_defaults(),
        ),
        usage_stats=LocalUsageStatsRepository(data_dir / "usage_stats.json"),
    )


class QtWorkflowConfig:
    """Read the typed route/configuration boundary for one operation."""

    def __init__(self, repositories: ApplicationRepositories) -> None:
        self.repositories = repositories

    def current(self):
        return self.repositories.config.load()

    def recording_usage_context(self, mode: str) -> dict[str, Any]:
        route = self.current().workflow(WorkflowScope.TRANSCRIPTION)
        return {
            "provider": route.provider_id,
            "model": route.model_id,
            "mode": str(mode),
        }


class QtProviderGateway:
    """Provider registry facade with no desktop toolkit dependency."""

    def __init__(self, config: QtWorkflowConfig) -> None:
        self.config = config

    def _route(self, scope: WorkflowScope):
        route = self.config.current().workflow(scope)
        if not route.enabled:
            raise RuntimeError(f"{scope.value} workflow is disabled")
        if not route.model_id:
            raise RuntimeError(f"No model configured for {scope.value}")
        return route

    def _connection(self, route):
        current = self.config.current()
        metadata = PROVIDER_REGISTRY.describe(route.provider_id)
        provider = getattr(current, route.provider_id)
        connection = ProviderConnection(
            api_key=provider.api_key,
            base_url=provider.base_url or metadata.default_base_url,
        )
        return PROVIDER_REGISTRY.connection_for_route(
            route.provider_id,
            connection,
            route.custom_endpoint,
        )

    def transcribe(
        self,
        audio_source: RecordingSnapshot,
        mode: str,
        language: str,
    ) -> TranscriptionResult:
        route = self._route(WorkflowScope.TRANSCRIPTION)
        provider = route.provider_id
        PROVIDER_REGISTRY.describe(provider)
        language = str(language or "auto").strip().lower()
        language_label = LANGUAGE_NAMES.get(language, "the detected source language")
        instruction = (
            TRANSCRIPTION_INSTRUCTION if mode == "transcription" else PROMPT_INSTRUCTION
        )
        request = TranscriptionRequest(
            audio_path=audio_source.audio_path,
            model=route.model_id,
            language=language,
            instruction=instruction,
            prompt=route.prompt or instruction,
            temperature=0.0 if mode == "transcription" else 0.1,
            audio_bytes=audio_source.audio_bytes,
        )
        result = PROVIDER_REGISTRY.transcribe(
            provider,
            request,
            self._connection(route),
            audio_source.cancel_token,
        )
        if not result.text or not result.text.strip():
            raise RuntimeError(
                f"{provider} returned no transcript for {language_label}"
            )
        return result

    def rewrite(self, text: str) -> RewriteResult:
        source = str(text).strip()
        if not source:
            raise RuntimeError("No text selected")
        route = self._route(WorkflowScope.REWRITE)
        provider = route.provider_id
        if not PROVIDER_REGISTRY.supports(provider, ProviderCapability.TEXT_GENERATION):
            raise RuntimeError(f"{provider} does not support text generation")
        request = RewriteRequest(
            text=source,
            model=route.model_id,
            language="en",
            instruction=route.prompt or "Rewrite the selected text clearly.",
            source_message=source,
            temperature=0.1,
        )
        result = PROVIDER_REGISTRY.rewrite(
            provider,
            request,
            self._connection(route),
        )
        if not result.text.strip():
            raise RuntimeError("Provider returned an empty rewrite")
        return result

    def translate(self, text: str, target_language: str) -> TranslationResult:
        source = str(text)
        if not source.strip():
            raise RuntimeError("No text selected")
        route = self._route(WorkflowScope.TRANSLATION)
        provider = route.provider_id
        target = str(target_language or "").strip().lower()
        if not target:
            raise RuntimeError("Translation target language is required")
        request = TranslationRequest(
            text=source,
            model=route.model_id,
            target_language=target,
            instruction=route.prompt or f"Translate the text to {target}.",
            source_message=source,
            temperature=0.0,
        )
        result = PROVIDER_REGISTRY.translate(
            provider,
            request,
            self._connection(route),
        )
        if not result.text.strip():
            raise RuntimeError("Provider returned an empty translation")
        return result


class QtRecorder:
    """Minimal SoX owner for the Qt recording session."""

    def __init__(self) -> None:
        root = Path(__file__).resolve().parents[2]
        bundled = (
            root
            / "extra"
            / "sox-14.4.2"
            / ("sox.exe" if platform.system() == "Windows" else "sox")
        )
        self.sox = str(bundled if bundled.is_file() else shutil.which("sox") or "")
        self.process: subprocess.Popen[bytes] | None = None
        self._lock = threading.RLock()

    def start(self, path: Path, cancel_event: threading.Event) -> None:
        if not self.sox:
            raise QtRuntimeError("SoX was not found in the ClarifyVoice runtime")
        if cancel_event.is_set():
            raise RuntimeError("Recording cancelled before startup")
        system = platform.system()
        if system == "Windows":
            source = ["-t", "waveaudio", "-d"]
        elif system == "Darwin":
            source = ["-t", "coreaudio", "default"]
        else:
            source = ["-t", "pulseaudio", "default"]
        args = [
            self.sox,
            *source,
            "-r",
            "16000",
            "-c",
            "1",
            "-b",
            "16",
            "-e",
            "signed-integer",
            str(path),
        ]
        kwargs: dict[str, Any] = {"stderr": subprocess.DEVNULL}
        if system == "Windows":
            kwargs["creationflags"] = 0x08000000
            kwargs["cwd"] = str(Path(self.sox).parent)
        with self._lock:
            self.process = subprocess.Popen(args, **kwargs)
        time.sleep(0.18)
        if cancel_event.is_set():
            self.cancel()
            raise RuntimeError("Recording cancelled during startup")
        with self._lock:
            process = self.process
        if process is None or process.poll() is not None:
            with self._lock:
                if self.process is process:
                    self.process = None
            raise MicrophoneUnavailableError("No active microphone")

    def stop(self) -> None:
        with self._lock:
            process = self.process
            self.process = None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    def cancel(self) -> None:
        self.stop()


class QtRecordingSession(RecordingSessionGateway):
    """Own one temporary recording until the provider has its snapshot."""

    def __init__(self, recorder: QtRecorder) -> None:
        self.recorder = recorder
        descriptor, raw_path = tempfile.mkstemp(
            prefix="clarifyvoice-recording-",
            suffix=".wav",
            dir=str(_data_directory()),
        )
        os.close(descriptor)
        self.audio_path = Path(raw_path)
        self.audio_path.unlink(missing_ok=True)
        self.cancel_event = threading.Event()
        self.provider_cancel_token = CancellationToken()
        self.start_finished = threading.Event()
        self.shutdown_complete = threading.Event()
        self._workers: set[threading.Thread] = set()
        self._lock = threading.RLock()
        self._started = False
        self._terminal = False
        self._error: Exception | None = None

    def attach_worker(self, worker: Any) -> None:
        with self._lock:
            self._workers.add(worker)

    attach_workflow_worker = attach_worker

    def detach_worker(self, worker: Any) -> None:
        with self._lock:
            self._workers.discard(worker)
            if self._terminal and not self._workers:
                self.shutdown_complete.set()

    def start(self) -> None:
        try:
            self.recorder.start(self.audio_path, self.cancel_event)
            with self._lock:
                self._started = True
        except Exception as error:
            with self._lock:
                self._error = error
            raise
        finally:
            self.start_finished.set()

    def wait_until_started(self) -> None:
        self.start_finished.wait()
        if self._error is not None:
            raise self._error

    def stop(self) -> RecordingSnapshot:
        self.wait_until_started()
        self.recorder.stop()
        time.sleep(0.3)
        try:
            audio_bytes = self.audio_path.read_bytes()
        except OSError as error:
            self.fail(error)
            raise NoUsableAudioError("Recording produced no audio") from error
        if len(audio_bytes) < 1000:
            self.fail(NoUsableAudioError("Recording produced no audio"))
            raise NoUsableAudioError("Recording produced no audio")
        return RecordingSnapshot(
            self.audio_path,
            audio_bytes,
            cancel_token=self.provider_cancel_token,
        )

    def cancel(self) -> None:
        self.cancel_event.set()
        self.provider_cancel_token.cancel()
        self.recorder.cancel()
        self._cleanup()
        self._mark_terminal()

    def complete(self) -> bool:
        self._cleanup()
        self._mark_terminal()
        return True

    def fail(self, error: Exception) -> None:
        with self._lock:
            self._error = error
        self._cleanup()
        self._mark_terminal()

    def _cleanup(self) -> None:
        self.audio_path.unlink(missing_ok=True)

    def _mark_terminal(self) -> None:
        with self._lock:
            self._terminal = True
            if not self._workers:
                self.shutdown_complete.set()


class QtRecordingAudioGateway:
    def __init__(self, recorder: QtRecorder) -> None:
        self.recorder = recorder
        self._active: QtRecordingSession | None = None
        self._lock = threading.Lock()

    def microphone_available(self) -> bool | None:
        return bool(self.recorder.sox)

    def create_session(self) -> QtRecordingSession | None:
        with self._lock:
            if self._active is not None and not self._active.shutdown_complete.is_set():
                return None
            self._active = QtRecordingSession(self.recorder)
            return self._active


class QtClipboardGateway(ClipboardGateway):
    """Copy results without taking focus from the QML shell."""

    def __init__(self) -> None:
        self.adapter = WindowsClipboardAdapter()

    def capture_target(self) -> SelectionTarget | None:
        return None

    def is_target_current(self, target: SelectionTarget) -> bool:
        return False

    def capture_selection(self, target: SelectionTarget):
        return None

    def restore(self, capture) -> None:
        return None

    def apply_result(self, capture, result: str) -> SelectionDisposition:
        self.write_dictation_result(None, result)
        return SelectionDisposition.COPIED

    def write_dictation_result(
        self,
        target: SelectionTarget | None,
        text: str,
    ) -> SelectionDisposition:
        if self.adapter.is_windows:
            self.adapter.write_text(text)
        elif platform.system() == "Darwin":
            subprocess.run(["pbcopy"], input=text.encode(), check=False)
        else:
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=text.encode(),
                check=False,
            )
        return SelectionDisposition.COPIED

    def activate(self, target: SelectionTarget) -> None:
        return None

    def alt_pressed(self) -> bool:
        return False


class QtStatisticsGateway:
    def __init__(self, repositories: ApplicationRepositories) -> None:
        self.repositories = repositories

    def _record(self, event: dict[str, Any]) -> None:
        self.repositories.usage_stats.append(event)

    def record_dictation(
        self,
        context: dict[str, Any],
        duration_seconds: float,
        result: str,
    ) -> None:
        self._record(
            {
                "event": "dictation",
                "provider": context.get("provider", ""),
                "model": context.get("model", ""),
                "mode": context.get("mode", ""),
                "duration_seconds": max(0.0, float(duration_seconds)),
                "result_characters": len(result),
            }
        )

    def record_rewrite(
        self, provider: str, model: str, source: str, result: str
    ) -> None:
        self._record(
            {
                "event": "rewrite",
                "provider": provider,
                "model": model,
                "source_characters": len(source),
                "result_characters": len(result),
            }
        )

    def record_translation(
        self,
        provider: str,
        model: str,
        source: str,
        result: str,
        target_language: str,
    ) -> None:
        self._record(
            {
                "event": "translation",
                "provider": provider,
                "model": model,
                "source_characters": len(source),
                "result_characters": len(result),
                "target_language": target_language,
            }
        )


def create_real_workflow_service(
    scheduler: QtWorkflowScheduler,
    *,
    repositories: ApplicationRepositories | None = None,
) -> WorkflowService:
    """Create the production workflow used by the QML frontend."""

    active = repositories or create_runtime_repositories()
    config = QtWorkflowConfig(active)
    return WorkflowService(
        QtProviderGateway(config),
        QtRecordingAudioGateway(QtRecorder()),
        QtClipboardGateway(),
        config,
        QtStatisticsGateway(active),
        scheduler,
    )


__all__ = [
    "QtRuntimeError",
    "QtWorkflowScheduler",
    "QtWorkflowConfig",
    "QtProviderGateway",
    "QtRecordingSession",
    "QtRecordingAudioGateway",
    "QtClipboardGateway",
    "QtStatisticsGateway",
    "create_runtime_repositories",
    "create_real_workflow_service",
]
