from __future__ import annotations

from pathlib import Path
import threading
import time
from tempfile import TemporaryDirectory
import unittest

from audio_file_batch import (
    AudioBatchConfigurationError,
    AudioBatchCancelledError,
    AudioFileBatchService,
    AudioFileStatus,
    AudioFileValidationError,
    FileTranscriptionSelection,
    RegistryAudioTranscriptionGateway,
    RetryableAudioBatchError,
    SUPPORTED_AUDIO_EXTENSIONS,
    UnsupportedAudioFormatError,
    validate_audio_path,
)
from provider_http import CancellationToken
from provider_http import QuotaError
from provider_types import (
    ProviderConnection,
    TranscriptionRequest,
    TranscriptionResult,
)


class _FakeConverter:
    def __init__(self):
        self.calls: list[tuple[Path, Path]] = []

    def convert(self, source, destination, cancel_token):
        if cancel_token.cancelled:
            raise AudioBatchCancelledError("cancelled")
        self.calls.append((Path(source), Path(destination)))
        Path(destination).write_bytes(Path(source).read_bytes() + b"-normalized")
        return Path(destination)


class _FakeGateway:
    def __init__(self, *, fail_names=()):
        self.fail_names = set(fail_names)
        self.requests = []

    def transcribe(self, request, selection, cancel_token):
        self.requests.append((request, selection, cancel_token))
        if request.audio_path.name in self.fail_names:
            raise RuntimeError("provider rejected this fixture")
        return TranscriptionResult(
            text=f"text:{request.audio_path.name}",
            provider_id=selection.provider_id,
            model=selection.model,
        )


def _selection() -> FileTranscriptionSelection:
    return FileTranscriptionSelection(
        provider_id="local_asr",
        model="ggml-small",
        language="en",
        connection=ProviderConnection("", ""),
    )


class AudioFileValidationTests(unittest.TestCase):
    def test_supported_extensions_are_explicit_and_case_insensitive(self):
        self.assertIn(".wav", SUPPORTED_AUDIO_EXTENSIONS)
        self.assertNotIn(".mp3", SUPPORTED_AUDIO_EXTENSIONS)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "recording.FLAC"
            path.write_bytes(b"fixture")
            self.assertEqual(validate_audio_path(path), path.resolve())

    def test_invalid_and_remote_paths_are_rejected_without_network(self):
        with TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.wav"
            with self.assertRaises(AudioFileValidationError):
                validate_audio_path(missing)
            unsupported = Path(directory) / "recording.mp3"
            unsupported.write_bytes(b"fixture")
            with self.assertRaises(UnsupportedAudioFormatError) as raised:
                validate_audio_path(unsupported)
            self.assertIn(".mp3", str(raised.exception))
        with self.assertRaises(AudioFileValidationError) as raised:
            validate_audio_path("https://example.test/audio.wav")
        self.assertIn("local audio files", str(raised.exception))

    def test_selection_and_batch_limits_fail_before_worker_start(self):
        gateway = _FakeGateway()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "input.wav"
            path.write_bytes(b"fixture")
            service = AudioFileBatchService(gateway)
            with self.assertRaises(AudioBatchConfigurationError):
                service.run([path], FileTranscriptionSelection("", "model", "en"))
            limited = AudioFileBatchService(gateway, max_files=1)
            with self.assertRaises(AudioBatchConfigurationError):
                limited.run([path, path], _selection())

    def test_per_file_byte_limit_rejects_before_snapshot_or_provider(self):
        gateway = _FakeGateway()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "large.wav"
            path.write_bytes(b"0123456789")
            result = AudioFileBatchService(
                gateway, max_audio_bytes=4).run([path], _selection())
            self.assertEqual(result.files[0].status, AudioFileStatus.FAILED)
            self.assertIn("limit", result.files[0].error)
            self.assertEqual(gateway.requests, [])


