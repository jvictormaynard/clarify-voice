"""Contract tests between the workflow service and the real recording owner."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from provider_types import TranscriptionResult
from workflows import (
    RecordingSnapshot,
    SelectionDisposition,
    SelectionTarget,
    StartDictation,
    StopDictation,
    WorkflowPhase,
    WorkflowService,
)


class ImmediateScheduler:
    def call_soon(self, callback):
        callback()

    def run_in_background(self, callback):
        callback()


class ThreadScheduler(ImmediateScheduler):
    def __init__(self):
        self.threads = []

    def run_in_background(self, callback):
        worker = threading.Thread(target=callback)
        self.threads.append(worker)
        worker.start()

    def join(self):
        for worker in self.threads:
            worker.join(timeout=2)
            if worker.is_alive():
                raise AssertionError("workflow worker did not finish")


class Provider:
    def __init__(self):
        self.audio_source = None
        self.path_existed_during_call = False

    def transcribe(self, audio_source, mode, language):
        self.audio_source = audio_source
        self.path_existed_during_call = audio_source.audio_path.exists()
        return TranscriptionResult("Transcribed", "gemini", "gemini-test")


class Clipboard:
    def __init__(self):
        self.outputs = []

    def write_dictation_result(self, target, text):
        self.outputs.append((target, text))
        return SelectionDisposition.PASTED


class Config:
    def recording_usage_context(self, mode):
        return {"mode": mode}


class Statistics:
    def __init__(self):
        self.dictations = []

    def record_dictation(self, context, duration_seconds, result):
        self.dictations.append((context, duration_seconds, result))


class Clock:
    def __init__(self):
        self.now = 10.0

    def time(self):
        return self.now


class Recorder:
    def __init__(self, *, block_start=False):
        self.block_start = block_start
        self.start_entered = threading.Event()
        self.start_release = threading.Event()
        self.order = []
        self.path = None

    def start(self, audio_path, cancel_event=None):
        self.path = audio_path
        self.order.append("start_entered")
        self.start_entered.set()
        if self.block_start and not self.start_release.wait(timeout=1):
            raise AssertionError("test did not release recording startup")
        audio_path.write_bytes(b"a" * 1200)
        self.order.append("start_terminal")

    def stop(self):
        self.order.append("stop")

    def cancel(self):
        self.order.append("cancel")
        self.start_release.set()


class WorkflowRecordingIntegrationTests(unittest.TestCase):
    def make_service(self, gateway, provider, clipboard, scheduler, clock):
        return WorkflowService(
            provider,
            gateway,
            clipboard,
            Config(),
            Statistics(),
            scheduler,
            clock,
        )

    def test_real_session_snapshots_audio_before_terminal_cleanup(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(app.time, "sleep", return_value=None),
        ):
            path = Path(directory) / "recording.wav"
            recorder = Recorder()
            sessions = []

            def create_session():
                session = app.RecordingSession(recorder, audio_path=path)
                sessions.append(session)
                return session

            gateway = app.RecordingAudioGateway(create_session, lambda: True)
            provider = Provider()
            clipboard = Clipboard()
            clock = Clock()
            service = self.make_service(
                gateway, provider, clipboard, ImmediateScheduler(), clock
            )
            target = SelectionTarget(77, "editor.exe")

            self.assertTrue(service.dispatch(StartDictation(target, "prompt", "en")))
            clock.now = 12.5
            self.assertTrue(service.dispatch(StopDictation()))

            session = sessions[0]
            self.assertIsInstance(provider.audio_source, RecordingSnapshot)
            self.assertEqual(provider.audio_source.audio_bytes, b"a" * 1200)
            self.assertTrue(provider.path_existed_during_call)
            self.assertFalse(path.exists())
            self.assertTrue(session.audio_snapshot_complete.is_set())
            self.assertTrue(session.shutdown_complete.is_set())
            self.assertEqual(
                session.state_history,
                ["created", "recording", "processing", "completed"],
            )
            self.assertEqual(
                recorder.order, ["start_entered", "start_terminal", "stop"]
            )
            self.assertEqual(clipboard.outputs, [(target, "Transcribed")])
            self.assertEqual(service.state.phase, WorkflowPhase.COMPLETED)

    def test_real_session_defers_immediate_stop_until_startup_finishes(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(app.time, "sleep", return_value=None),
        ):
            path = Path(directory) / "recording.wav"
            recorder = Recorder(block_start=True)
            sessions = []

            def create_session():
                session = app.RecordingSession(recorder, audio_path=path)
                sessions.append(session)
                return session

            gateway = app.RecordingAudioGateway(create_session, lambda: True)
            scheduler = ThreadScheduler()
            service = self.make_service(
                gateway, Provider(), Clipboard(), scheduler, Clock()
            )

            service.dispatch(
                StartDictation(SelectionTarget(77, "editor.exe"), "prompt", "en")
            )
            self.assertTrue(recorder.start_entered.wait(timeout=1))
            self.assertTrue(service.dispatch(StopDictation()))
            self.assertNotIn("stop", recorder.order)

            recorder.start_release.set()
            scheduler.join()

            self.assertLess(
                recorder.order.index("start_terminal"), recorder.order.index("stop")
            )
            self.assertEqual(recorder.order.count("start_entered"), 1)
            self.assertEqual(recorder.order.count("stop"), 1)
            self.assertEqual(sessions[0].state, "completed")
            self.assertFalse(path.exists())
            self.assertEqual(service.state.phase, WorkflowPhase.COMPLETED)


if __name__ == "__main__":
    unittest.main()
