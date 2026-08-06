"""Deterministic tests for the real QML workflow boundary."""

from __future__ import annotations

import sys
import os
import subprocess
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    from PySide6.QtCore import QCoreApplication
except (ImportError, ModuleNotFoundError):
    PYSIDE6_AVAILABLE = False
else:
    PYSIDE6_AVAILABLE = True

if PYSIDE6_AVAILABLE:
    from spikes.pyside6.qml_bridge import QmlWorkflowBridge
    from spikes.pyside6.qml_runtime import (
        QtProviderGateway,
        QtRecorder,
        QtRecordingSession,
        QtHistoryRecorder,
        QtStatisticsGateway,
        QtWorkflowRuntime,
        QtWorkflowConfig,
        QtWorkflowScheduler,
        create_real_workflow_runtime,
    )
    from provider_types import (
        ProviderCapability,
        RewriteResult,
        TranscriptionResult,
    )
    from repositories import AppConfig, ProviderConfig
    from workflow_config import WorkflowConfig, WorkflowRoute
    from workflows import (
        CancelDictation,
        CancelTranslation,
        ChooseTranslationLanguage,
        DismissMicrophoneUnavailable,
        MicrophoneUnavailableError,
        StartDictation,
        StartRewrite,
        StartTranslation,
        StopDictation,
        RecordingSnapshot,
        WorkflowPhase,
        WorkflowService,
        WorkflowState,
    )


class DeterministicWorkflowService:
    """Small service double used only to exercise the bridge contract."""

    def __init__(self):
        self._state = WorkflowState()
        self._listeners = []
        self.commands = []
        self.finished = []

    @property
    def state(self):
        return self._state

    def subscribe(self, listener):
        self._listeners.append(listener)

    def publish(self, state):
        self._state = state
        for listener in tuple(self._listeners):
            listener(state)

    def dispatch(self, command):
        self.commands.append(command)
        operation_id = self._state.operation_id or 1
        if isinstance(command, StartDictation):
            self.publish(
                WorkflowState(
                    phase=WorkflowPhase.RECORDING,
                    operation_id=operation_id,
                    kind=command.__class__.__name__,
                )
            )
        elif isinstance(command, StopDictation):
            self.publish(
                WorkflowState(
                    phase=WorkflowPhase.PROCESSING,
                    operation_id=operation_id,
                )
            )
            self.publish(
                WorkflowState(
                    phase=WorkflowPhase.COMPLETED,
                    operation_id=operation_id,
                    result_text="Real result",
                )
            )
        elif isinstance(command, CancelDictation):
            self.publish(WorkflowState())
        elif isinstance(command, DismissMicrophoneUnavailable):
            self.publish(WorkflowState())
        return True

    def finish(self, operation_id):
        self.finished.append(operation_id)
        self.publish(WorkflowState())
        return True


