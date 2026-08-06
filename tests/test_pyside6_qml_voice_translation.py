"""Focused tests for the QML voice-translation composition boundaries."""

from __future__ import annotations

import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

try:
    from PySide6.QtCore import QCoreApplication
except (ImportError, ModuleNotFoundError):
    PYSIDE6_AVAILABLE = False
else:
    PYSIDE6_AVAILABLE = True

if PYSIDE6_AVAILABLE:
    from provider_http import CancellationToken
    from provider_types import (
        ProviderCapability,
        ProviderMetadata,
        ProviderConnection,
        TranslationRequest,
        TranslationResult,
        TranscriptionRequest,
        TranscriptionResult,
    )
    from repositories import AppConfig, ProviderConfig
    from voice_translation import (
        VoiceTranslationConfigurationError,
        VoiceTranslationConfig,
        VoiceTranslationLanguages,
        VoiceTranslationPhase,
        VoiceTranslationRequest,
        VoiceTranslationRoute,
    )
    from workflow_config import WorkflowConfig, WorkflowRoute
    from workflows import RecordingSnapshot
    from spikes.pyside6.qml_voice_translation import (
        QmlVoiceTranslationController,
        QmlVoiceTranslationProvider,
        QtVoiceTranslationRecording,
        QtVoiceTranslationRecordingFactory,
    )


def _app_config(
    *,
    transcription_route: WorkflowRoute | None = None,
    voice_route: VoiceTranslationRoute | None = None,
) -> AppConfig:
    return AppConfig(
        gemini=ProviderConfig(
            api_key="gemini-key",
            base_url="https://gemini.example/v1",
            audio_model="gemini-audio",
            text_model="gemini-text",
        ),
        openai=ProviderConfig(
            api_key="openai-key",
            base_url="https://openai.example/v1",
            audio_model="openai-audio",
            text_model="openai-text",
        ),
        workflows=WorkflowConfig(
            transcription=transcription_route
            or WorkflowRoute(
                provider_id="gemini",
                model_id="gemini-audio",
                prompt="transcribe only",
                custom_endpoint="https://transcribe.example/v1",
            ),
            refinement=WorkflowRoute(provider_id="gemini", model_id="gemini-text"),
            rewrite=WorkflowRoute(provider_id="gemini", model_id="gemini-text"),
            translation=WorkflowRoute(provider_id="gemini", model_id="gemini-text"),
        ),
        voice_translation=VoiceTranslationConfig(
            languages=VoiceTranslationLanguages("pt-BR", "en-US"),
            route=voice_route
            or VoiceTranslationRoute(
                provider_id="openai",
                model_id="openai-text",
                prompt="translate only",
                custom_endpoint="https://voice.example/v1",
            ),
        ),
    )


