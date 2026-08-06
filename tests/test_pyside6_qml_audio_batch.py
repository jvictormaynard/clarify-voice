from __future__ import annotations

from pathlib import Path
import threading
import time
import unittest

try:
    from PySide6.QtCore import QCoreApplication
    from spikes.pyside6.qml_audio_batch import QmlAudioFileImportController
except (ImportError, ModuleNotFoundError):
    PYSIDE6_AVAILABLE = False
else:
    PYSIDE6_AVAILABLE = True

from audio_file_batch import (
    AudioBatchResult,
    AudioFileResult,
    AudioFileStatus,
    FileTranscriptionSelection,
)
from provider_types import ProviderConnection


class _QueuedScheduler:
    def __init__(self) -> None:
        self.calls: list[tuple[int, object]] = []

    def call_soon(self, callback) -> None:
        self.calls.append((threading.get_ident(), callback))

    def drain(self) -> None:
        while self.calls:
            _thread_id, callback = self.calls.pop(0)
            callback()

    def drain_until(self, predicate, timeout: float = 1.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.drain()
            time.sleep(0.005)
        self.drain()


class _FakeJob:
    def __init__(self) -> None:
        self.done = False
        self.cancelled = False
        self.result = None
        self._finished = threading.Event()

    def cancel(self) -> None:
        self.cancelled = True

    def wait(self, _timeout=None):
        self._finished.wait(2)
        if not self.done:
            raise TimeoutError("fake job did not finish")
        return None

    def finish(self) -> None:
        self.done = True
        self._finished.set()


class _FakeService:
    def __init__(self) -> None:
        self.calls = []
        self.jobs: list[_FakeJob] = []
        self.results: list[dict[Path, AudioFileResult]] = []

    def start(self, paths, selection, *, on_update):
        job = _FakeJob()
        self.calls.append((tuple(paths), selection, on_update))
        self.jobs.append(job)
        self.results.append({})
        return job

    def publish(self, call_index: int, item: AudioFileResult) -> None:
        self.results[call_index][item.path] = item
        self.calls[call_index][2](item)

    def finish(self, call_index: int) -> None:
        paths = self.calls[call_index][0]
        files = tuple(
            self.results[call_index].get(
                path,
                AudioFileResult(path, AudioFileStatus.PENDING),
            )
            for path in paths
        )
        self.jobs[call_index].result = AudioBatchResult(
            files,
            cancelled=self.jobs[call_index].cancelled,
        )
        self.jobs[call_index].finish()


def _selection(
    provider_id: str = "local_asr",
    model: str = "ggml-small",
    language: str = "en",
    mode: str = "transcription",
) -> FileTranscriptionSelection:
    return FileTranscriptionSelection(
        provider_id=provider_id,
        model=model,
        language=language,
        mode=mode,
        connection=ProviderConnection("", ""),
    )


@unittest.skipUnless(PYSIDE6_AVAILABLE, "PySide6 is not installed")
class QmlAudioFileImportControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_start_builds_typed_selection_and_existing_controller_deduplicates(self):
        service = _FakeService()
        scheduler = _QueuedScheduler()
        factory_calls = []

        def selection_factory(provider_id, model, language, mode):
            factory_calls.append((provider_id, model, language, mode))
            return _selection(provider_id, model, language, mode)

        controller = QmlAudioFileImportController(
            service,
            selection_factory=selection_factory,
            call_soon=scheduler.call_soon,
        )

        self.assertTrue(
            controller.start(
                ["one.wav", "one.wav", "two.wav"],
                "local_asr",
                "ggml-small",
                "en",
                "transcription",
            )
        )
        self.assertEqual(
            factory_calls,
            [("local_asr", "ggml-small", "en", "transcription")],
        )
        self.assertEqual(
            service.calls[0][0],
            (Path("one.wav"), Path("two.wav")),
        )
        self.assertEqual(controller.selectedFiles, ["one.wav", "two.wav"])

        service.publish(
            0,
            AudioFileResult(Path("one.wav"), AudioFileStatus.PROCESSING),
        )
        self.assertEqual(controller.files[0]["status"], "pending")
        scheduler.drain()
        self.assertEqual(controller.files[0]["status"], "processing")

    def test_progress_cancel_and_completion_are_exposed(self):
        service = _FakeService()
        scheduler = _QueuedScheduler()
        controller = QmlAudioFileImportController(
            service,
            selection_factory=lambda *_args: _selection(),
            call_soon=scheduler.call_soon,
        )
        self.assertTrue(controller.start(["recording.wav"], "p", "m", "en"))
        self.assertFalse(controller.done)
        self.assertTrue(controller.cancel())
        self.assertTrue(service.jobs[0].cancelled)

        service.publish(
            0,
            AudioFileResult(
                Path("recording.wav"),
                AudioFileStatus.CANCELLED,
                error="Batch cancelled",
            ),
        )
        service.finish(0)
        scheduler.drain_until(lambda: controller.done)

        self.assertTrue(controller.done)
        self.assertFalse(controller.running)
        self.assertEqual(controller.failedCount, 0)
        self.assertFalse(controller.canRetry)
        self.assertEqual(controller.files[0]["error"], "Batch cancelled")
        self.assertEqual(controller.errorFor("recording.wav"), "Batch cancelled")

    def test_failed_files_can_be_retried_with_new_typed_selection(self):
        service = _FakeService()
        scheduler = _QueuedScheduler()
        factory_calls = []

        def selection_factory(provider_id, model, language, mode):
            factory_calls.append((provider_id, model, language, mode))
            return _selection(provider_id, model, language, mode)

        controller = QmlAudioFileImportController(
            service,
            selection_factory=selection_factory,
            call_soon=scheduler.call_soon,
        )
        self.assertTrue(controller.start(["good.wav", "bad.wav"], "p", "m", "en"))
        service.publish(
            0,
            AudioFileResult(Path("good.wav"), AudioFileStatus.SUCCEEDED, text="ok"),
        )
        service.publish(
            0,
            AudioFileResult(Path("bad.wav"), AudioFileStatus.FAILED, error="retry"),
        )
        service.finish(0)
        scheduler.drain_until(lambda: controller.done)
        self.assertTrue(controller.canRetry)
        self.assertEqual(controller.failedCount, 1)

        self.assertTrue(controller.retryFailed("p2", "m2", "pt", "prompt"))
        self.assertEqual(
            service.calls[1][0],
            (Path("bad.wav"),),
        )
        self.assertEqual(
            factory_calls[-1],
            ("p2", "m2", "pt", "prompt"),
        )
        service.publish(
            1,
            AudioFileResult(Path("bad.wav"), AudioFileStatus.SUCCEEDED, text="fixed"),
        )
        service.finish(1)
        scheduler.drain_until(lambda: controller.done and not controller.canRetry)
        self.assertFalse(controller.canRetry)
        self.assertEqual([item["text"] for item in controller.files], ["ok", "fixed"])

    def test_worker_callbacks_only_update_qml_state_after_call_soon(self):
        service = _FakeService()
        scheduler = _QueuedScheduler()
        controller = QmlAudioFileImportController(
            service,
            selection_factory=lambda *_args: _selection(),
            call_soon=scheduler.call_soon,
        )
        self.assertTrue(controller.start(["threaded.wav"], "p", "m", "en"))
        scheduler.drain()
        self.assertEqual(controller.files[0]["status"], "pending")

        callback_thread = []

        def publish_from_worker():
            callback_thread.append(threading.get_ident())
            service.publish(
                0,
                AudioFileResult(
                    Path("threaded.wav"),
                    AudioFileStatus.SUCCEEDED,
                    text="marshaled",
                ),
            )

        worker = threading.Thread(target=publish_from_worker)
        worker.start()
        worker.join()
        self.assertNotEqual(callback_thread[0], threading.get_ident())
        self.assertEqual(controller.files[0]["status"], "pending")

        scheduler.drain()
        self.assertEqual(controller.files[0]["status"], "succeeded")
        self.assertEqual(controller.textFor("threaded.wav"), "marshaled")


if __name__ == "__main__":
    unittest.main()