@unittest.skipUnless(PYSIDE6_AVAILABLE, "PySide6 is an optional spike dependency")
class QtRecordingSessionTests(unittest.TestCase):
    def test_snapshot_owns_provider_cancellation_until_completion(self):
        class Recorder:
            def __init__(self):
                self.path = None
                self.cancelled = False

            def start(self, path, _cancel_event):
                self.path = path
                path.write_bytes(b"RIFF" + b"0" * 1196)

            def stop(self):
                return None

            def cancel(self):
                self.cancelled = True

        with TemporaryDirectory() as directory:
            recorder = Recorder()
            with patch(
                "spikes.pyside6.qml_runtime._data_directory",
                return_value=Path(directory),
            ):
                session = QtRecordingSession(recorder)
                session.start()
                snapshot = session.stop()

                self.assertIs(snapshot.cancel_token, session.provider_cancel_token)
                self.assertFalse(snapshot.cancel_token.cancelled)
                self.assertIsNotNone(snapshot.duration_seconds)
                self.assertGreaterEqual(snapshot.duration_seconds, 0.0)
                self.assertTrue(session.audio_path.exists())

                session.complete()

            self.assertFalse(session.audio_path.exists())
            self.assertTrue(session.shutdown_complete.is_set())

            session.cancel()
            self.assertTrue(snapshot.cancel_token.cancelled)
            self.assertTrue(recorder.cancelled)

    def test_recorder_uses_configured_windows_microphone(self):
        from microphone_controls import MicrophoneDevice, MicrophoneInventory

        selected = MicrophoneDevice(
            stable_id="selected",
            name="USB microphone",
            input_channels=1,
            is_default=False,
            backend_index=4,
        )
        default = MicrophoneDevice(
            stable_id="default",
            name="System microphone",
            input_channels=1,
            is_default=True,
            backend_index=1,
        )
        inventory = MicrophoneInventory.from_records(
            [selected, default], default_id="default"
        )

        class Config:
            def current(self):
                return SimpleNamespace(
                    microphone=SimpleNamespace(selected_id="selected")
                )

        class InventorySource:
            def snapshot(self):
                return inventory

        recorder = QtRecorder(Config(), InventorySource())
        recorder.sox = "sox"
        process = Mock()
        process.poll.return_value = None
        with (
            patch(
                "spikes.pyside6.qml_runtime.platform.system",
                return_value="Windows",
            ),
            patch(
                "spikes.pyside6.qml_runtime.subprocess.Popen",
                return_value=process,
            ) as popen,
            patch("spikes.pyside6.qml_runtime.time.sleep"),
        ):
            recorder.start(Path("capture.wav"), threading.Event())

        self.assertEqual(
            popen.call_args.args[0][0:4],
            ["sox", "-t", "waveaudio", "USB microphone"],
        )
        recorder.stop()

    def test_recorder_resolves_sox_from_a_frozen_bundle(self):
        from spikes.pyside6 import qml_runtime

        with TemporaryDirectory() as directory:
            bundle_root = Path(directory)
            bundled_sox = bundle_root / "extra" / "sox-14.4.2" / "sox.exe"
            bundled_sox.parent.mkdir(parents=True)
            bundled_sox.write_bytes(b"sox")
            with (
                patch.object(
                    qml_runtime.sys, "_MEIPASS", str(bundle_root), create=True
                ),
                patch.object(qml_runtime.sys, "frozen", True, create=True),
                patch.object(qml_runtime.platform, "system", return_value="Windows"),
                patch.object(qml_runtime, "_sounddevice", None),
                patch.object(qml_runtime.shutil, "which", return_value=None),
            ):
                recorder = QtRecorder()

        self.assertEqual(recorder.sox, str(bundled_sox))

    def test_recorder_rejects_waveaudio_prefix_collision(self):
        from microphone_controls import MicrophoneDevice, MicrophoneInventory

        prefix = "x" * 31
        selected = MicrophoneDevice(
            stable_id="selected",
            name=prefix + "-a",
            input_channels=1,
            is_default=False,
            backend_index=4,
        )
        colliding = MicrophoneDevice(
            stable_id="colliding",
            name=prefix + "-b",
            input_channels=1,
            is_default=False,
            backend_index=5,
        )
        default = MicrophoneDevice(
            stable_id="default",
            name="System microphone",
            input_channels=1,
            is_default=True,
            backend_index=1,
        )
        inventory = MicrophoneInventory.from_records(
            [selected, colliding, default], default_id="default"
        )

        class Config:
            def current(self):
                return SimpleNamespace(
                    microphone=SimpleNamespace(selected_id="selected")
                )

        class InventorySource:
            def snapshot(self):
                return inventory

        recorder = QtRecorder(Config(), InventorySource())
        recorder.sox = "sox"
        with patch(
            "spikes.pyside6.qml_runtime.platform.system", return_value="Windows"
        ):
            with self.assertRaises(MicrophoneUnavailableError):
                recorder.start(Path("capture.wav"), threading.Event())

    def test_recorder_exposes_only_sox_safe_microphones_to_qml(self):
        from microphone_controls import MicrophoneDevice, MicrophoneInventory

        prefix = "x" * 31
        colliding_a = MicrophoneDevice(
            stable_id="a",
            name=prefix + "-a",
            input_channels=1,
            backend_index=4,
        )
        colliding_b = MicrophoneDevice(
            stable_id="b",
            name=prefix + "-b",
            input_channels=1,
            backend_index=5,
        )
        safe = MicrophoneDevice(
            stable_id="safe",
            name="USB microphone",
            input_channels=1,
            backend_index=6,
        )
        inventory = MicrophoneInventory.from_records(
            [colliding_a, colliding_b, safe], default_id="safe"
        )

        with patch(
            "spikes.pyside6.qml_runtime.platform.system", return_value="Windows"
        ):
            recorder = QtRecorder()
            selectable = recorder.selectable_microphone_devices(inventory)

        self.assertEqual([device.stable_id for device in selectable], ["safe"])

    def test_qml_microphone_test_uses_the_selected_backend_handle_and_closes_stream(
        self,
    ):
        from array import array
        from microphone_controls import MicrophoneDevice, MicrophoneInventory
        from spikes.pyside6 import qml_runtime

        selected = MicrophoneDevice(
            stable_id="selected",
            name="USB microphone",
            input_channels=1,
            backend_index=4,
        )
        inventory = MicrophoneInventory.from_records([selected], default_id="selected")

        class InventorySource:
            def snapshot(self):
                return inventory

        class Stream:
            instance = None

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.started = False
                self.stopped = False
                self.closed = False
                Stream.instance = self

            def start(self):
                self.started = True
                self.kwargs["callback"](array("h", [1024, -1024]), 2, None, None)

            def stop(self):
                self.stopped = True

            def close(self):
                self.closed = True

        recorder = QtRecorder(None, InventorySource())
        selection = inventory.resolve("selected")
        fake_sounddevice = SimpleNamespace(RawInputStream=Stream)
        with (
            patch.object(qml_runtime, "_sounddevice", fake_sounddevice),
            patch.object(qml_runtime.platform, "system", return_value="Windows"),
            patch.object(qml_runtime.time, "sleep"),
        ):
            peak = recorder.test_microphone(selection, inventory)

        self.assertGreater(peak, 0.0)
        self.assertEqual(Stream.instance.kwargs["device"], 4)
        self.assertTrue(Stream.instance.started)
        self.assertTrue(Stream.instance.stopped)
        self.assertTrue(Stream.instance.closed)

    def test_recording_session_stops_at_configured_max_duration(self):
        from microphone_controls import RecordingBoundaryReason, RecordingControls

        controls = RecordingControls(max_duration_seconds=0.02, warning_seconds=0)
        config = SimpleNamespace(
            current=lambda: SimpleNamespace(recording_controls=controls)
        )
        boundary = threading.Event()
        reasons = []

        class Recorder:
            def __init__(self):
                self.config = config

            def start(self, path, _cancel_event):
                path.write_bytes(b"RIFF" + b"0" * 1196)

            def stop(self):
                return None

            def cancel(self):
                return None

        with (
            TemporaryDirectory() as directory,
            patch(
                "spikes.pyside6.qml_runtime._data_directory",
                return_value=Path(directory),
            ),
        ):
            session = QtRecordingSession(Recorder())
            session.set_boundary_callback(
                lambda reason: (reasons.append(reason), boundary.set())
            )
            session.start()
            self.assertTrue(boundary.wait(timeout=1))
            self.assertEqual(reasons, [RecordingBoundaryReason.MAX_DURATION])
            session.cancel()