@unittest.skipUnless(PYSIDE6_AVAILABLE, "PySide6 is an optional QML dependency")
class QmlVoiceTranslationProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        capabilities = frozenset(
            {
                ProviderCapability.AUDIO_TRANSCRIPTION,
                ProviderCapability.TEXT_GENERATION,
                ProviderCapability.CUSTOM_BASE_URL,
            }
        )
        self.metadata = {
            "gemini": ProviderMetadata(
                "gemini",
                "Gemini",
                capabilities,
                "https://gemini.default/v1",
                "gemini_audio",
                "gemini_text",
                "gemini-audio-default",
                "gemini-text-default",
            ),
            "openai": ProviderMetadata(
                "openai",
                "OpenAI",
                capabilities,
                "https://openai.default/v1",
                "openai_audio",
                "openai_text",
                "openai-audio-default",
                "openai-text-default",
            ),
        }

        class Registry:
            def __init__(inner_self):
                inner_self.transcription_calls = []
                inner_self.translation_calls = []

            def describe(inner_self, provider_id):
                return self.metadata[provider_id]

            def supports(inner_self, provider_id, capability):
                return self.metadata[provider_id].supports(capability)

            def canonical_audio_model(inner_self, _provider_id, model):
                return model

            def connection_for_route(
                inner_self, provider_id, connection, custom_endpoint
            ):
                return ProviderConnection(
                    connection.api_key,
                    custom_endpoint or connection.base_url,
                )

            def transcribe(inner_self, provider_id, request, connection, cancel_token):
                inner_self.transcription_calls.append(
                    (provider_id, request, connection, cancel_token)
                )
                return TranscriptionResult("raw transcript", provider_id, request.model)

            def translate(inner_self, provider_id, request, connection, cancel_token):
                inner_self.translation_calls.append(
                    (provider_id, request, connection, cancel_token)
                )
                return TranslationResult(
                    "translated text",
                    provider_id,
                    request.model,
                    request.target_language,
                )

        self.registry = Registry()
        self.config = _app_config()
        self.provider = QmlVoiceTranslationProvider(
            lambda: self.config,
            registry=self.registry,
        )

    def test_audio_and_translation_use_independent_typed_routes(self):
        transcription_token = CancellationToken()
        translation_token = CancellationToken()
        snapshot = RecordingSnapshot(
            Path("voice.wav"), b"RIFF audio", cancel_token=transcription_token
        )

        transcription = self.provider.transcribe(snapshot, "pt-BR")
        translation = self.provider.translate(
            VoiceTranslationRequest(
                text=transcription.text,
                source_language="pt-BR",
                target_language="en-US",
                route=self.config.voice_translation.route,
                cancel_event=translation_token,
            )
        )

        provider_id, transcription_request, connection, cancel_token = (
            self.registry.transcription_calls[0]
        )
        self.assertEqual(provider_id, "gemini")
        self.assertIsInstance(transcription_request, TranscriptionRequest)
        self.assertEqual(transcription_request.model, "gemini-audio")
        self.assertEqual(transcription_request.language, "pt")
        self.assertEqual(transcription_request.audio_bytes, b"RIFF audio")
        self.assertEqual(connection.api_key, "gemini-key")
        self.assertEqual(connection.base_url, "https://transcribe.example/v1")
        self.assertIs(cancel_token, transcription_token)
        self.assertIsInstance(transcription, TranscriptionResult)

        provider_id, translation_request, connection, cancel_token = (
            self.registry.translation_calls[0]
        )
        self.assertEqual(provider_id, "openai")
        self.assertIsInstance(translation_request, TranslationRequest)
        self.assertEqual(translation_request.model, "openai-text")
        self.assertEqual(translation_request.target_language, "en-US")
        self.assertEqual(translation_request.source_message, "raw transcript")
        self.assertEqual(connection.api_key, "openai-key")
        self.assertEqual(connection.base_url, "https://voice.example/v1")
        self.assertIs(cancel_token, translation_token)
        self.assertIsInstance(translation, TranslationResult)

    def test_disabled_or_unsupported_routes_fail_before_registry_calls(self):
        disabled = QmlVoiceTranslationProvider(
            lambda: _app_config(
                voice_route=VoiceTranslationRoute(
                    provider_id="openai",
                    model_id="openai-text",
                    enabled=False,
                )
            ),
            registry=self.registry,
        )
        with self.assertRaises(VoiceTranslationConfigurationError):
            disabled.translate(
                VoiceTranslationRequest(
                    "text",
                    "pt-BR",
                    "en-US",
                    self.config.voice_translation.route,
                )
            )
        self.assertEqual(self.registry.translation_calls, [])

        unsupported_capability = frozenset(
            {
                ProviderCapability.AUDIO_TRANSCRIPTION,
                ProviderCapability.CUSTOM_BASE_URL,
            }
        )
        self.metadata["openai"] = ProviderMetadata(
            "openai",
            "OpenAI",
            unsupported_capability,
            "https://openai.default/v1",
            "openai_audio",
            "openai_text",
            "openai-audio-default",
            "openai-text-default",
        )
        with self.assertRaises(VoiceTranslationConfigurationError):
            self.provider.translate(
                VoiceTranslationRequest(
                    "text",
                    "pt-BR",
                    "en-US",
                    self.config.voice_translation.route,
                )
            )
        self.assertEqual(self.registry.translation_calls, [])


@unittest.skipUnless(PYSIDE6_AVAILABLE, "PySide6 is an optional QML dependency")
class QtVoiceTranslationRecordingTests(unittest.TestCase):
    def test_factory_wraps_qt_session_and_exposes_request_cancel(self):
        class Token:
            def __init__(self):
                self.cancelled = False

            def cancel(self):
                self.cancelled = True

        class Session:
            def __init__(self):
                self.cancel_event = threading.Event()
                self.provider_cancel_token = Token()
                self.calls = []

            def start(self):
                self.calls.append("start")

            def wait_until_started(self):
                self.calls.append("wait")

            def stop(self):
                self.calls.append("stop")
                return "snapshot"

            def complete(self):
                self.calls.append("complete")
                return True

            def fail(self, error):
                self.calls.append(("fail", error))
                return True

            def cancel(self):
                self.calls.append("cancel")

            def attach_worker(self, worker):
                self.calls.append(("attach", worker))

            def detach_worker(self, worker):
                self.calls.append(("detach", worker))

        class Gateway:
            def __init__(self, session):
                self.session = session

            def create_session(self):
                return self.session

        session = Session()
        recording = QtVoiceTranslationRecordingFactory(Gateway(session))()
        self.assertIsInstance(recording, QtVoiceTranslationRecording)
        worker = object()
        recording.attach_worker(worker)
        recording.attach_workflow_worker(worker)
        recording.detach_worker(worker)
        recording.start()
        recording.wait_until_started()
        self.assertEqual(recording.stop(), "snapshot")
        self.assertTrue(recording.complete())
        self.assertTrue(recording.request_cancel())
        self.assertTrue(session.cancel_event.is_set())
        self.assertTrue(session.provider_cancel_token.cancelled)
        self.assertTrue(recording.cancel())
        self.assertEqual(
            session.calls,
            [
                ("attach", worker),
                ("attach", worker),
                ("detach", worker),
                "start",
                "wait",
                "stop",
                "complete",
                "cancel",
            ],
        )


