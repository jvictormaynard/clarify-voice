from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest

from audio_file_batch import (
    AudioBatchCancelledError,
    AudioFileBatchService,
    AudioFileStatus,
    FileTranscriptionSelection,
)
from audio_file_batch_ui import (
    AudioFileImportController,
    audio_import_provider_options,
    deduplicate_audio_paths,
)
from provider_http import CancellationToken
from provider_registry import PROVIDER_REGISTRY
from provider_types import ProviderConnection, TranscriptionResult


class _Gateway:
    def __init__(self, failures=None):
        self.failures = dict(failures or {})
        self.requests = []

    def transcribe(self, request, selection, cancel_token: CancellationToken):
        self.requests.append((request, selection))
        remaining = self.failures.get(request.audio_path.name, 0)
        if remaining:
            self.failures[request.audio_path.name] = remaining - 1
            raise RuntimeError("temporary fixture failure")
        return TranscriptionResult(
            text=f"result:{request.audio_path.name}",
            provider_id=selection.provider_id,
            model=selection.model,
        )


class _BlockingGateway:
    def __init__(self):
        self.started = threading.Event()

    def transcribe(self, request, selection, cancel_token: CancellationToken):
        self.started.set()
        while not cancel_token.cancelled:
            cancel_token.wait(0.01)
        raise AudioBatchCancelledError("cancelled")


def _selection() -> FileTranscriptionSelection:
    return FileTranscriptionSelection(
        provider_id="local_asr",
        model="ggml-small",
        language="en",
        connection=ProviderConnection("", ""),
    )


class AudioFileBatchUiSeamTests(unittest.TestCase):
    def test_provider_options_keep_local_and_cloud_routes_explicit(self):
        local = audio_import_provider_options(PROVIDER_REGISTRY, "local")
        cloud = audio_import_provider_options(PROVIDER_REGISTRY, "cloud")

        self.assertEqual(tuple(option.provider_id for option in local), ("local_asr",))
        self.assertEqual(
            {option.provider_id for option in cloud},
            {"gemini", "openai", "groq"},
        )
        self.assertTrue(all(option.execution == "cloud" for option in cloud))
        with self.assertRaises(ValueError):
            audio_import_provider_options(PROVIDER_REGISTRY, "remote")

    def test_picker_paths_are_ordered_and_deduplicated(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "one.wav"
            second = root / "two.wav"
            paths = deduplicate_audio_paths((first, first, second, str(second)))

        self.assertEqual(paths, (first, second))

    def test_resolved_service_path_updates_original_picker_key(self):
        gateway = _Gateway()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "nested").mkdir()
            source = root / "nested" / ".." / "recording.wav"
            source.resolve().write_bytes(b"audio")
            service = AudioFileBatchService(gateway, max_workers=1)
            controller = AudioFileImportController(service)

            controller.start((source,), _selection())
            result = controller.wait()

        self.assertEqual(result.files[0].path, source)
        self.assertEqual(result.files[0].status, AudioFileStatus.SUCCEEDED)
        self.assertEqual(controller.snapshot()[0].path, source)

    def test_retry_failed_keeps_success_and_never_deletes_sources(self):
        gateway = _Gateway({"bad.wav": 1})
        updates = []
        with TemporaryDirectory() as directory:
            root = Path(directory)
            good = root / "good.wav"
            bad = root / "bad.wav"
            good.write_bytes(b"good")
            bad.write_bytes(b"bad")
            service = AudioFileBatchService(gateway, max_workers=1)
            controller = AudioFileImportController(
                service, on_update=updates.append)

            controller.start((good, bad), _selection())
            first = controller.wait()
            self.assertEqual(
                [item.status for item in first.files],
                [AudioFileStatus.SUCCEEDED, AudioFileStatus.FAILED],
            )
            self.assertEqual(first.files[0].text, "result:good.wav")
            self.assertEqual(controller.failed_paths, (bad,))

            controller.retry_failed()
            retried = controller.wait()
            self.assertEqual(
                [item.status for item in retried.files],
                [AudioFileStatus.SUCCEEDED, AudioFileStatus.SUCCEEDED],
            )
            self.assertEqual(retried.files[0].text, "result:good.wav")
            self.assertEqual(retried.files[1].text, "result:bad.wav")
            self.assertEqual(len(gateway.requests), 3)
            self.assertTrue(good.exists())
            self.assertTrue(bad.exists())
            self.assertIn(AudioFileStatus.PENDING, [item.status for item in updates])

    def test_cancel_delegates_to_active_job_and_preserves_source(self):
        gateway = _BlockingGateway()
        with TemporaryDirectory() as directory:
            source = Path(directory) / "recording.wav"
            source.write_bytes(b"audio")
            service = AudioFileBatchService(gateway, max_workers=1)
            controller = AudioFileImportController(service)
            controller.start((source,), _selection())

            self.assertTrue(gateway.started.wait(2))
            self.assertTrue(controller.cancel())
            result = controller.wait(2)

            self.assertTrue(result.cancelled)
            self.assertEqual(result.files[0].status, AudioFileStatus.CANCELLED)
            self.assertTrue(source.exists())
            self.assertFalse(controller.cancel())


if __name__ == "__main__":
    unittest.main()