@unittest.skipUnless(PYSIDE6_AVAILABLE, "PySide6 is an optional spike dependency")
class QmlWorkflowBridgeTests(unittest.TestCase):
    def test_bridge_hydrates_saved_mode_and_language(self):
        service = DeterministicWorkflowService()
        service._config = SimpleNamespace(
            current=lambda: SimpleNamespace(
                ui=SimpleNamespace(mode="transcription", language="pt")
            )
        )
        bridge = QmlWorkflowBridge(service)

        self.assertEqual(bridge.mode, "transcription")
        self.assertEqual(bridge.language, "pt")
        bridge.startRecording()
        self.assertEqual(service.commands[0].mode, "transcription")
        self.assertEqual(service.commands[0].language, "pt")

    def test_bridge_maps_real_state_and_finishes_terminal_result(self):
        service = DeterministicWorkflowService()
        copied = []
        completed = []
        bridge = QmlWorkflowBridge(service, copy_runner=copied.append)
        bridge.copyCompleted.connect(completed.append)

        self.assertEqual(bridge.surface, "idle")
        self.assertFalse(bridge.busy)

        bridge.startRecording()
        self.assertIsInstance(service.commands[0], StartDictation)
        self.assertEqual(service.commands[0].mode, "prompt")
        self.assertEqual(service.commands[0].language, "en")
        self.assertEqual(bridge.surface, "recording")
        self.assertTrue(bridge.busy)

        bridge.stopRecording()
        self.assertIsInstance(service.commands[1], StopDictation)
        self.assertEqual(bridge.surface, "success")
        self.assertEqual(bridge.result, "Real result")
        self.assertTrue(bridge.canShowResult)

        bridge.showResult()
        self.assertEqual(bridge.surface, "result")
        self.assertTrue(bridge.copyResult())
        self.assertEqual(copied, ["Real result"])
        self.assertEqual(completed, [True])
        bridge.reset()
        self.assertEqual(service.finished, [1])
        self.assertEqual(bridge.surface, "idle")
        self.assertFalse(bridge.canShowResult)

    def test_bridge_reports_copy_failure_after_runner_finishes(self):
        service = DeterministicWorkflowService()
        completed = []

        def fail_copy(_text):
            raise RuntimeError("clipboard unavailable")

        bridge = QmlWorkflowBridge(service, copy_runner=fail_copy)
        bridge.copyCompleted.connect(completed.append)
        bridge.startRecording()
        bridge.stopRecording()
        bridge.showResult()

        self.assertTrue(bridge.copyResult())
        self.assertEqual(completed, [False])

    def test_bridge_routes_shell_hotkeys_to_workflow_commands(self):
        service = DeterministicWorkflowService()
        bridge = QmlWorkflowBridge(service)

        self.assertTrue(bridge.handleHotkey("recording_hotkey"))
        self.assertIsInstance(service.commands[-1], StartDictation)
        self.assertTrue(bridge.handleHotkey("recording_hotkey"))
        self.assertIsInstance(service.commands[-1], StopDictation)

        bridge.reset()
        self.assertTrue(bridge.handleHotkey("rewrite_hotkey"))
        self.assertIsInstance(service.commands[-1], StartRewrite)
        self.assertTrue(bridge.handleHotkey("translation_hotkey"))
        self.assertIsInstance(service.commands[-1], StartTranslation)

        service.publish(
            WorkflowState(phase=WorkflowPhase.TRANSLATION_PICKER, operation_id=3)
        )
        self.assertTrue(bridge.handleHotkey("escape"))
        self.assertIsInstance(service.commands[-1], CancelTranslation)
        self.assertFalse(bridge.handleHotkey("toggle_visibility"))

    def test_workflow_hotkeys_dismiss_files_before_dispatch(self):
        cases = (
            ("recording_hotkey", StartDictation),
            ("rewrite_hotkey", StartRewrite),
            ("translation_hotkey", StartTranslation),
        )

        for action, command_type in cases:
            with self.subTest(action=action):
                service = DeterministicWorkflowService()
                dispatched_surfaces = []
                bridge = None

                def dispatch_runner(callback):
                    dispatched_surfaces.append(bridge.surface)
                    callback()

                bridge = QmlWorkflowBridge(
                    service,
                    dispatch_runner=dispatch_runner,
                )
                bridge.openFiles()
                self.assertEqual(bridge.surface, "files")

                self.assertTrue(bridge.handleHotkey(action))
                self.assertEqual(dispatched_surfaces, ["idle"])
                self.assertNotEqual(bridge.surface, "files")
                self.assertIsInstance(service.commands[-1], command_type)

    def test_workflow_hotkeys_do_not_overlap_a_running_file_batch(self):
        class SignalProxy:
            def connect(self, callback):
                self.callback = callback

            def emit(self):
                self.callback()

        class AudioBatch:
            def __init__(self):
                self.running = False
                self.runningChanged = SignalProxy()

        service = DeterministicWorkflowService()
        audio_batch = AudioBatch()
        bridge = QmlWorkflowBridge(
            service,
            audio_batch_controller=audio_batch,
        )
        bridge.openFiles()
        audio_batch.running = True
        audio_batch.runningChanged.emit()

        for action in (
            "recording_hotkey",
            "rewrite_hotkey",
            "translation_hotkey",
        ):
            with self.subTest(action=action):
                self.assertFalse(bridge.handleHotkey(action))

        self.assertEqual(bridge.surface, "files")
        self.assertEqual(service.commands, [])

    def test_bridge_routes_voice_translation_hotkey_to_dedicated_handler(self):
        service = DeterministicWorkflowService()
        handler = Mock()
        bridge = QmlWorkflowBridge(
            service,
            voice_translation_handler=handler,
        )

        self.assertTrue(bridge.handleHotkey("voice_translation_hotkey"))
        handler.assert_called_once_with()
        self.assertEqual(service.commands, [])

    def test_voice_translation_hotkey_dismisses_files_only_when_starting(self):
        for voice_active, expected_surface in ((False, "idle"), (True, "files")):
            with self.subTest(voice_active=voice_active):
                service = DeterministicWorkflowService()
                handler = Mock()
                controller = SimpleNamespace(active=False, stateChanged=Mock())
                dispatched_surfaces = []
                bridge = None

                def dispatch_runner(callback):
                    dispatched_surfaces.append(bridge.surface)
                    callback()

                bridge = QmlWorkflowBridge(
                    service,
                    dispatch_runner=dispatch_runner,
                    voice_translation_handler=handler,
                    voice_translation_controller=controller,
                )
                bridge.openFiles()
                self.assertEqual(bridge.surface, "files")

                controller.active = voice_active
                self.assertTrue(bridge.handleHotkey("voice_translation_hotkey"))
                self.assertEqual(dispatched_surfaces, [expected_surface])
                self.assertEqual(bridge.surface, expected_surface)
                handler.assert_called_once_with()

    def test_bridge_keeps_audio_import_exclusive_until_it_stops(self):
        class SignalProxy:
            def __init__(self):
                self.callbacks = []

            def connect(self, callback):
                self.callbacks.append(callback)

            def emit(self):
                for callback in tuple(self.callbacks):
                    callback()

        class AudioBatch:
            def __init__(self):
                self.running = False
                self.runningChanged = SignalProxy()

        service = DeterministicWorkflowService()
        audio_batch = AudioBatch()
        bridge = QmlWorkflowBridge(
            service,
            audio_batch_controller=audio_batch,
        )

        bridge.openFiles()
        self.assertEqual(bridge.surface, "files")
        audio_batch.running = True
        audio_batch.runningChanged.emit()
        self.assertTrue(bridge.busy)
        bridge.closeFiles()
        self.assertEqual(bridge.surface, "files")

        audio_batch.running = False
        audio_batch.runningChanged.emit()
        bridge.closeFiles()
        self.assertEqual(bridge.surface, "idle")

    def test_bridge_exposes_translation_options_and_dispatches_picker_choices(self):
        service = DeterministicWorkflowService()
        bridge = QmlWorkflowBridge(service)

        self.assertFalse(bridge.chooseTranslation("de"))
        self.assertEqual(
            bridge.translationOptions,
            [
                {"code": "en", "label": "English"},
                {"code": "pt", "label": "Portuguese"},
                {"code": "es", "label": "Spanish"},
                {"code": "de", "label": "German"},
                {"code": "ru", "label": "Russian"},
            ],
        )

        service.publish(
            WorkflowState(phase=WorkflowPhase.TRANSLATION_PICKER, operation_id=4)
        )
        self.assertEqual(bridge.surface, "translation_picker")
        self.assertTrue(bridge.chooseTranslation("DE"))
        self.assertIsInstance(service.commands[-1], ChooseTranslationLanguage)
        self.assertEqual(service.commands[-1].language, "de")
        self.assertFalse(bridge.chooseTranslation("fr"))

        service.publish(
            WorkflowState(phase=WorkflowPhase.TRANSLATION_PICKER, operation_id=4)
        )
        self.assertTrue(bridge.cancelTranslation())
        self.assertIsInstance(service.commands[-1], CancelTranslation)

    def test_bridge_cancel_and_error_states_are_non_terminal_for_qml(self):
        service = DeterministicWorkflowService()
        bridge = QmlWorkflowBridge(service)

        bridge.startRecording()
        bridge.cancelRecording()
        self.assertIsInstance(service.commands[1], CancelDictation)
        self.assertEqual(bridge.surface, "idle")
        self.assertFalse(bridge.busy)

        service.publish(
            WorkflowState(
                phase=WorkflowPhase.FAILED,
                operation_id=2,
                status_key="no_audio",
            )
        )
        self.assertEqual(bridge.surface, "error")
        self.assertEqual(bridge.status, "No usable audio was captured")
        self.assertFalse(bridge.canShowResult)
        bridge.reset()
        self.assertEqual(service.finished, [2])
        self.assertEqual(bridge.surface, "idle")

    def test_slots_submit_commands_without_waiting_for_worker(self):
        service = DeterministicWorkflowService()
        entered = threading.Event()
        release = threading.Event()
        workers = []

        def runner(callback):
            def run():
                entered.set()
                release.wait(timeout=2)
                callback()

            worker = threading.Thread(target=run, daemon=True)
            workers.append(worker)
            worker.start()

        bridge = QmlWorkflowBridge(service, dispatch_runner=runner)
        started = time.monotonic()
        bridge.startRecording()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.1)
        self.assertTrue(entered.wait(timeout=1))
        self.assertEqual(service.commands, [])
        release.set()
        workers[0].join(timeout=1)
        self.assertFalse(workers[0].is_alive())
        self.assertEqual(len(service.commands), 1)