class AudioFileBatchTests(unittest.TestCase):
    def test_partial_failure_is_visible_and_originals_are_never_deleted(self):
        gateway = _FakeGateway(fail_names={"bad.wav"})
        converter = _FakeConverter()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.wav"
            bad = root / "bad.wav"
            converted = root / "input.flac"
            first.write_bytes(b"first")
            bad.write_bytes(b"bad")
            converted.write_bytes(b"flac")
            service = AudioFileBatchService(
                gateway, converter, max_workers=2, temp_root=root / "temp")
            result = service.run([first, bad, converted], _selection())
            self.assertEqual(
                [item.status for item in result.files],
                [AudioFileStatus.SUCCEEDED, AudioFileStatus.FAILED,
                 AudioFileStatus.SUCCEEDED],
            )
            self.assertIn("provider rejected", result.files[1].error)
            self.assertEqual(first.read_bytes(), b"first")
            self.assertEqual(bad.read_bytes(), b"bad")
            self.assertEqual(converted.read_bytes(), b"flac")
            self.assertEqual(list((root / "temp").glob("*")), [])
            self.assertEqual(len(converter.calls), 1)

    def test_conversion_uses_snapshot_path_and_cleans_temporary_directory(self):
        gateway = _FakeGateway()
        converter = _FakeConverter()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "meeting.ogg"
            source.write_bytes(b"original")
            service = AudioFileBatchService(
                gateway, converter, temp_root=root / "conversion")
            result = service.run([source], _selection())
            self.assertEqual(result.files[0].status, AudioFileStatus.SUCCEEDED)
            request = gateway.requests[0][0]
            self.assertEqual(request.audio_path.suffix, ".wav")
            self.assertEqual(request.audio_bytes, b"original-normalized")
            self.assertFalse(request.audio_path.exists())
            self.assertEqual(source.read_bytes(), b"original")
            self.assertEqual(list((root / "conversion").glob("*")), [])

    def test_bounded_concurrency_never_exceeds_configured_workers(self):
        active = 0
        peak = 0
        lock = threading.Lock()

        class TrackingGateway:
            def transcribe(self, request, selection, cancel_token):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                try:
                    time.sleep(0.03)
                    return TranscriptionResult(
                        request.audio_path.name, selection.provider_id, selection.model)
                finally:
                    with lock:
                        active -= 1

        with TemporaryDirectory() as directory:
            root = Path(directory)
            paths = []
            for index in range(8):
                path = root / f"{index}.wav"
                path.write_bytes(b"fixture")
                paths.append(path)
            result = AudioFileBatchService(
                TrackingGateway(), max_workers=2).run(paths, _selection())
            self.assertTrue(all(item.status is AudioFileStatus.SUCCEEDED
                                for item in result.files))
            self.assertLessEqual(peak, 2)

    def test_cancel_stops_new_work_and_cancels_active_provider(self):
        started = threading.Event()
        calls = 0
        lock = threading.Lock()

        class BlockingGateway:
            def transcribe(self, request, selection, cancel_token):
                nonlocal calls
                with lock:
                    calls += 1
                started.set()
                while not cancel_token.cancelled:
                    time.sleep(0.005)
                raise AudioBatchCancelledError("cancelled")

        with TemporaryDirectory() as directory:
            root = Path(directory)
            paths = []
            for index in range(5):
                path = root / f"{index}.wav"
                path.write_bytes(b"fixture")
                paths.append(path)
            job = AudioFileBatchService(
                BlockingGateway(), max_workers=1).start(paths, _selection())
            self.assertTrue(started.wait(2))
            job.cancel()
            result = job.wait(3)
            self.assertTrue(result.cancelled)
            self.assertEqual(calls, 1)
            self.assertTrue(all(item.status is AudioFileStatus.CANCELLED
                                for item in result.files))

    def test_transient_failure_can_be_retried_with_bounded_attempts(self):
        attempts = 0

        class RetryGateway:
            def transcribe(self, request, selection, cancel_token):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise RetryableAudioBatchError("try again")
                return TranscriptionResult("ok", selection.provider_id, selection.model)

        with TemporaryDirectory() as directory:
            path = Path(directory) / "input.wav"
            path.write_bytes(b"fixture")
            result = AudioFileBatchService(
                RetryGateway(), max_attempts=2).run([path], _selection())
            self.assertEqual(result.files[0].status, AudioFileStatus.SUCCEEDED)
            self.assertEqual(result.files[0].attempts, 2)
            self.assertEqual(attempts, 2)

    def test_permanent_quota_failure_is_not_retried(self):
        attempts = 0

        class QuotaGateway:
            def transcribe(self, request, selection, cancel_token):
                nonlocal attempts
                attempts += 1
                raise QuotaError(provider="local-test", operation="transcription")

        with TemporaryDirectory() as directory:
            path = Path(directory) / "input.wav"
            path.write_bytes(b"fixture")
            result = AudioFileBatchService(
                QuotaGateway(), max_attempts=3).run([path], _selection())
            self.assertEqual(result.files[0].status, AudioFileStatus.FAILED)
            self.assertEqual(result.files[0].attempts, 1)
            self.assertEqual(attempts, 1)

    def test_cancellation_wins_when_provider_returns_after_cancellation(self):
        class RaceGateway:
            def transcribe(self, request, selection, cancel_token):
                cancel_token.cancel()
                return TranscriptionResult("late", selection.provider_id, selection.model)

        with TemporaryDirectory() as directory:
            path = Path(directory) / "input.wav"
            path.write_bytes(b"fixture")
            result = AudioFileBatchService(RaceGateway()).run([path], _selection())
            self.assertEqual(result.files[0].status, AudioFileStatus.CANCELLED)
            self.assertFalse(result.files[0].text)


class RegistryGatewayTests(unittest.TestCase):
    def test_registry_gateway_reuses_typed_registry_contract(self):
        seen = {}

        class Registry:
            def transcribe(self, provider_id, request, connection, cancel_token):
                seen.update({
                    "provider_id": provider_id,
                    "request": request,
                    "connection": connection,
                    "cancel_token": cancel_token,
                })
                return TranscriptionResult("text", provider_id, request.model)

        gateway = RegistryAudioTranscriptionGateway(Registry())
        token = CancellationToken()
        request = TranscriptionRequest(
            Path("input.wav"), "model", "en", "instruction", "prompt", 0.0,
            audio_bytes=b"wav")
        selection = _selection()
        result = gateway.transcribe(request, selection, token)
        self.assertEqual(result.provider_id, "local_asr")
        self.assertEqual(seen["provider_id"], "local_asr")
        self.assertIs(seen["request"], request)
        self.assertIs(seen["connection"], selection.connection)
        self.assertIs(seen["cancel_token"], token)


if __name__ == "__main__":
    unittest.main()
