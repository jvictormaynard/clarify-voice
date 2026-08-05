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
from unittest.mock import patch

try:
    from PySide6.QtCore import QCoreApplication
    from spikes.pyside6.qml_bridge import QmlWorkflowBridge
    from spikes.pyside6.qml_runtime import (
        QtProviderGateway,
        QtRecordingSession,
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
        DismissMicrophoneUnavailable,
        StartDictation,
        StopDictation,
        RecordingSnapshot,
        WorkflowPhase,
        WorkflowService,
        WorkflowState,
    )

    PYSIDE6_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    PYSIDE6_AVAILABLE = False


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
                self.assertTrue(session.audio_path.exists())

                session.complete()

            self.assertFalse(session.audio_path.exists())
            self.assertTrue(session.shutdown_complete.is_set())

            session.cancel()
            self.assertTrue(snapshot.cancel_token.cancelled)
            self.assertTrue(recorder.cancelled)


@unittest.skipUnless(PYSIDE6_AVAILABLE, "PySide6 is an optional spike dependency")
class QmlWorkflowBridgeTests(unittest.TestCase):
    def test_bridge_maps_real_state_and_finishes_terminal_result(self):
        service = DeterministicWorkflowService()
        bridge = QmlWorkflowBridge(service)

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
        bridge.reset()
        self.assertEqual(service.finished, [1])
        self.assertEqual(bridge.surface, "idle")
        self.assertFalse(bridge.canShowResult)

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
            transcription=WorkflowRoute(
                provider_id="openai", model_id="whisper", prompt="Transcribe"
            ),
            refinement=WorkflowRoute(
                provider_id="gemini", model_id="editor", prompt="Refine"
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
            result = gateway.transcribe(audio, "prompt", "en")

        self.assertEqual(result.text, "refined transcript expanded")
        self.assertEqual(result.raw_text, "raw transcript")
        self.assertEqual(result.refined_text, "refined transcript expanded")
        self.assertEqual(result.refinement_provider_id, "gemini")
        self.assertEqual(result.refinement_model, "editor")
        self.assertEqual(dictionary.applied.dictionary_context, "")
        self.assertEqual(
            registry.transcription_requests[0][1].dictionary_context,
            "Use QML terms",
        )
        self.assertEqual(dictionary.expanded, ["refined transcript"])
        self.assertEqual(registry.rewrite_requests[0][0], "gemini")


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
            provider_registry=Registry(),
        )

        runtime.shutdown(1.25)
        runtime.shutdown(0.01)

        self.assertEqual(
            calls[:6],
            [
                "begin_shutdown",
                "wait_for_dispatches",
                "cancel_active",
                "provider_cancel",
                "wait_for_dispatches",
                "cancel_active",
            ],
        )
        self.assertEqual(calls[7:], ["wait_for_background", "provider_shutdown"])
        self.assertEqual(calls[6][0], "wait_for_shutdown")
        self.assertGreaterEqual(calls[6][1], 0.0)
        self.assertLessEqual(calls[6][1], 1.25)


@unittest.skipUnless(PYSIDE6_AVAILABLE, "PySide6 is an optional spike dependency")
class QmlRuntimeFactoryTests(unittest.TestCase):
    def test_factory_composes_ui_free_runtime_without_importing_legacy_app(self):
        with TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"CLARIFYVOICE_DATA_DIR": directory}):
                runtime = create_real_workflow_runtime(object())

        self.assertIsInstance(runtime.workflow_service, WorkflowService)
        self.assertNotIn("app", sys.modules)


if __name__ == "__main__":
    unittest.main()