@unittest.skipUnless(PYSIDE6_AVAILABLE, "PySide6 is an optional spike dependency")
class QtProviderGatewayTests(unittest.TestCase):
    def test_prompt_mode_keeps_refinement_and_dictionary_processing(self):
        class ConfigRepository:
            path = Path("/tmp/qml-provider-config.json")

            def __init__(self, model):
                self.model = model

            def load(self):
                return self.model

        class Repositories:
            def __init__(self, model):
                self.config = ConfigRepository(model)

        class Dictionary:
            def __init__(self):
                self.applied = None
                self.expanded = []

            def apply_context(self, request):
                self.applied = request
                return replace(request, dictionary_context="Use QML terms")

            def expand(self, text):
                self.expanded.append(text)
                return f"{text} expanded"

        class Metadata:
            default_base_url = "https://provider.test/v1"

            def supports(self, capability):
                return capability is ProviderCapability.TEXT_GENERATION

        class Registry:
            def __init__(self):
                self.transcription_requests = []
                self.rewrite_requests = []

            def describe(self, _provider):
                return Metadata()

            def supports(self, _provider, capability):
                return capability is ProviderCapability.TEXT_GENERATION

            def connection_for_route(self, _provider, connection, _endpoint):
                return connection

            def transcribe(self, provider, request, _connection, _cancel_token):
                self.transcription_requests.append((provider, request))
                return TranscriptionResult("raw transcript", provider, request.model)

            def rewrite(self, provider, request, _connection, _cancel_token):
                self.rewrite_requests.append((provider, request))
                return RewriteResult("refined transcript", provider, request.model)

        workflows = WorkflowConfig(
            transcription=WorkflowRoute(provider_id="openai", model_id="whisper"),
            refinement=WorkflowRoute(
                provider_id="gemini",
                model_id="editor",
                prompt="Rewrite the transcript clearly while preserving its meaning.",
            ),
            rewrite=WorkflowRoute(provider_id="gemini", model_id="editor"),
            translation=WorkflowRoute(provider_id="gemini", model_id="editor"),
            local_asr_refinement=WorkflowRoute(
                provider_id="gemini", model_id="editor", enabled=False
            ),
        )
        config = AppConfig(
            openai=ProviderConfig(
                api_key="openai-key",
                base_url="https://openai.test/v1",
                audio_model="whisper",
                text_model="editor",
            ),
            gemini=ProviderConfig(
                api_key="gemini-key",
                base_url="https://gemini.test/v1",
                text_model="editor",
            ),
            workflows=workflows,
            local_asr_cloud_refinement=False,
        )
        dictionary = Dictionary()
        registry = Registry()
        audio = RecordingSnapshot(Path("recording.wav"), b"audio", cancel_token=None)

        with patch("spikes.pyside6.qml_runtime.PROVIDER_REGISTRY", registry):
            gateway = QtProviderGateway(
                QtWorkflowConfig(Repositories(config)),
                dictionary,
            )
            result = gateway.transcribe(audio, "prompt", "pt")

        self.assertEqual(result.text, "refined transcript expanded")
        self.assertEqual(result.raw_text, "raw transcript")
        self.assertEqual(result.refined_text, "refined transcript expanded")
        self.assertEqual(result.refinement_provider_id, "gemini")
        self.assertEqual(result.refinement_model, "editor")
        transcription_request = registry.transcription_requests[0][1]
        self.assertIn("editing, not summarization", transcription_request.instruction)
        self.assertIn("NEVER answer it", transcription_request.instruction)
        self.assertIn(
            "Output MUST be in Brazilian Portuguese.", transcription_request.instruction
        )
        self.assertIn(
            "Output MUST be in Brazilian Portuguese.", transcription_request.prompt
        )
        self.assertEqual(dictionary.applied.dictionary_context, "")
        self.assertEqual(
            transcription_request.dictionary_context,
            "Use QML terms",
        )
        self.assertEqual(dictionary.expanded, ["refined transcript"])
        self.assertEqual(registry.rewrite_requests[0][0], "gemini")
        self.assertIn(
            "already-transcribed source text",
            registry.rewrite_requests[0][1].instruction,
        )
        self.assertIn(
            "Output MUST be in Brazilian Portuguese.",
            registry.rewrite_requests[0][1].instruction,
        )
        self.assertIn(
            "Workflow-specific instruction",
            registry.rewrite_requests[0][1].instruction,
        )
        self.assertIn(
            "Rewrite the transcript clearly while preserving its meaning.",
            registry.rewrite_requests[0][1].instruction,
        )


