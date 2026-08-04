"""Lifecycle tests for the global voice-translation recording bridge."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from provider_http import CancellationToken
from provider_types import TranscriptionResult, TranslationResult
from voice_translation import (
    VoiceTranslationConfig,
    VoiceTranslationPhase,
    VoiceTranslationPublication,
    VoiceTranslationRoute,
)
from voice_translation_runtime import (
    VoiceTranslationRuntime,
    VoiceTranslationRuntimeState,
)


class _Scheduler:
    def run_in_background(self, callback):
        callback()

    def run_recording(self, _recording, callback):
        callback()


class _Recording:
    def __init__(self):
        self.snapshot = SimpleNamespace(cancel_token=CancellationToken())
        self.started = False
        self.stopped = False
        self.completed = False
        self.cancelled = False

    def start(self):
        self.started = True

    def wait_until_started(self):
        if not self.started:
            raise AssertionError("recording was not started")

    def stop(self):
        self.stopped = True
        return self.snapshot

    def complete(self):
        self.completed = True
        return True

    def fail(self, _error):
        return True

    def request_cancel(self):
        self.cancelled = True
        return True

    def cancel(self):
        self.cancelled = True
        return True


class _Provider:
    def __init__(self):
        self.raw = "Olá"
        self.translated = "Hello"
        self.error = None
        self.requests = []

    def transcribe(self, audio_source, source_language):
        return TranscriptionResult(self.raw, "gemini", "gemini-2.5-flash")

    def translate(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return TranslationResult(
            self.translated,
            request.route.provider_id,
            request.route.model_id,
            request.target_language,
        )


class _Clipboard:
    def __init__(self):
        self.published = []

    def capture_target(self):
        return "target"

    def is_target_current(self, target):
        return target == "target"

    def owns_clipboard(self):
        return True

    def publish(self, text, target, disposition):
        self.published.append((text, target, disposition))


def _config():
    return VoiceTranslationConfig(
        route=VoiceTranslationRoute(
            provider_id="openai", model_id="gpt-4o-mini", prompt="literal"
        )
    )


class VoiceTranslationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.provider = _Provider()
        self.clipboard = _Clipboard()
        self.recording = _Recording()
        self.events: list[VoiceTranslationRuntimeState] = []
        self.usage = []
        self.runtime = VoiceTranslationRuntime(
            self.provider,
            self.clipboard,
            lambda: self.recording,
            _Scheduler(),
            _config,
            on_state=self.events.append,
            on_usage=lambda config, state: self.usage.append((config, state)),
        )

    def test_start_stop_runs_once_with_dedicated_route_and_publication(self):
        self.assertTrue(self.runtime.start("target"))
        self.assertFalse(self.runtime.start("other"))
        self.assertTrue(self.runtime.stop())

        self.assertFalse(self.runtime.active)
        self.assertTrue(self.recording.started)
        self.assertTrue(self.recording.stopped)
        self.assertTrue(self.recording.completed)
        self.assertEqual(len(self.usage), 1)
        self.assertEqual(
            [event.phase for event in self.events],
            [
                VoiceTranslationPhase.RECORDING,
                VoiceTranslationPhase.TRANSCRIBING,
                VoiceTranslationPhase.TRANSLATING,
                VoiceTranslationPhase.COMPLETED,
            ],
        )
        self.assertEqual(self.provider.requests[0].route.prompt, "literal")
        self.assertTrue(hasattr(self.provider.requests[0].cancel_event, "cancelled"))
        self.assertEqual(
            self.clipboard.published,
            [("Hello", "target", VoiceTranslationPublication.PASTED)],
        )

    def test_translation_failure_publishes_raw_copy_only(self):
        self.provider.error = RuntimeError("translation unavailable")
        self.runtime.start("target")
        self.runtime.stop()

        result = self.events[-1].workflow_state
        self.assertEqual(self.events[-1].phase, VoiceTranslationPhase.FAILED)
        self.assertIsNotNone(result)
        self.assertEqual(result.published_text, "Olá")
        self.assertEqual(result.publication, VoiceTranslationPublication.COPY_ONLY)
        self.assertEqual(
            self.clipboard.published,
            [("Olá", "target", VoiceTranslationPublication.COPY_ONLY)],
        )

    def test_cancel_during_recording_releases_operation_without_provider_call(self):
        self.runtime.start("target")
        self.assertTrue(self.runtime.cancel())

        self.assertFalse(self.runtime.active)
        self.assertTrue(self.recording.cancelled)
        self.assertEqual(self.events[-1].phase, VoiceTranslationPhase.CANCELLED)
        self.assertEqual(self.provider.requests, [])


if __name__ == "__main__":
    unittest.main()