@unittest.skipUnless(PYSIDE6_AVAILABLE, "PySide6 is an optional QML dependency")
class QmlVoiceTranslationControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QCoreApplication.instance() or QCoreApplication([])

    def test_start_stop_delivers_runtime_states_on_qt_thread(self):
        class Provider:
            def __init__(self):
                self.calls = []

            def transcribe(self, audio_source, source_language):
                self.calls.append(("transcribe", audio_source, source_language))
                return TranscriptionResult("Olá", "gemini", "audio-model")

            def translate(self, request):
                self.calls.append(("translate", request))
                return TranslationResult(
                    "Hello", "openai", "text-model", request.target_language
                )

        class Recording:
            def __init__(self):
                self.started = False
                self.cancel_requested = False

            def start(self):
                self.started = True

            def wait_until_started(self):
                if not self.started:
                    raise AssertionError("recording was not started")

            def stop(self):
                return RecordingSnapshot(Path("voice.wav"), b"audio")

            def complete(self):
                return True

            def fail(self, _error):
                return True

            def request_cancel(self):
                self.cancel_requested = True
                return True

            def cancel(self):
                self.cancel_requested = True
                return True

        class Scheduler:
            def run_recording(self, _recording, callback):
                callback()

            def run_in_background(self, callback):
                callback()

        class Clipboard:
            def capture_target(self):
                return "target"

            def is_target_current(self, target):
                return target == "target"

            def owns_clipboard(self):
                return True

            def publish(self, _text, _target, disposition):
                return disposition

        provider = Provider()
        recording = Recording()
        controller = QmlVoiceTranslationController(
            lambda: _app_config(),
            Clipboard(),
            lambda: recording,
            Scheduler(),
            provider=provider,
        )
        gui_thread_id = threading.get_ident()
        delivered = []
        controller.stateChanged.connect(
            lambda state: delivered.append((state.phase, threading.get_ident()))
        )

        self.assertTrue(controller.start())
        self.assertTrue(controller.stop())
        self.qt_app.processEvents()

        self.assertEqual(
            [phase for phase, _thread_id in delivered],
            [
                VoiceTranslationPhase.RECORDING,
                VoiceTranslationPhase.TRANSCRIBING,
                VoiceTranslationPhase.TRANSLATING,
                VoiceTranslationPhase.COMPLETED,
            ],
        )
        self.assertTrue(all(thread_id == gui_thread_id for _, thread_id in delivered))
        self.assertEqual(controller.phase, VoiceTranslationPhase.COMPLETED.value)
        self.assertFalse(controller.active)
        self.assertEqual(
            [call[0] for call in provider.calls], ["transcribe", "translate"]
        )

        delivered.clear()
        worker = threading.Thread(
            target=lambda: controller._queue_runtime_state(controller.state)
        )
        worker.start()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.qt_app.processEvents()
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0][0], VoiceTranslationPhase.COMPLETED)
        self.assertEqual(delivered[0][1], gui_thread_id)

    def test_cancel_during_recording_reaches_recording_owner(self):
        class Recording:
            def __init__(self):
                self.cancelled = False

            def start(self):
                pass

            def wait_until_started(self):
                pass

            def stop(self):
                return RecordingSnapshot(Path("voice.wav"), b"audio")

            def complete(self):
                return True

            def fail(self, _error):
                return True

            def request_cancel(self):
                self.cancelled = True
                return True

            def cancel(self):
                self.cancelled = True
                return True

        class Scheduler:
            def run_recording(self, _recording, callback):
                callback()

            def run_in_background(self, callback):
                callback()

        recording = Recording()
        provider_calls = []

        class Provider:
            def transcribe(self, *_args):
                provider_calls.append("transcribe")
                return TranscriptionResult("raw", "gemini", "audio")

            def translate(self, *_args):
                provider_calls.append("translate")
                return TranslationResult("translated", "openai", "text", "en")

        controller = QmlVoiceTranslationController(
            lambda: _app_config(),
            SimpleNamespace(
                capture_target=lambda: None,
                is_target_current=lambda _target: False,
                owns_clipboard=lambda: False,
                publish=lambda *_args: None,
            ),
            lambda: recording,
            Scheduler(),
            provider=Provider(),
        )
        self.assertTrue(controller.start())
        self.assertTrue(controller.cancel())
        self.qt_app.processEvents()

        self.assertTrue(recording.cancelled)
        self.assertEqual(controller.phase, VoiceTranslationPhase.CANCELLED.value)
        self.assertEqual(provider_calls, [])


if __name__ == "__main__":
    unittest.main()