@unittest.skipUnless(PYSIDE6_AVAILABLE, "PySide6 is an optional spike dependency")
class QtWorkflowSchedulerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QCoreApplication.instance() or QCoreApplication([])

    def test_worker_callback_is_delivered_on_qt_thread(self):
        scheduler = QtWorkflowScheduler(self.qt_app)
        gui_thread_id = threading.get_ident()
        delivered = threading.Event()
        callback_thread_ids = []

        worker = threading.Thread(
            target=lambda: scheduler.call_soon(
                lambda: (
                    callback_thread_ids.append(threading.get_ident()),
                    delivered.set(),
                )
            ),
            daemon=True,
        )
        worker.start()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())

        deadline = time.monotonic() + 1
        while not delivered.is_set() and time.monotonic() < deadline:
            self.qt_app.processEvents()
            time.sleep(0.005)

        self.assertTrue(delivered.is_set())
        self.assertEqual(callback_thread_ids, [gui_thread_id])

    def test_shutdown_barrier_rejects_new_dispatches(self):
        scheduler = QtWorkflowScheduler(self.qt_app)
        calls = []

        scheduler.begin_shutdown()
        scheduler.run_dispatch(lambda: calls.append("dispatch"))

        self.assertTrue(scheduler.wait_for_dispatches(0.1))
        self.assertEqual(calls, [])

    def test_shutdown_barrier_drains_dispatch_started_before_shutdown(self):
        scheduler = QtWorkflowScheduler(self.qt_app)
        entered = threading.Event()
        release = threading.Event()

        scheduler.run_dispatch(lambda: (entered.set(), release.wait(timeout=1)))
        self.assertTrue(entered.wait(timeout=1))
        scheduler.begin_shutdown()

        drained = threading.Event()
        waiter = threading.Thread(
            target=lambda: (
                scheduler.wait_for_dispatches(1),
                drained.set(),
            ),
            daemon=True,
        )
        waiter.start()
        self.assertFalse(drained.wait(timeout=0.05))
        release.set()
        waiter.join(timeout=1)

        self.assertTrue(drained.is_set())

    def test_qml_runtime_modules_do_not_import_app_at_import_time(self):
        repository_root = Path(__file__).resolve().parents[1]
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(repository_root)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "import spikes.pyside6.qml_bridge; "
                    "import spikes.pyside6.qml_runtime; "
                    "print('app' in sys.modules)"
                ),
            ],
            cwd=repository_root,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "False")


