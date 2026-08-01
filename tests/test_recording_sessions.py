import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app


class RecordingSessionTests(unittest.TestCase):
    def test_sessions_reserve_unique_paths_and_cleanup_success(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
                app, "DATA_DIR", Path(directory)):
            first = app.RecordingSession(recorder=Mock())
            second = app.RecordingSession(recorder=Mock())

            self.assertNotEqual(first.audio_path, second.audio_path)
            self.assertFalse(first.audio_path.exists())
            first.audio_path.write_bytes(b"audio")
            first.state = "recording"

            self.assertTrue(first.begin_processing())
            self.assertTrue(first.finalize("completed"))
            self.assertEqual(first.state, "completed")
            self.assertFalse(first.audio_path.exists())
            self.assertFalse(first.finalize("failed"))

    def test_cancellation_stops_owner_and_cleans_audio_once(self):
        recorder = Mock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.wav"
            path.write_bytes(b"audio")
            session = app.RecordingSession(recorder=recorder, audio_path=path)
            session.state = "recording"

            self.assertTrue(session.cancel())
            self.assertEqual(session.state, "cancelled")
            self.assertTrue(session.cancel_event.is_set())
            recorder.cancel.assert_called_once_with()
            self.assertFalse(path.exists())
            self.assertFalse(session.cancel())

    def test_provider_exception_is_terminal_and_cleans_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.wav"
            path.write_bytes(b"audio")
            session = app.RecordingSession(recorder=Mock(), audio_path=path)
            session.state = "recording"
            session.begin_processing()
            provider_error = RuntimeError("provider unavailable")

            try:
                raise provider_error
            except RuntimeError as error:
                session.finalize("failed", error)

            self.assertEqual(session.state, "failed")
            self.assertIs(session.error, provider_error)
            self.assertFalse(path.exists())

    def test_start_failure_is_terminal_and_cleans_reserved_audio(self):
        recorder = Mock()
        recorder.start.side_effect = app.MicrophoneUnavailableError("no mic")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.wav"
            path.write_bytes(b"stale")
            session = app.RecordingSession(recorder=recorder, audio_path=path)

            with self.assertRaises(app.MicrophoneUnavailableError):
                session.start()

            self.assertEqual(session.state, "failed")
            self.assertFalse(path.exists())

    def test_cleanup_failure_is_typed_and_fails_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.wav"
            path.write_bytes(b"audio")
            session = app.RecordingSession(recorder=Mock(), audio_path=path)
            session.state = "processing"

            with patch.object(
                    app.Recorder, "_safe_delete",
                    side_effect=app.RecordingCleanupError("locked")):
                session.finalize("completed")

            self.assertEqual(session.state, "failed")
            self.assertIsInstance(session.cleanup_error, app.RecordingCleanupError)
            self.assertIsInstance(session.error, app.RecordingCleanupError)

    def test_stale_process_cleanup_targets_session_path(self):
        command_runner = Mock()
        session_path = Path("C:/Users/test/AppData/Local/ClarifyVoice/recording-42.wav")
        with patch.object(app, "IS_WIN", True), patch.object(
                app.subprocess, "run", command_runner):
            app.Recorder._stop_stale_windows_recorders(session_path)

        command = command_runner.call_args.args[0]
        self.assertIn(str(session_path), command[-1])
        self.assertIn("sox.exe", command[-1])

    def test_unique_session_start_does_not_scan_stale_processes(self):
        stream = Mock()
        fake_sounddevice = SimpleNamespace(
            query_devices=Mock(return_value={"max_input_channels": 1}),
            RawInputStream=Mock(return_value=stream),
        )
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(app, "sd", fake_sounddevice), \
                patch.object(app.subprocess, "Popen") as popen, \
                patch.object(app.Recorder, "_stop_stale_windows_recorders") as cleanup, \
                patch.object(app.time, "sleep"):
            popen.return_value.poll.return_value = None
            recorder = app.Recorder()
            cleanup.assert_called_once_with()
            cleanup.reset_mock()
            recorder.start(Path(directory) / "clarifyvoice-recording-unique.wav")

        cleanup.assert_not_called()

    def test_exit_waits_for_active_upload_then_retries_cleanup(self):
        release_upload = threading.Event()
        upload_started = threading.Event()
        recorder = Mock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.wav"
            path.write_bytes(b"audio")
            session = app.RecordingSession(recorder=recorder, audio_path=path)
            session.state = "processing"

            def upload_worker():
                with path.open("rb"):
                    upload_started.set()
                    release_upload.wait(1)
                session.detach_worker(threading.current_thread())

            worker = threading.Thread(target=upload_worker)
            session.attach_worker(worker)
            worker.start()
            self.assertTrue(upload_started.wait(1))

            original_delete = app.Recorder._safe_delete

            def delete_after_upload(audio_path, *, strict=False):
                if not release_upload.is_set():
                    raise app.RecordingCleanupError("upload still owns file")
                return original_delete(audio_path, strict=strict)

            with patch.object(app.Recorder, "_safe_delete", side_effect=delete_after_upload):
                harness = SimpleNamespace(_recording_session=session)
                app.App._shutdown_recording(harness, timeout=0.05)
                self.assertIsNone(harness._recording_session)
                release_upload.set()
                self.assertTrue(session.wait_for_shutdown(1))

            self.assertEqual(session.state, "cancelled")
            self.assertFalse(path.exists())

    def test_escape_cancel_retains_owner_until_recorder_shutdown(self):
        shutdown_entered = threading.Event()
        release_shutdown = threading.Event()
        recorder = Mock()

        def blocked_cancel():
            shutdown_entered.set()
            release_shutdown.wait(1)

        recorder.cancel.side_effect = blocked_cancel
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.wav"
            path.write_bytes(b"audio")
            session = app.RecordingSession(recorder=recorder, audio_path=path)
            session.state = "recording"
            harness = SimpleNamespace(
                app_state="recording",
                _recording_session=session,
                _set_state=Mock(),
            )

            app.App._cancel(harness)
            self.assertTrue(shutdown_entered.wait(1))
            self.assertIs(harness._recording_session, session)
            app.App._start_recording(harness)
            recorder.start.assert_not_called()

            release_shutdown.set()
            deadline = time.time() + 1
            while harness._recording_session is session and time.time() < deadline:
                time.sleep(0.01)
            self.assertIsNone(harness._recording_session)

    def test_late_worker_cannot_update_newer_session(self):
        old_session = SimpleNamespace()
        new_session = SimpleNamespace()
        harness = SimpleNamespace(
            _recording_session=new_session,
            _closing=False,
        )

        self.assertFalse(app.App._session_is_current(harness, old_session))
        self.assertTrue(app.App._session_is_current(harness, new_session))

    def test_application_exit_cancels_active_session(self):
        session = Mock()
        harness = SimpleNamespace(
            _closing=False,
            _tray_icon=None,
            _single_instance_guard=None,
            _recording_session=session,
            destroy=Mock(side_effect=session.cancel),
        )

        app.App._exit_application(harness)

        self.assertTrue(harness._closing)
        session.cancel.assert_called_once_with()
        harness.destroy.assert_called_once_with()

    def test_simultaneous_hotkeys_do_not_start_or_stop_processing_session(self):
        session = Mock()
        harness = SimpleNamespace(
            _rewrite_active=False,
            _translation_active=False,
            app_state="processing",
            _recording_session=session,
        )

        app.App.toggle_recording(harness)
        app.App.toggle_recording(harness)

        session.begin_processing.assert_not_called()
        session.cancel.assert_not_called()


if __name__ == "__main__":
    unittest.main()
