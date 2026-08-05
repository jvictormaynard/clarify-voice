"""Deterministic tests for the real QML workflow boundary."""

from __future__ import annotations

import sys
import os
import subprocess
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

try:
    from PySide6.QtCore import QCoreApplication
    from spikes.pyside6.qml_bridge import QmlWorkflowBridge
    from spikes.pyside6.qml_runtime import (
        QtRecordingSession,
        QtWorkflowScheduler,
        create_real_workflow_service,
    )
    from workflows import (
        CancelDictation,
        DismissMicrophoneUnavailable,
        StartDictation,
        StopDictation,
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
        self.assertEqual(bridge.surface, "idle")
        self.assertEqual(bridge.status, "No usable audio was captured")
        self.assertFalse(bridge.canShowResult)

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


@unittest.skipUnless(PYSIDE6_AVAILABLE, "PySide6 is an optional spike dependency")
class QmlRuntimeFactoryTests(unittest.TestCase):
    def test_factory_composes_ui_free_runtime_without_importing_legacy_app(self):
        with TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"CLARIFYVOICE_DATA_DIR": directory}):
                service = create_real_workflow_service(object())

        self.assertIsInstance(service, WorkflowService)
        self.assertNotIn("app", sys.modules)


if __name__ == "__main__":
    unittest.main()