@unittest.skipUnless(PYSIDE6_AVAILABLE, "PySide6 is an optional QML dependency")
class QtWorkflowRuntimeTests(unittest.TestCase):
    def test_audio_file_selection_rejects_disabled_transcription_route(self):
        current = AppConfig()
        disabled_workflows = replace(
            current.workflows,
            transcription=replace(current.workflows.transcription, enabled=False),
        )
        repositories = SimpleNamespace(
            config=SimpleNamespace(
                load=lambda: replace(current, workflows=disabled_workflows)
            )
        )
        runtime = QtWorkflowRuntime(
            None,
            None,
            None,
            None,
            repositories=repositories,
        )

        with self.assertRaisesRegex(RuntimeError, "transcription workflow is disabled"):
            runtime.audio_file_selection()

    def test_shutdown_cancels_waits_and_closes_provider_registry(self):
        calls = []

        class Service:
            def cancel_active(self):
                calls.append("cancel_active")

        class Audio:
            def wait_for_shutdown(self, timeout_seconds):
                calls.append(("wait_for_shutdown", timeout_seconds))
                return True

        class Registry:
            def cancel(self):
                calls.append("provider_cancel")

            def shutdown(self):
                calls.append("provider_shutdown")

        class Clipboard:
            def write_dictation_result(self, _target, text):
                calls.append(("copy_result", text))
                return "copied"

        class Scheduler:
            def begin_shutdown(self):
                calls.append("begin_shutdown")

            def wait_for_dispatches(self, _timeout_seconds):
                calls.append("wait_for_dispatches")
                return True

            def wait_for_background(self, _timeout_seconds):
                calls.append("wait_for_background")
                return True

        runtime = QtWorkflowRuntime(
            Service(),
            Audio(),
            Scheduler(),
            Clipboard(),
            provider_registry=Registry(),
        )

        self.assertEqual(runtime.copy_result("Visible result"), "copied")
        runtime.shutdown(1.25)
        runtime.shutdown(0.01)

        self.assertEqual(
            calls[:7],
            [
                ("copy_result", "Visible result"),
                "begin_shutdown",
                "wait_for_dispatches",
                "cancel_active",
                "provider_cancel",
                "wait_for_dispatches",
                "cancel_active",
            ],
        )
        self.assertEqual(calls[8:], ["wait_for_background", "provider_shutdown"])
        self.assertEqual(calls[7][0], "wait_for_shutdown")
        self.assertGreaterEqual(calls[7][1], 0.0)
        self.assertLessEqual(calls[7][1], 1.25)


