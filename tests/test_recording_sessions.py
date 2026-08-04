import tempfile
import threading
import time
import unittest
from array import array
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app
from microphone_controls import RecordingBoundaryReason, VADSettings


class RecordingSessionTests(unittest.TestCase):
    def test_boundary_monitor_notifies_lifecycle_callback_and_releases_on_cancel(self):
        class BoundaryRecorder:
            def __init__(self):
                self.boundary_event = threading.Event()
                self.boundary_reason = RecordingBoundaryReason.MAX_DURATION
                self.path = None

            def start(self, audio_path, cancel_event=None):
                self.path = Path(audio_path)
                self.path.write_bytes(b"a" * 1200)

            def stop(self):
                pass

            def cancel(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            recorder = BoundaryRecorder()
            session = app.RecordingSession(
                recorder=recorder,
                audio_path=Path(directory) / "recording.wav",
            )
            callback_seen = threading.Event()
            callback_details = []

            def on_boundary(reason):
                callback_details.append((reason, threading.current_thread().name))
                callback_seen.set()

            session.set_boundary_callback(on_boundary)
            session.start()
            recorder.boundary_event.set()

            self.assertTrue(callback_seen.wait(1))
            self.assertEqual(
                callback_details[0][0], RecordingBoundaryReason.MAX_DURATION)
            self.assertNotEqual(callback_details[0][1], threading.current_thread().name)

            self.assertTrue(session.cancel())
            self.assertTrue(session.shutdown_complete.wait(1))
            self.assertFalse(session._active_workers())

    def test_boundary_monitor_can_be_stopped_without_an_event(self):
        class QuietRecorder:
            def __init__(self):
                self.boundary_event = threading.Event()

            def start(self, audio_path, cancel_event=None):
                Path(audio_path).write_bytes(b"a" * 1200)

            def stop(self):
                pass

            def cancel(self):
                pass

        with tempfile.TemporaryDirectory() as directory, patch.object(
                app.time, "sleep", return_value=None):
            session = app.RecordingSession(
                recorder=QuietRecorder(),
                audio_path=Path(directory) / "recording.wav",
            )
            session.start()
            session.cancel()

            self.assertTrue(session.shutdown_complete.wait(1))
            self.assertFalse(session._active_workers())

    def test_recorder_boundary_callback_sets_typed_reason(self):
        recorder = app.Recorder(
            controls=app.RecordingControls(
                max_duration_seconds=1,
                warning_seconds=0,
            )
        )
        recorder.boundary_policy = app.RecordingBoundaryPolicy(recorder.controls)
        recorder.boundary_policy.start(0)

        with patch.object(app.time, "monotonic", return_value=1):
            recorder._audio_cb(array("h", [1200]).tobytes(), 1, None, None)

        self.assertTrue(recorder.boundary_event.is_set())
        self.assertEqual(
            recorder.boundary_reason, RecordingBoundaryReason.MAX_DURATION)

    def test_recorder_routes_selected_microphone_by_current_backend_handle(self):
        inventory = app.MicrophoneInventory.from_records([
            {
                "name": "System default",
                "host_api": "WASAPI",
                "index": 1,
                "max_input_channels": 1,
                "is_default": True,
            },
            {
                "name": "USB headset",
                "host_api": "WASAPI",
                "index": 7,
                "max_input_channels": 1,
            },
        ])
        selected_id = inventory.devices[1].stable_id
        source = SimpleNamespace(snapshot=lambda: inventory)
        stream = Mock()
        fake_sounddevice = SimpleNamespace(RawInputStream=Mock(return_value=stream))
        process = Mock()
        process.poll.return_value = None

        with tempfile.TemporaryDirectory() as directory, patch.object(
                app, "sd", fake_sounddevice), patch.object(
                app, "IS_WIN", True), patch.object(
                app.subprocess, "Popen", return_value=process), patch.object(
                app.Recorder, "_stop_stale_windows_recorders"), patch.object(
                app.time, "sleep", return_value=None):
            recorder = app.Recorder(
                microphone_source=source,
                controls=app.RecordingControls(),
            )
            recorder.start(
                Path(directory) / "recording.wav",
                microphone=selected_id,
            )

            args = app.subprocess.Popen.call_args.args[0]
            self.assertEqual(
                args[0:4], [app.SOX_EXE, "-t", "waveaudio", "USB headset"])
            stream.start.assert_called_once_with()
            recorder.stop()

    def test_production_recorder_reloads_controls_after_settings_apply(self):
        initial = app.RecordingControls(max_duration_seconds=30)
        updated = app.RecordingControls(
            max_duration_seconds=90,
            warning_seconds=12,
            vad=VADSettings(
                enabled=True,
                level_threshold=0.12,
                minimum_speech_seconds=0.4,
                silence_duration_seconds=1.1,
            ),
        )
        process = Mock()
        process.poll.return_value = None
        original_config = app.APP_CONFIG.copy()

        try:
            app.APP_CONFIG.clear()
            app.APP_CONFIG.update({
                "recording_controls": initial.to_mapping(),
            })
            with tempfile.TemporaryDirectory() as directory, patch.object(
                    app, "_save_app_config") as save_config, patch.object(
                    app, "sd", None), patch.object(
                    app, "IS_WIN", False), patch.object(
                    app.subprocess, "Popen", return_value=process), patch.object(
                    app.Recorder, "_stop_stale_windows_recorders"), patch.object(
                    app.time, "sleep", return_value=None):
                # This is the production construction path: no controls are
                # injected, so the instance is allowed to reload APP_CONFIG.
                recorder = app.Recorder()
                self.assertEqual(recorder.controls, initial)
                self.assertFalse(recorder._controls_override)

                app._apply_settings_transaction(
                    {"provider": "gemini", "model": "gemini-2.5-flash"},
                    {"provider": "openai", "model": "gpt-4o-mini"},
                    [("gemini", "gemini-2.5-flash")],
                    [("openai", "gpt-4o-mini")],
                    {"gemini": "gemini_model"},
                    False,
                    microphone=app.MicrophoneSettings.defaults(),
                    recording_controls=updated,
                )

                save_config.assert_called_once_with(None)
                self.assertEqual(
                    app.APP_CONFIG["recording_controls"], updated.to_mapping())

                recorder.start(Path(directory) / "recording.wav")

                self.assertEqual(recorder.controls, updated)
                self.assertEqual(recorder.boundary_policy.controls, updated)
                recorder.stop()
        finally:
            app.APP_CONFIG.clear()
            app.APP_CONFIG.update(original_config)

    def test_explicit_recorder_controls_remain_overrides_after_settings_apply(self):
        injected = app.RecordingControls(max_duration_seconds=30)
        persisted = app.RecordingControls(max_duration_seconds=90)
        process = Mock()
        process.poll.return_value = None
        original_config = app.APP_CONFIG.copy()

        try:
            app.APP_CONFIG.clear()
            app.APP_CONFIG.update({"recording_controls": persisted.to_mapping()})
            with tempfile.TemporaryDirectory() as directory, patch.object(
                    app, "_recording_controls") as load_controls, patch.object(
                    app, "sd", None), patch.object(
                    app, "IS_WIN", False), patch.object(
                    app.subprocess, "Popen", return_value=process), patch.object(
                    app.Recorder, "_stop_stale_windows_recorders"), patch.object(
                    app.time, "sleep", return_value=None):
                recorder = app.Recorder(controls=injected)
                recorder.start(Path(directory) / "recording.wav")

                self.assertTrue(recorder._controls_override)
                self.assertEqual(recorder.controls, injected)
                self.assertEqual(recorder.boundary_policy.controls, injected)
                load_controls.assert_not_called()
                recorder.stop()
        finally:
            app.APP_CONFIG.clear()
            app.APP_CONFIG.update(original_config)

    def test_source_only_capture_enforces_max_duration_without_level_meter(self):
        class FakeClock:
            def __init__(self):
                self.now = 0.0

            def monotonic(self):
                return self.now

        controls = app.RecordingControls(
            max_duration_seconds=5,
            warning_seconds=1,
        )
        clock = FakeClock()
        process = Mock()
        process.poll.return_value = None

        with tempfile.TemporaryDirectory() as directory, patch.object(
                app, "sd", None), patch.object(
                app, "IS_WIN", False), patch.object(
                app.subprocess, "Popen", return_value=process), patch.object(
                app.Recorder, "_stop_stale_windows_recorders"), patch.object(
                app.time, "sleep", return_value=None):
            recorder = app.Recorder(
                controls=controls,
                boundary_clock=clock,
            )
            recorder.start(Path(directory) / "recording.wav")
            self.assertIsNone(recorder.mic_stream)

            clock.now = 5
            self.assertTrue(recorder.boundary_event.wait(1))
            self.assertEqual(
                recorder.boundary_reason, RecordingBoundaryReason.MAX_DURATION)
            recorder.stop()

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

    def test_cancel_before_start_is_typed_and_signals_start_finished(self):
        class DeferredWorker:
            ident = None

            def __init__(self, target):
                self.target = target

            def start(self):
                pass

            def run(self):
                self.target()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.wav"
            path.write_bytes(b"stale")
            recorder = Mock()
            session = app.RecordingSession(recorder=recorder, audio_path=path)
            errors = []
            worker = DeferredWorker(
                lambda: self._run_session_start(session, errors))
            worker.start()

            session.cancel()
            worker.run()

            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], app.RecordingCancelledError)
            self.assertTrue(session.start_finished.is_set())
            self.assertEqual(session.state, "cancelled")
            self.assertEqual(session.state_history.count("cancelled"), 1)
            recorder.start.assert_not_called()
            recorder.cancel.assert_called_once_with()
            self.assertFalse(path.exists())

    @staticmethod
    def _run_session_start(session, errors):
        try:
            session.start()
        except Exception as error:
            errors.append(error)

    def test_cleanup_failure_does_not_rewrite_completed_terminal_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.wav"
            path.write_bytes(b"audio")
            session = app.RecordingSession(recorder=Mock(), audio_path=path)
            session.start()
            session.begin_processing()

            with patch.object(
                    app.Recorder, "_safe_delete",
                    side_effect=app.RecordingCleanupError("locked")), \
                    patch.object(app, "SESSION_CLEANUP_RETRY_DELAY_SECONDS", 0):
                session.finalize("completed")
                self.assertTrue(session.cleanup_terminal.wait(2))

            self.assertEqual(session.state, "completed")
            self.assertEqual(
                session.state_history,
                ["created", "recording", "processing", "completed"],
            )
            self.assertIsInstance(session.cleanup_error, app.RecordingCleanupError)
            self.assertIsInstance(session.error, app.RecordingCleanupError)

    def test_cleanup_failure_does_not_rewrite_cancelled_terminal_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.wav"
            path.write_bytes(b"audio")
            session = app.RecordingSession(recorder=Mock(), audio_path=path)
            session.start()

            with patch.object(
                    app.Recorder, "_safe_delete",
                    side_effect=app.RecordingCleanupError("locked")), \
                    patch.object(app, "SESSION_CLEANUP_RETRY_DELAY_SECONDS", 0):
                session.cancel()
                self.assertTrue(session.cleanup_terminal.wait(2))

            self.assertEqual(session.state, "cancelled")
            self.assertEqual(session.state_history, ["created", "recording", "cancelled"])
            self.assertIsInstance(session.cleanup_error, app.RecordingCleanupError)
            self.assertTrue(session.cleanup_retry_exhausted)

    def test_cleanup_failure_then_late_success_completes_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.wav"
            path.write_bytes(b"audio")
            session = app.RecordingSession(recorder=Mock(), audio_path=path)
            session.state = "recording"
            attempts = []

            def delete(path_to_remove, *, strict=False):
                attempts.append(path_to_remove)
                if len(attempts) == 1:
                    raise app.RecordingCleanupError("temporarily locked")
                path_to_remove.unlink(missing_ok=True)

            with patch.object(app.Recorder, "_safe_delete", side_effect=delete):
                session.cancel()
                self.assertTrue(session.wait_for_shutdown(2))

            self.assertGreaterEqual(len(attempts), 2)
            self.assertTrue(session._cleanup_done.is_set())
            self.assertTrue(session.shutdown_complete.is_set())
            self.assertFalse(path.exists())
            self.assertIsNone(session.cleanup_error)
            self.assertFalse(session.cleanup_retry_exhausted)
            self.assertEqual(session.state, "cancelled")
            self.assertEqual(session.state_history.count("cancelled"), 1)

    def test_persistent_cleanup_failure_retains_ownership_and_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.wav"
            path.write_bytes(b"audio")
            recorder = Mock()
            session = app.RecordingSession(recorder=recorder, audio_path=path)
            session.state = "recording"
            harness = SimpleNamespace(_recording_session=session)

            with patch.object(
                    app.Recorder, "_safe_delete",
                    side_effect=app.RecordingCleanupError("persistently locked")):
                session.cancel()
                self.assertFalse(session.wait_for_shutdown(2))
                app.App._shutdown_recording(harness, timeout=0.01)
                self.assertIs(harness._recording_session, session)
                app.App._start_recording(harness)
                recorder.start.assert_not_called()
                self.assertFalse(session._cleanup_done.is_set())
                self.assertTrue(session.cleanup_retry_exhausted)
                self.assertIsInstance(
                    session.cleanup_error, app.RecordingCleanupError)
                self.assertTrue(path.exists())

    def test_finisher_retains_session_when_cleanup_is_still_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.wav"
            path.write_bytes(b"audio")
            session = app.RecordingSession(recorder=Mock(), audio_path=path)
            session.state = "recording"
            states = []
            harness = SimpleNamespace(
                _recording_session=session,
                _closing=False,
                _session_is_current=lambda candidate: (
                    harness._recording_session is candidate and not harness._closing),
                _set_state=lambda state, text="": states.append((state, text)),
                _t=lambda key: key,
            )

            with patch.object(
                    app.Recorder, "_safe_delete",
                    side_effect=app.RecordingCleanupError("locked")):
                session.cancel()
                self.assertFalse(session.wait_for_shutdown(2))
                app.App._finish_recording_session(
                    harness, session, error=session.cleanup_error)
                self.assertIs(harness._recording_session, session)
                self.assertEqual(states, [("ready", "error")])
                self.assertTrue(path.exists())

    def test_finisher_releases_after_cleanup_past_ui_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.wav"
            path.write_bytes(b"audio")
            session = app.RecordingSession(recorder=Mock(), audio_path=path)
            session.state = "recording"
            attempts = []
            harness = SimpleNamespace(
                _recording_session=session,
                _closing=False,
                _session_is_current=lambda candidate: (
                    harness._recording_session is candidate and not harness._closing),
                _set_state=Mock(),
                _t=lambda key: key,
                after=lambda _delay, callback: callback(),
            )

            def delete(path_to_remove, *, strict=False):
                attempts.append(path_to_remove)
                if len(attempts) < 4:
                    raise app.RecordingCleanupError("temporarily locked")
                path_to_remove.unlink(missing_ok=True)

            with patch.object(
                    app.Recorder, "_safe_delete", side_effect=delete), \
                    patch.object(app, "SESSION_SHUTDOWN_JOIN_SECONDS", 0.01), \
                    patch.object(app, "SESSION_CLEANUP_RETRY_DELAY_SECONDS", 0.03):
                session.cancel()
                app.App._finish_recording_session(
                    harness, session, error=session.cleanup_error)
                deadline = time.time() + 1
                while (harness._recording_session is session
                       and time.time() < deadline):
                    time.sleep(0.01)

            self.assertGreaterEqual(len(attempts), 4)
            self.assertIsNone(harness._recording_session)
            self.assertTrue(session.shutdown_complete.is_set())
            self.assertTrue(session.cleanup_terminal.is_set())
            self.assertFalse(path.exists())

    def test_exhausted_cleanup_observer_keeps_owner_and_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.wav"
            path.write_bytes(b"audio")
            session = app.RecordingSession(recorder=Mock(), audio_path=path)
            session.state = "recording"
            harness = SimpleNamespace(
                _recording_session=session,
                _closing=False,
                _session_is_current=lambda candidate: (
                    harness._recording_session is candidate and not harness._closing),
                _set_state=Mock(),
                _t=lambda key: key,
                after=Mock(),
            )

            with patch.object(
                    app.Recorder, "_safe_delete",
                    side_effect=app.RecordingCleanupError("persistently locked")):
                session.cancel()
                app.App._finish_recording_session(
                    harness, session, error=session.cleanup_error)
                self.assertTrue(session.cleanup_terminal.wait(2))

            self.assertTrue(session.cleanup_retry_exhausted)
            self.assertFalse(session.shutdown_complete.is_set())
            self.assertIs(harness._recording_session, session)
            harness.after.assert_not_called()
            self.assertTrue(path.exists())

    def test_provider_worker_join_deadline_keeps_file_owned(self):
        release_provider = threading.Event()
        provider_started = threading.Event()
        recorder = Mock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.wav"
            path.write_bytes(b"audio")
            session = app.RecordingSession(recorder=recorder, audio_path=path)
            session.state = "processing"

            def provider_worker():
                provider_started.set()
                release_provider.wait(2)
                session.detach_worker(threading.current_thread())

            worker = threading.Thread(target=provider_worker, daemon=True)
            session.attach_worker(worker)
            worker.start()
            self.assertTrue(provider_started.wait(1))

            with patch.object(app, "SESSION_WORKER_JOIN_SECONDS", 0.01), \
                    patch.object(app, "SESSION_WORKER_GRACE_SECONDS", 0.01), \
                    patch.object(app.Recorder, "_safe_delete") as delete:
                session.finalize("completed")
                self.assertTrue(session.cleanup_terminal.wait(1))
                self.assertFalse(session.shutdown_complete.is_set())
                self.assertIsNotNone(session.shutdown_error)
                self.assertTrue(session.cleanup_retry_exhausted)
                delete.assert_not_called()
                self.assertTrue(path.exists())
                self.assertFalse(session._shutdown_watcher.is_alive())

            release_provider.set()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertTrue(session.wait_for_shutdown(1))
            self.assertFalse(path.exists())

    def test_worker_grace_owner_survives_join_deadline_until_detach(self):
        release_provider = threading.Event()
        provider_started = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.wav"
            path.write_bytes(b"audio")
            session = app.RecordingSession(recorder=Mock(), audio_path=path)
            session.state = "completed"

            def provider_worker():
                provider_started.set()
                release_provider.wait(1)
                session.detach_worker(threading.current_thread())

            worker = threading.Thread(target=provider_worker, daemon=True)
            session.attach_worker(worker)
            worker.start()
            self.assertTrue(provider_started.wait(1))

            def delete(path_to_remove, *, strict=False):
                path_to_remove.unlink(missing_ok=True)

            with patch.object(app, "SESSION_WORKER_JOIN_SECONDS", 0.01), \
                    patch.object(app, "SESSION_WORKER_GRACE_SECONDS", 0.2), \
                    patch.object(app.Recorder, "_safe_delete", side_effect=delete) as cleanup:
                session.finalize("completed")
                time.sleep(0.05)
                self.assertTrue(session._shutdown_watcher.is_alive())
                self.assertFalse(session.shutdown_complete.is_set())
                self.assertFalse(session._cleanup_done.is_set())
                self.assertTrue(path.exists())

                release_provider.set()
                worker.join(1)
                self.assertFalse(worker.is_alive())
                self.assertTrue(session.wait_for_shutdown(1))

            self.assertEqual(cleanup.call_count, 1)
            self.assertFalse(path.exists())

    def test_snapshot_releases_wav_before_unbounded_provider_returns(self):
        provider_release = threading.Event()
        provider_started = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.wav"
            path.write_bytes(b"0" * 1001)
            session = app.RecordingSession(recorder=Mock(), audio_path=path)
            session.state = "processing"

            def provider_worker():
                session.mark_audio_snapshot_complete()
                provider_started.set()
                provider_release.wait(2)
                session.detach_worker(threading.current_thread())

            worker = threading.Thread(target=provider_worker, daemon=True)
            session.attach_worker(worker)
            worker.start()
            self.assertTrue(provider_started.wait(1))

            with patch.object(app, "SESSION_WORKER_JOIN_SECONDS", 0.01):
                session.finalize("completed")
                self.assertTrue(session.wait_for_shutdown(1))
                self.assertTrue(session.cleanup_terminal.is_set())
                self.assertFalse(path.exists())
                self.assertFalse(session._shutdown_watcher.is_alive())

            provider_release.set()
            worker.join(1)
            self.assertFalse(worker.is_alive())

    def test_late_worker_detach_rearms_shutdown_handoff_once(self):
        release_provider = threading.Event()
        active_checked = threading.Event()
        allow_handoff = threading.Event()
        provider_started = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.wav"
            path.write_bytes(b"audio")
            session = app.RecordingSession(recorder=Mock(), audio_path=path)
            session.state = "completed"

            def provider_worker():
                provider_started.set()
                release_provider.wait(1)
                session.detach_worker(threading.current_thread())

            worker = threading.Thread(target=provider_worker, daemon=True)
            session.attach_worker(worker)
            worker.start()
            self.assertTrue(provider_started.wait(1))

            original_active_workers = session._active_workers
            active_calls = [0]

            def active_workers_with_barrier():
                active_calls[0] += 1
                result = original_active_workers()
                if active_calls[0] == 3:
                    active_checked.set()
                    self.assertTrue(allow_handoff.wait(1))
                return result

            def delete(path_to_remove, *, strict=False):
                path_to_remove.unlink(missing_ok=True)

            with patch.object(app, "SESSION_WORKER_JOIN_SECONDS", 0.01), \
                    patch.object(session, "_active_workers",
                                 side_effect=active_workers_with_barrier), \
                    patch.object(app.Recorder, "_safe_delete", side_effect=delete) as cleanup:
                session.finalize("completed")
                self.assertTrue(active_checked.wait(1))
                release_provider.set()
                worker.join(1)
                self.assertFalse(worker.is_alive())
                allow_handoff.set()
                self.assertTrue(session.wait_for_shutdown(1))

            self.assertTrue(session._cleanup_done.is_set())
            self.assertFalse(session.cleanup_retry_exhausted)
            self.assertEqual(cleanup.call_count, 1)
            self.assertFalse(path.exists())
            self.assertFalse(session._shutdown_watcher.is_alive())

    def test_cleanup_exhaustion_rechecks_concurrent_late_success(self):
        failure_returned = threading.Event()
        allow_exhaustion = threading.Event()
        original_cleanup_once = None
        watcher_call = [True]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.wav"
            path.write_bytes(b"audio")
            session = app.RecordingSession(recorder=Mock(), audio_path=path)
            session.state = "completed"

            def delete(path_to_remove, *, strict=False):
                if path_to_remove.exists():
                    if not failure_returned.is_set():
                        raise app.RecordingCleanupError("locked")
                    path_to_remove.unlink()

            def wrapped_cleanup_once():
                result = original_cleanup_once()
                if watcher_call[0] and not result:
                    watcher_call[0] = False
                    failure_returned.set()
                    self.assertTrue(allow_exhaustion.wait(1))
                return result

            with patch.object(app, "SESSION_CLEANUP_RETRY_ATTEMPTS", 1), \
                    patch.object(app.Recorder, "_safe_delete", side_effect=delete):
                original_cleanup_once = session._cleanup_once
                with patch.object(
                        session, "_cleanup_once", side_effect=wrapped_cleanup_once):
                    session._ensure_shutdown_watcher()
                    self.assertTrue(failure_returned.wait(1))

                    competitor = threading.Thread(
                        target=original_cleanup_once, daemon=True)
                    competitor.start()
                    competitor.join(1)
                    self.assertFalse(competitor.is_alive())
                    allow_exhaustion.set()

                    self.assertTrue(session.cleanup_terminal.wait(1))

            self.assertTrue(session._cleanup_done.is_set())
            self.assertTrue(session.shutdown_complete.is_set())
            self.assertFalse(session.cleanup_retry_exhausted)
            self.assertIsNone(session.cleanup_error)
            self.assertFalse(path.exists())

    def test_stale_process_cleanup_targets_session_path(self):
        command_runner = Mock()
        session_path = Path("C:/Users/test/AppData/Local/ClarifyVoice/recording-42.wav")
        with patch.object(app, "IS_WIN", True), patch.object(
                app.subprocess, "run", command_runner):
            app.Recorder._stop_stale_windows_recorders(session_path)

        command = command_runner.call_args.args[0]
        self.assertIn(str(session_path), command[-1])
        self.assertIn("sox.exe", command[-1])

    def test_startup_cleanup_removes_owned_wavs_only(self):
        with tempfile.TemporaryDirectory() as directory, \
                tempfile.TemporaryDirectory() as outside:
            data_dir = Path(directory)
            legacy = data_dir / "temp_recording.wav"
            session_wav = data_dir / "clarifyvoice-recording-ab12CD.wav"
            unrelated = data_dir / "meeting.wav"
            wrong_suffix = data_dir / "clarifyvoice-recording-ab12CD.mp3"
            outside_wav = Path(outside) / "clarifyvoice-recording-outside.wav"
            for path in (legacy, session_wav, unrelated, wrong_suffix, outside_wav):
                path.write_bytes(b"audio")

            with patch.object(app, "IS_WIN", True), patch.object(
                    app, "DATA_DIR", data_dir), patch.object(
                    app, "AUDIO_PATH", legacy):
                app.Recorder._cleanup_orphaned_recordings()

            self.assertFalse(legacy.exists())
            self.assertFalse(session_wav.exists())
            self.assertTrue(unrelated.exists())
            self.assertTrue(wrong_suffix.exists())
            self.assertTrue(outside_wav.exists())

    def test_startup_cleanup_failure_does_not_abort_recorder_init(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            legacy = data_dir / "temp_recording.wav"
            legacy.write_bytes(b"audio")
            with patch.object(app, "IS_WIN", True), patch.object(
                    app, "DATA_DIR", data_dir), patch.object(
                    app, "AUDIO_PATH", legacy), patch.object(
                    app.Recorder, "_stop_stale_windows_recorders"), patch.object(
                    app.Recorder, "_safe_delete",
                    side_effect=PermissionError("locked")):
                recorder = app.Recorder()

            self.assertIsInstance(recorder, app.Recorder)
            self.assertTrue(legacy.exists())

    def test_unix_startup_does_not_remove_another_instance_session(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            session_wav = data_dir / "clarifyvoice-recording-active123.wav"
            session_wav.write_bytes(b"active audio")
            with patch.object(app, "IS_WIN", False), patch.object(
                    app, "DATA_DIR", data_dir), patch.object(
                    app, "AUDIO_PATH", data_dir / "temp_recording.wav"):
                app.Recorder._cleanup_orphaned_recordings()

            self.assertTrue(session_wav.exists())

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
                self.assertIs(harness._recording_session, session)
                self.assertIsNotNone(session._shutdown_watcher)
                self.assertFalse(session._shutdown_watcher.daemon)
                self.assertFalse(session.shutdown_complete.is_set())
                release_upload.set()
                self.assertTrue(session.wait_for_shutdown(1))
                app.App._shutdown_recording(harness, timeout=0.1)
                self.assertIsNone(harness._recording_session)

            self.assertEqual(session.state, "cancelled")
            self.assertFalse(path.exists())

    def test_recorder_cancel_during_stream_setup_rolls_back_everything(self):
        stream_constructing = threading.Event()
        release_stream = threading.Event()
        cancel_event = threading.Event()
        stream = Mock()
        proc = Mock()
        proc.poll.return_value = None

        def construct_stream(**kwargs):
            stream_constructing.set()
            self.assertTrue(release_stream.wait(1))
            return stream

        fake_sounddevice = SimpleNamespace(
            RawInputStream=construct_stream,
            query_devices=Mock(return_value={"max_input_channels": 1}),
        )
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(app, "sd", fake_sounddevice), \
                patch.object(app, "IS_WIN", False), \
                patch.object(app, "_has_active_microphone", return_value=True), \
                patch.object(app, "time") as clock, \
                patch.object(app.subprocess, "Popen", return_value=proc), \
                patch.object(app.Recorder, "_stop_stale_windows_recorders"):
            clock.sleep.return_value = None
            recorder = app.Recorder()
            errors = []

            def start_recorder():
                try:
                    recorder.start(Path(directory) / "recording.wav", cancel_event)
                except Exception as error:
                    errors.append(error)

            starter = threading.Thread(target=start_recorder)
            starter.start()
            self.assertTrue(stream_constructing.wait(1))
            cancel_event.set()
            canceller = threading.Thread(target=recorder.cancel)
            canceller.start()
            time.sleep(0.02)
            self.assertTrue(canceller.is_alive())
            release_stream.set()
            starter.join(1)
            canceller.join(1)

        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], app.RecordingCancelledError)
        stream.stop.assert_called_once_with()
        stream.close.assert_called_once_with()
        proc.terminate.assert_called_once_with()
        self.assertIsNone(recorder.proc)
        self.assertIsNone(recorder.mic_stream)

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

    def test_audio_import_shutdown_cancels_and_waits_with_bounded_timeout(self):
        events = []

        class AudioImport:
            def cancel(self):
                events.append("cancel")

            def wait(self, timeout=None):
                events.append(("wait", timeout))
                raise TimeoutError("fixture keeps the worker active")

        harness = SimpleNamespace(
            _audio_file_import_controller=AudioImport(),
        )

        self.assertFalse(app.App._shutdown_audio_file_import(
            harness, timeout=app.SESSION_SHUTDOWN_JOIN_SECONDS))

        self.assertEqual(events, [
            "cancel",
            ("wait", app.SESSION_SHUTDOWN_JOIN_SECONDS),
        ])

    def test_closed_audio_import_controller_is_retained_until_job_finishes(self):
        wait_started = threading.Event()
        release_wait = threading.Event()

        class AudioImport:
            done = False

            def wait(self):
                wait_started.set()
                release_wait.wait(1)
                self.done = True

        controller = AudioImport()
        harness = SimpleNamespace(_audio_file_import_controller=controller)
        watcher = app.App._retain_audio_file_import_controller(
            harness, controller)

        self.assertTrue(wait_started.wait(1))
        self.assertIs(harness._audio_file_import_controller, controller)
        release_wait.set()
        watcher.join(1)

        self.assertFalse(watcher.is_alive())
        self.assertIsNone(harness._audio_file_import_controller)

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