@unittest.skipUnless(PYSIDE6_AVAILABLE, "PySide6 is an optional spike dependency")
class QmlRuntimeFactoryTests(unittest.TestCase):
    def test_factory_composes_ui_free_runtime_without_importing_legacy_app(self):
        missing = object()
        legacy_app_before = sys.modules.get("app", missing)
        with TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"CLARIFYVOICE_DATA_DIR": directory}):
                runtime = create_real_workflow_runtime(object())

        self.assertIsInstance(runtime.workflow_service, WorkflowService)
        self.assertIs(runtime.repositories, runtime.history_recorder.repositories)
        self.assertIs(sys.modules.get("app", missing), legacy_app_before)

    def test_factory_constructs_opt_in_history_recorder(self):
        with TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"CLARIFYVOICE_DATA_DIR": directory}):
                runtime = create_real_workflow_runtime(object())

        self.assertIsInstance(runtime.history_recorder, QtHistoryRecorder)
        self.assertFalse(runtime.history_recorder.store.enabled)


@unittest.skipUnless(PYSIDE6_AVAILABLE, "PySide6 is an optional spike dependency")
class QtStatisticsGatewayTests(unittest.TestCase):
    class UsageRepository:
        def __init__(self):
            self.events = []

        def append(self, event):
            self.events.append(event)

    def test_workflow_statistics_use_the_production_event_schema(self):
        repository = self.UsageRepository()
        statistics = QtStatisticsGateway(SimpleNamespace(usage_stats=repository))

        with patch("spikes.pyside6.qml_runtime.time.time", return_value=1234.5):
            statistics.record_dictation(
                {
                    "provider": "openai",
                    "model": "whisper-1",
                    "mode": "prompt",
                },
                12.5,
                "Uma transcrição",
            )
            statistics.record_rewrite("openai", "gpt-4o-mini", "source", "rewritten")
            statistics.record_translation(
                "openai", "gpt-4o-mini", "source", "traduzido", "en-US"
            )

        self.assertEqual(
            [event["type"] for event in repository.events],
            ["recording", "rewrite", "translation"],
        )
        self.assertEqual(repository.events[0]["duration_seconds"], 12.5)
        self.assertEqual(repository.events[0]["mode"], "prompt")
        self.assertEqual(repository.events[1]["models"][0]["purpose"], "refinement")
        self.assertEqual(repository.events[2]["target_language"], "en-US")

    def test_voice_translation_matches_usage_summary_schema_and_keeps_both_legs(self):
        from app import _usage_summary

        repository = self.UsageRepository()
        statistics = QtStatisticsGateway(SimpleNamespace(usage_stats=repository))
        config = SimpleNamespace(
            route=SimpleNamespace(provider_id="openai", model_id="gpt-4o-mini"),
            target_language="de-DE",
        )
        state = SimpleNamespace(
            transcription_provider="openai",
            transcription_model="whisper-1",
            raw_transcript="Olá do microfone",
            translated_text="Hallo vom Mikrofon",
        )

        with patch("spikes.pyside6.qml_runtime.time.time", return_value=1234.5):
            statistics.record_voice_translation(config, state, 45.5)

        self.assertEqual(len(repository.events), 1)
        event = repository.events[0]
        self.assertEqual(event["timestamp"], 1234.5)
        self.assertEqual(event["type"], "voice_translation")
        self.assertEqual(event["mode"], "voice_translation")
        self.assertEqual(event["workflow"], "voice_translation")
        self.assertEqual(event["duration_seconds"], 45.5)
        self.assertEqual(event["transcription_provider"], "openai")
        self.assertEqual(event["transcription_model"], "whisper-1")
        self.assertEqual(event["translation_provider"], "openai")
        self.assertEqual(event["translation_model"], "gpt-4o-mini")
        self.assertEqual(event["target_language"], "de-DE")
        self.assertEqual(event["word_count"], 3)
        self.assertEqual(event["character_count"], len("Olá do microfone"))
        self.assertEqual(
            event["translation_character_count"], len("Hallo vom Mikrofon")
        )
        self.assertGreater(event["estimated_cost_usd"], 0.0045)
        self.assertTrue(event["cost_complete"])
        self.assertEqual(
            [
                (entry["provider"], entry["model"], entry["purpose"])
                for entry in event["models"]
            ],
            [
                ("openai", "whisper-1", "transcription"),
                ("openai", "gpt-4o-mini", "translation"),
            ],
        )

        summary = _usage_summary([event], now=1234.5)
        self.assertEqual(summary["recordings"], 1)
        self.assertEqual(summary["translations"], 1)
        self.assertEqual(summary["total_seconds"], 45.5)
        self.assertEqual(summary["total_words"], 3)
        self.assertEqual(summary["model_calls"], 2)
        self.assertEqual(summary["total_cost_usd"], event["estimated_cost_usd"])


@unittest.skipUnless(PYSIDE6_AVAILABLE, "PySide6 is an optional spike dependency")
class QtHistoryRecorderTests(unittest.TestCase):
    def test_terminal_dictation_preserves_raw_and_refined_history(self):
        from workflows import WorkflowKind

        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"

            class ConfigRepository:
                path = config_path

                def load(self):
                    return AppConfig(
                        ui=AppConfig().ui,
                        history_enabled=True,
                        history_retention_days=30,
                    )

            repositories = SimpleNamespace(config=ConfigRepository())

            class Scheduler:
                def run_in_background(self, callback):
                    callback()

            recorder = QtHistoryRecorder(repositories, Scheduler())
            recorder.on_state(
                WorkflowState(
                    phase=WorkflowPhase.COMPLETED,
                    kind=WorkflowKind.DICTATION,
                    source_text="raw transcript",
                    result_text="refined transcript",
                    refined_text="refined transcript",
                    provider_id="gemini",
                    model="audio-model",
                    refinement_provider_id="openai",
                    refinement_model="text-model",
                )
            )

            records = recorder.store.list_records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].raw_text, "raw transcript")
            self.assertEqual(records[0].refined_text, "refined transcript")
            self.assertEqual(records[0].refinement_provider, "openai")


@unittest.skipUnless(PYSIDE6_AVAILABLE, "PySide6 is an optional spike dependency")
class QtClipboardGatewayTests(unittest.TestCase):
    def test_xclip_failure_is_reported_to_copy_bridge(self):
        from spikes.pyside6.qml_runtime import QtClipboardGateway

        failure = subprocess.CalledProcessError(1, ["xclip", "-selection", "clipboard"])
        with patch(
            "spikes.pyside6.qml_clipboard.subprocess.run", side_effect=failure
        ) as run:
            gateway = QtClipboardGateway(is_windows=False, platform_name="Linux")
            with self.assertRaises(subprocess.CalledProcessError):
                gateway.write_dictation_result(None, "Visible result")

        self.assertTrue(run.call_args.kwargs["check"])


if __name__ == "__main__":
    unittest.main()
